"""Authentication logic.

Refresh-token rotation with reuse detection:
  * Every refresh token has a row (keyed by its jti).
  * /refresh revokes the presented token and issues a new pair, using ONE conditional
    UPDATE (... WHERE revoked_at IS NULL RETURNING). Two parallel refreshes with the same
    token cannot both win - Postgres row locking lets exactly one UPDATE match.
  * If a token that was already *rotated* shows up again, someone kept a copy: we revoke
    every refresh token of that user AND bump users.token_version, which invalidates all
    of their access tokens too.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import Conflict, Unauthorized
from app.models import RefreshToken, User
from app.schemas import TokenPair
from app.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from app.services.rate_limit import release_login_attempt, reserve_login_attempt


async def register(session: AsyncSession, email: str, password: str, base_currency: str) -> User:
    user = User(email=email, password_hash=await hash_password(password), base_currency=base_currency)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("Email is already registered", code="email_taken") from exc
    await session.refresh(user)
    return user


async def _issue_pair(session: AsyncSession, user: User) -> TokenPair:
    refresh, jti, expires_at = create_refresh_token(user.id)
    session.add(RefreshToken(jti=jti, user_id=user.id, expires_at=expires_at))
    access = create_access_token(user.id, user.token_version)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=get_settings().access_token_ttl_seconds)


async def login(session: AsyncSession, email: str, password: str) -> TokenPair:
    await reserve_login_attempt(email)  # raises 429 when the limit is reached
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    password_ok = await verify_password(user.password_hash if user else None, password)
    if user is None or not password_ok:
        # the reserved slot stays counted as a failure
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    await release_login_attempt(email)
    pair = await _issue_pair(session, user)
    await session.commit()
    return pair


async def _revoke_all(session: AsyncSession, user_id: int, reason: str) -> None:
    now = datetime.now(UTC)
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    await session.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1))


def _jti(payload: dict[str, object]) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload["jti"]))
    except (KeyError, ValueError) as exc:
        raise Unauthorized("Invalid token", code="invalid_token") from exc


async def refresh(session: AsyncSession, token: str) -> TokenPair:
    jti = _jti(decode_token(token, "refresh"))
    now = datetime.now(UTC)
    rotated_user_id = (
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None), RefreshToken.expires_at > now)
            .values(revoked_at=now, revoked_reason="rotated")
            .returning(RefreshToken.user_id)
        )
    ).scalar_one_or_none()

    if rotated_user_id is None:
        row = (await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))).scalar_one_or_none()
        if row is not None and row.revoked_reason == "rotated":
            await _revoke_all(session, row.user_id, "reuse")
            await session.commit()
            raise Unauthorized(
                "Refresh token was already used. All sessions have been revoked.", code="token_reuse_detected"
            )
        await session.rollback()
        raise Unauthorized("Refresh token is invalid or revoked", code="invalid_token")

    user = await session.get(User, rotated_user_id)
    assert user is not None  # FK guarantees it
    pair = await _issue_pair(session, user)
    await session.commit()
    return pair


async def logout(session: AsyncSession, token: str) -> None:
    jti = _jti(decode_token(token, "refresh"))
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason="logout")
    )
    await session.commit()
