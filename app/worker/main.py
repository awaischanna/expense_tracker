"""Background worker: `python -m app.worker.main`

One asyncio process that loops over small, independent jobs:
  * outbox events -> budget notifications
  * pending CSV imports
  * S3 deletions that failed earlier (retried with exponential backoff)
  * housekeeping: expired idempotency keys, old processed outbox rows, stale imports

All coordination goes through PostgreSQL (FOR UPDATE SKIP LOCKED + unique constraints),
so you can run as many worker replicas as you like: they never take the same row and a
replayed event cannot create a duplicate notification.
"""

import asyncio
import logging
import signal
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import storage
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.models import S3Deletion
from app.services import idempotency, imports, outbox

logger = logging.getLogger("worker")
HOUSEKEEPING_EVERY_SECONDS = 300


async def process_s3_deletions(limit: int = 50) -> int:
    async with get_sessionmaker()() as session, session.begin():
        rows = (
            (
                await session.execute(
                    select(S3Deletion)
                    .where(S3Deletion.next_attempt_at <= datetime.now(UTC))
                    .order_by(S3Deletion.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            try:
                await storage.delete_object(row.key)  # S3 delete is idempotent: deleting twice is fine
            except Exception as exc:  # noqa: BLE001
                row.attempts += 1
                row.last_error = str(exc)[:1000]
                backoff = min(2**row.attempts, 3600)
                row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff)
            else:
                await session.delete(row)
        return len(rows)


async def run_once() -> bool:
    """Run every job once. Returns True if anything was done (so we don't sleep)."""
    sm = get_sessionmaker()
    async with sm() as session:
        handled = await outbox.process_outbox_batch(session)
    async with sm() as session:
        imported = await imports.process_next_import(session)
    deleted = await process_s3_deletions()
    return bool(handled or imported or deleted)


async def housekeeping() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        await idempotency.purge_expired(session)
    async with sm() as session:
        await outbox.purge_processed_events(session)
    async with sm() as session:
        await imports.requeue_stale_jobs(session)


async def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    poll = get_settings().worker_poll_seconds
    last_housekeeping = 0.0
    logger.info("worker started")
    while not stop.is_set():
        try:
            if time.monotonic() - last_housekeeping > HOUSEKEEPING_EVERY_SECONDS:
                await housekeeping()
                last_housekeeping = time.monotonic()
            busy = await run_once()
        except Exception:
            logger.exception("worker iteration failed")
            busy = False
        if not busy:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except TimeoutError:
                pass
    # SIGTERM: we finish the current batch (its transaction commits) and exit cleanly.
    await dispose_engine()
    logger.info("worker stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
