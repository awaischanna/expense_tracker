"""Transactional outbox + budget alerts.

Writing side (API): `add_budget_check_event` only adds an OutboxEvent to the *same*
session/transaction as the expense change. Either both rows commit or neither does,
so an event can never be lost and never refers to an expense that was rolled back.

Reading side (worker): `process_outbox_batch`
  * claims events with SELECT ... FOR UPDATE SKIP LOCKED, so two workers never take
    the same event;
  * groups the claimed events by (user, category, month) and checks each group once
    (500k backlogged events for the same category collapse into one SQL query);
  * inserts notifications with ON CONFLICT DO NOTHING on the unique key
    (user_id, category_id, period, level). That makes the handler idempotent: if a
    worker crashes after inserting but before marking the event done, re-running the
    event cannot create a second alert.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Notification, OutboxEvent, User
from app.services.reports import category_spend

logger = logging.getLogger(__name__)

BUDGET_CHECK = "budget.check"
ALERT_LEVELS = (80, 100)
MAX_ATTEMPTS = 10


def add_budget_check_event(session: AsyncSession, user_id: int, category_id: int, spent_on: date) -> None:
    period = spent_on.replace(day=1)
    session.add(
        OutboxEvent(
            event_type=BUDGET_CHECK,
            payload={"user_id": user_id, "category_id": category_id, "period": period.isoformat()},
        )
    )


async def check_budget(session: AsyncSession, user_id: int, category_id: int, period: date) -> int:
    """Create any missing alerts for one category-month. Returns how many new ones were created."""
    user = await session.get(User, user_id)
    if user is None:
        return 0
    rows = await category_spend(session, user_id, user.base_currency, period, category_id)
    if not rows or rows[0]["monthly_budget"] is None:
        return 0  # category deleted or has no budget
    row = rows[0]
    spent: Decimal = row["spent_this_month"]
    budget: Decimal = row["monthly_budget"]
    created = 0
    for level in ALERT_LEVELS:
        if spent * 100 < budget * level:
            continue
        message = (
            f"'{row['name']}' passed {level}% of its {period.strftime('%B %Y')} budget: "
            f"{spent} of {budget} {user.base_currency} used ({row['percent_used']}%)."
        )
        result = await session.execute(
            insert(Notification)
            .values(
                user_id=user_id,
                category_id=category_id,
                period=period,
                level=level,
                message=message,
                spent=spent,
                budget=budget,
            )
            .on_conflict_do_nothing(constraint="uq_notifications_once")
            .returning(Notification.id)
        )
        if result.scalar_one_or_none() is not None:
            created += 1
    return created


async def process_outbox_batch(session: AsyncSession, limit: int | None = None) -> int:
    """Process one batch of events. Returns how many events were handled."""
    limit = limit or get_settings().outbox_batch_size
    async with session.begin():
        events = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.processed_at.is_(None))
                    .order_by(OutboxEvent.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not events:
            return 0

        groups: dict[tuple[int, int, date], list[OutboxEvent]] = {}
        for ev in events:
            if ev.event_type != BUDGET_CHECK:
                ev.processed_at = datetime.now(UTC)
                ev.last_error = f"unknown event type {ev.event_type}"
                continue
            p: dict[str, Any] = ev.payload
            key = (int(p["user_id"]), int(p["category_id"]), date.fromisoformat(p["period"]))
            groups.setdefault(key, []).append(ev)

        now = datetime.now(UTC)
        for (user_id, category_id, period), group in groups.items():
            try:
                async with session.begin_nested():  # SAVEPOINT: one bad group doesn't undo the rest
                    await check_budget(session, user_id, category_id, period)
                for ev in group:
                    ev.processed_at = now
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("budget check failed for %s", (user_id, category_id, period))
                for ev in group:
                    ev.attempts += 1
                    ev.last_error = str(exc)[:1000]
                    if ev.attempts >= MAX_ATTEMPTS:
                        ev.processed_at = now  # give up ("dead letter"); last_error keeps the reason
        return len(events)


async def purge_processed_events(session: AsyncSession, older_than_days: int = 7) -> None:
    """Keep the outbox table small: processed events are only kept for debugging for a week."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    await session.execute(
        delete(OutboxEvent).where(OutboxEvent.processed_at.is_not(None), OutboxEvent.processed_at < cutoff)
    )
    await session.commit()
