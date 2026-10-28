"""Expense business logic."""

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BadRequest, NotFound, PreconditionFailed, PreconditionRequired, Unprocessable
from app.models import Category, Expense, S3Deletion
from app.schemas import ExpenseCreate, ExpenseUpdate
from app.services.outbox import add_budget_check_event
from app.services.reports import ensure_rates_cover


# ---------- helpers ----------
async def get_owned_category(session: AsyncSession, user_id: int, category_id: int) -> Category:
    # Filtering by user_id in the query (not checking afterwards) means another user's
    # category looks exactly like a missing one -> 404, never 403.
    category = (
        await session.execute(select(Category).where(Category.id == category_id, Category.user_id == user_id))
    ).scalar_one_or_none()
    if category is None:
        raise NotFound("Category not found", details=[{"loc": ["category_id"], "msg": "not found"}])
    return category


async def get_owned_expense(session: AsyncSession, user_id: int, expense_id: int) -> Expense:
    expense = (
        await session.execute(select(Expense).where(Expense.id == expense_id, Expense.user_id == user_id))
    ).scalar_one_or_none()
    if expense is None:
        raise NotFound("Expense not found")
    return expense


def etag_for(version: int) -> str:
    return f'"{version}"'


def parse_if_match(if_match: str | None) -> int:
    if if_match is None:
        raise PreconditionRequired("If-Match header is required. Send the ETag from GET /expenses/{id}.")
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"')
    try:
        return int(value)
    except ValueError as exc:
        raise BadRequest('If-Match must be an ETag like "3"', code="invalid_if_match") from exc


# ---------- create ----------
async def create_expense(session: AsyncSession, user_id: int, data: ExpenseCreate) -> Expense:
    """Adds the expense (and its outbox event) to the session. The caller commits."""
    category = await get_owned_category(session, user_id, data.category_id)
    await ensure_rates_cover(session, data.spent_on)
    expense = Expense(
        user_id=user_id,
        category_id=data.category_id,
        amount=data.amount,
        currency=data.currency,
        description=data.description,
        spent_on=data.spent_on,
    )
    session.add(expense)
    if category.monthly_budget is not None:
        add_budget_check_event(session, user_id, category.id, data.spent_on)
    await session.flush()
    await session.refresh(expense)
    return expense


# ---------- list (keyset pagination) ----------
def encode_cursor(spent_on: date, expense_id: int) -> str:
    raw = json.dumps({"s": spent_on.isoformat(), "i": expense_id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[date, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return date.fromisoformat(data["s"]), int(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise BadRequest("Invalid cursor", code="invalid_cursor") from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_expenses(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int,
    cursor: str | None = None,
    category_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    currency: str | None = None,
    q: str | None = None,
) -> tuple[list[Expense], str | None]:
    """Keyset pagination ordered by (spent_on DESC, id DESC).

    The cursor holds the (spent_on, id) of the last row of the previous page and the next
    page is "rows strictly after it": WHERE (spent_on, id) < (:s, :i). Unlike OFFSET, rows
    inserted between page requests cannot shift the window, so nothing is skipped or shown
    twice. id is the tie-breaker because many expenses share one spent_on date.
    Index used: ix_expenses_user_spent_id (user_id, spent_on DESC, id DESC).
    """
    if date_from and date_to and date_from > date_to:
        raise Unprocessable("date_from must be on or before date_to")
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise Unprocessable("min_amount must be less than or equal to max_amount")

    stmt = select(Expense).where(Expense.user_id == user_id)
    if category_id is not None:
        stmt = stmt.where(Expense.category_id == category_id)
    if date_from is not None:
        stmt = stmt.where(Expense.spent_on >= date_from)
    if date_to is not None:
        stmt = stmt.where(Expense.spent_on <= date_to)
    if min_amount is not None:
        stmt = stmt.where(Expense.amount >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(Expense.amount <= max_amount)
    if currency is not None:
        stmt = stmt.where(Expense.currency == currency)
    if q:
        stmt = stmt.where(Expense.description.ilike(f"%{_escape_like(q)}%", escape="\\"))
    if cursor:
        c_date, c_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Expense.spent_on, Expense.id) < tuple_(c_date, c_id))

    stmt = stmt.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].spent_on, rows[-1].id)
    return rows, next_cursor


# ---------- update (optimistic locking) ----------
async def update_expense(
    session: AsyncSession, user_id: int, expense_id: int, expected_version: int, data: ExpenseUpdate
) -> Expense:
    """The version check and the write are ONE statement:

        UPDATE expenses SET ..., version = version + 1
        WHERE id = :id AND user_id = :uid AND version = :expected
        RETURNING *

    If two requests send the same version at the same time, Postgres makes the second
    UPDATE wait for the first one's row lock, then re-checks `version = :expected`
    against the new row - which no longer matches - so it updates 0 rows -> 412.
    A read-then-write in Python could not give this guarantee.
    """
    values: dict[str, Any] = data.model_dump(exclude_unset=True)
    if not values:
        raise Unprocessable("Send at least one field to update")
    if any(values.get(k) is None for k in ("category_id", "amount", "currency", "spent_on") if k in values):
        raise Unprocessable("category_id, amount, currency and spent_on cannot be null")

    category = None
    if "category_id" in values:
        category = await get_owned_category(session, user_id, values["category_id"])
    if "spent_on" in values:
        await ensure_rates_cover(session, values["spent_on"])

    result = await session.execute(
        update(Expense)
        .where(Expense.id == expense_id, Expense.user_id == user_id, Expense.version == expected_version)
        .values(**values, version=Expense.version + 1, updated_at=func.now())
        .returning(Expense)
    )
    expense = result.scalar_one_or_none()
    if expense is None:
        current = (
            await session.execute(select(Expense.version).where(Expense.id == expense_id, Expense.user_id == user_id))
        ).scalar_one_or_none()
        await session.rollback()
        if current is None:
            raise NotFound("Expense not found")
        raise PreconditionFailed(
            "The expense was changed by someone else. Reload it and try again.",
            headers={"ETag": etag_for(current)},
            details=[{"current_version": current}],
        )

    if category is None:
        category = await session.get(Category, expense.category_id)
    if category is not None and category.monthly_budget is not None:
        # Total for the (new) category-month may have gone up -> let the worker check.
        add_budget_check_event(session, user_id, expense.category_id, expense.spent_on)
    await session.commit()
    return expense


# ---------- delete ----------
async def delete_expense(session: AsyncSession, user_id: int, expense_id: int) -> str | None:
    """Deletes the row; if it had a receipt, queues the S3 delete in the SAME transaction.

    Returns the receipt key so the API can try to delete it right away. If that attempt
    fails (S3 down, network), the s3_deletions row is still there and the worker retries.
    """
    receipt_key = (
        await session.execute(
            delete(Expense).where(Expense.id == expense_id, Expense.user_id == user_id).returning(Expense.receipt_key)
        )
    ).first()
    if receipt_key is None:
        await session.rollback()
        raise NotFound("Expense not found")
    key = receipt_key[0]
    if key:
        session.add(S3Deletion(key=key, next_attempt_at=datetime.now(UTC)))
    await session.commit()
    return key
