"""Password hashing (argon2) and JWT helpers.

argon2 is deliberately CPU-heavy (~tens of ms). Running it directly inside an async
handler would block the event loop, so it runs in a worker thread via asyncio.to_thread.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings
from app.errors import Unauthorized

_hasher = PasswordHasher()

# A real hash used when the email does not exist, so "unknown email" and "wrong password"
# take the same time (no user enumeration through timing).
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


def _verify_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(password_hash: str | None, password: str) -> bool:
    ok = await asyncio.to_thread(_verify_sync, password_hash or _DUMMY_HASH, password)
    return ok and password_hash is not None


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(user_id: int, token_version: int) -> str:
    s = get_settings()
    now = _now()
    payload = {
        "sub": str(user_id),
        "type": "access",
        "tv": token_version,
        "iat": now,
        "exp": now + timedelta(seconds=s.access_token_ttl_seconds),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def create_refresh_token(user_id: int) -> tuple[str, uuid.UUID, datetime]:
    s = get_settings()
    now = _now()
    jti = uuid.uuid4()
    expires_at = now + timedelta(seconds=s.refresh_token_ttl_seconds)
    payload = {"sub": str(user_id), "type": "refresh", "jti": str(jti), "iat": now, "exp": expires_at}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm), jti, expires_at


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    s = get_settings()
    try:
        payload = jwt.decode(
            token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub", "type"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Token expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid token", code="invalid_token") from exc
    if payload.get("type") != expected_type:
        raise Unauthorized("Wrong token type", code="invalid_token")
    return payload
