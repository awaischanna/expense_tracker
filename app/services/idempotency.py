"""Idempotency-Key support for POST /expenses.

Race handling uses only a PostgreSQL unique constraint (PRIMARY KEY (user_id, key)):

  1. In the request's transaction we INSERT the key row first
     (INSERT ... ON CONFLICT DO UPDATE ... WHERE expired ... RETURNING).
  2. If we inserted it, we "own" the key: create the expense, store the response on the
     key row, and COMMIT both together.
  3. A second request with the same key that arrives at the same moment runs the same
     INSERT. Postgres sees the first, still-uncommitted row in the unique index and makes
     the second INSERT WAIT until the first transaction ends.
       - first commits  -> conflict, nothing returned -> we read the stored response and
                          replay it (or 422 if the body hash differs);
       - first rolls back (e.g. 404 category) -> the second insert succeeds and does the work.
     So two parallel requests can never both create an expense.

Why not a Redis lock? It would be a second system that must agree with the database. A
lock can expire while the first request is still running (then both run), and if Redis
is down we would have to choose between failing and being unsafe. The unique constraint
is in the same transaction as the expense, so "expense created" and "key recorded" are
atomic. Redis is still used for the login rate limit, where losing data is harmless.

Keys expire after 24h: an expired row can be re-claimed by the same conditional upsert,
and the worker deletes old rows.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import Unprocessable
from app.models import IdempotencyKey


def request_fingerprint(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def claim_key(session: AsyncSession, user_id: int, key: str, fingerprint: str) -> bool:
    """True if this request now owns the key. Must run inside the request's transaction."""
    expires_at = datetime.now(UTC) + timedelta(seconds=get_settings().idempotency_ttl_seconds)
    stmt = insert(IdempotencyKey).values(user_id=user_id, key=key, request_hash=fingerprint, expires_at=expires_at)
    stmt = stmt.on_conflict_do_update(
        index_elements=[IdempotencyKey.user_id, IdempotencyKey.key],
        set_={
            "request_hash": stmt.excluded.request_hash,
            "status_code": None,
            "response_body": None,
            "created_at": func.now(),
            "expires_at": stmt.excluded.expires_at,
        },
        where=IdempotencyKey.expires_at < func.now(),  # only an expired key may be taken over
    ).returning(IdempotencyKey.key)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def stored_response(session: AsyncSession, user_id: int, key: str, fingerprint: str) -> tuple[int, Any]:
    row = (
        await session.execute(
            select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
        )
    ).scalar_one()
    if row.request_hash != fingerprint:
        raise Unprocessable(
            "This Idempotency-Key was already used with a different request body",
            code="idempotency_key_reused",
        )
    assert row.status_code is not None  # written in the same transaction as the key row
    return row.status_code, row.response_body


async def save_response(session: AsyncSession, user_id: int, key: str, status_code: int, body: Any) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
        .values(status_code=status_code, response_body=body)
    )


async def purge_expired(session: AsyncSession) -> int:
    result = await session.execute(delete(IdempotencyKey).where(IdempotencyKey.expires_at < func.now()))
    await session.commit()
    return result.rowcount  # type: ignore[attr-defined,no-any-return]
