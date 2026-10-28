"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import Unauthorized
from app.models import User
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthorized("Missing bearer token", code="missing_token")
    payload = decode_token(creds.credentials, "access")
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise Unauthorized("Invalid token", code="invalid_token") from exc
    user = await session.get(User, user_id)
    # token_version check: after refresh-token reuse is detected we bump the version,
    # which kills every access token issued before that moment.
    if user is None or payload.get("tv") != user.token_version:
        raise Unauthorized("Token revoked", code="token_revoked")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
