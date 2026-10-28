"""Bulk CSV import.

API side (`accept_upload`): streams the upload to disk in 1 MB chunks while computing its
SHA-256 (never the whole file in memory), then inserts an import_jobs row. The unique
constraint (user_id, file_hash) means the same file uploaded twice returns the first job
instead of importing again.

Worker side (`process_next_import`): claims a pending job with FOR UPDATE SKIP LOCKED,
reads the CSV with csv.reader in a thread (file IO is blocking), `batch_size` rows at a
time, validates each row and bulk-inserts the good ones with one multi-row
INSERT ... ON CONFLICT DO NOTHING per batch. Every row gets an import_key
"<file sha256>:<line>" with a unique index, so if the worker crashes half way and the job
is run again, rows already saved are skipped, not duplicated.

Expected CSV header: date,amount,currency,category,description
"""

import asyncio
import csv
import hashlib
import itertools
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os
from fastapi import UploadFile
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import BadRequest
from app.models import Category, Expense, ImportJob, User
from app.services.outbox import add_budget_check_event
from app.services.reports import rates_start_date

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("date", "amount", "currency", "category")
MAX_ERRORS_STORED = 1000
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
CHUNK = 1024 * 1024


# ---------------- API side ----------------
async def accept_upload(session: AsyncSession, user_id: int, upload: UploadFile) -> tuple[ImportJob, bool]:
    """Returns (job, is_duplicate)."""
    s = get_settings()
    await aiofiles.os.makedirs(s.import_dir, exist_ok=True)
    job_id = uuid.uuid4()
    path = Path(s.import_dir) / f"{job_id}.csv"
    digest = hashlib.sha256()
    size = 0
    try:
        async with aiofiles.open(path, "wb") as out:
            while chunk := await upload.read(CHUNK):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise BadRequest("CSV file is too large (max 50 MB)", code="file_too_large")
                digest.update(chunk)
                await out.write(chunk)
        if size == 0:
            raise BadRequest("CSV file is empty", code="empty_file")

        file_hash = digest.hexdigest()
        new_id = (
            await session.execute(
                insert(ImportJob)
                .values(id=job_id, user_id=user_id, file_hash=file_hash, file_path=str(path))
                .on_conflict_do_nothing(constraint="uq_import_jobs_user_file")
                .returning(ImportJob.id)
            )
        ).scalar_one_or_none()
        await session.commit()
    except BaseException:
        await _remove(path)
        raise

    if new_id is None:  # same file uploaded before
        await _remove(path)
        existing = (
            await session.execute(
                select(ImportJob).where(ImportJob.user_id == user_id, ImportJob.file_hash == file_hash)
            )
        ).scalar_one()
        return existing, True
    job = await session.get(ImportJob, job_id)
    assert job is not None
    return job, False


async def _remove(path: Path | str) -> None:
    try:
        await aiofiles.os.remove(path)
    except FileNotFoundError:
        pass


# ---------------- row validation ----------------
class RowError(ValueError):
    pass


def parse_row(
    row: dict[str, str | None],
    categories: dict[str, int],
    currencies: set[str],
    min_date: date | None,
) -> dict[str, Any]:
    raw_date = (row.get("date") or "").strip()
    try:
        spent_on = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise RowError(f"invalid date '{raw_date}' (use YYYY-MM-DD)") from exc
    if min_date is None or spent_on < min_date:
        raise RowError(f"no exchange rate for {spent_on}")

    raw_amount = (row.get("amount") or "").strip()
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation as exc:
        raise RowError(f"invalid amount '{raw_amount}'") from exc
    if not amount.is_finite() or amount <= 0:
        raise RowError("amount must be greater than 0")
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise RowError("amount can have at most 2 decimal places")
    if amount >= Decimal("1e12"):
        raise RowError("amount is too large")

    currency = (row.get("currency") or "").strip().upper()
    if currency not in currencies:
        raise RowError(f"unsupported currency '{currency}'")

    category_name = (row.get("category") or "").strip()
    category_id = categories.get(category_name.lower())
    if category_id is None:
        raise RowError(f"unknown category '{category_name}'")

    description = (row.get("description") or "").strip()
    if len(description) > 500:
        raise RowError("description is longer than 500 characters")
    return {
        "category_id": category_id,
        "amount": amount,
        "currency": currency,
        "description": description,
        "spent_on": spent_on,
    }


# ---------------- worker side ----------------
async def claim_next_job(session: AsyncSession) -> ImportJob | None:
    job = (
        await session.execute(
            select(ImportJob)
            .where(ImportJob.status == "pending")
            .order_by(ImportJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if job is None:
        await session.rollback()
        return None
    now = datetime.now(UTC)
    job.status, job.started_at, job.heartbeat_at = "processing", now, now
    job.rows_total = job.rows_done = job.rows_failed = 0
    job.errors = []
    await session.commit()
    return job


async def requeue_stale_jobs(session: AsyncSession, stale_after_seconds: int = 600) -> int:
    """A job whose worker died stops sending heartbeats -> put it back to pending.
    Safe to re-run because of the per-row import_key."""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
    result = await session.execute(
        update(ImportJob)
        .where(ImportJob.status == "processing", ImportJob.heartbeat_at < cutoff)
        .values(status="pending")
    )
    await session.commit()
    return result.rowcount  # type: ignore[attr-defined,no-any-return]


def _open_reader(path: str) -> tuple[Any, Any]:
    fh = open(path, newline="", encoding="utf-8-sig")  # noqa: SIM115 - closed in process_job
    return fh, csv.DictReader(fh)


def _read_batch(reader: Any, size: int) -> list[dict[str, str | None]]:
    return list(itertools.islice(reader, size))


async def process_job(session: AsyncSession, job: ImportJob) -> ImportJob:
    s = get_settings()
    user = await session.get(User, job.user_id)
    cats = (await session.execute(select(Category.id, Category.name).where(Category.user_id == job.user_id))).all()
    categories = {name.lower(): cid for cid, name in cats}
    budget_ids = set(
        (
            await session.execute(
                select(Category.id).where(Category.user_id == job.user_id, Category.monthly_budget.is_not(None))
            )
        ).scalars()
    )
    min_date = await rates_start_date(session)
    await session.commit()
    currencies = set(s.supported_currencies)

    try:
        fh, reader = await asyncio.to_thread(_open_reader, job.file_path)
    except FileNotFoundError:
        return await _finish(session, job, "failed", [{"row": 0, "reason": "uploaded file is missing"}])

    total = done = failed = 0
    errors: list[dict[str, Any]] = []
    try:
        header = await asyncio.to_thread(lambda: reader.fieldnames)
        missing = [c for c in REQUIRED_COLUMNS if c not in [h.strip().lower() for h in header or []]]
        if missing or user is None:
            return await _finish(
                session, job, "failed", [{"row": 1, "reason": f"missing columns: {', '.join(missing)}"}]
            )
        reader.fieldnames = [h.strip().lower() for h in header]

        line = 1  # header is line 1; the first data row is line 2
        while batch := await asyncio.to_thread(_read_batch, reader, s.import_batch_size):
            good: list[dict[str, Any]] = []
            touched: set[tuple[int, date]] = set()
            for row in batch:
                line += 1
                total += 1
                if total > s.import_max_rows:
                    failed += 1
                    if len(errors) < MAX_ERRORS_STORED:
                        errors.append({"row": line, "reason": f"file has more than {s.import_max_rows} rows"})
                    continue
                try:
                    values = parse_row(row, categories, currencies, min_date)
                except RowError as exc:
                    failed += 1
                    if len(errors) < MAX_ERRORS_STORED:
                        errors.append({"row": line, "reason": str(exc)})
                    continue
                values["user_id"] = job.user_id
                values["import_key"] = f"{job.file_hash}:{line}"
                good.append(values)
                if values["category_id"] in budget_ids:
                    touched.add((values["category_id"], values["spent_on"].replace(day=1)))

            if good:
                await session.execute(
                    insert(Expense)
                    .values(good)
                    .on_conflict_do_nothing(
                        index_elements=["user_id", "import_key"], index_where=text("import_key IS NOT NULL")
                    )
                )
            for category_id, period in touched:
                add_budget_check_event(session, job.user_id, category_id, period)
            done += len(good)
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job.id)
                .values(
                    rows_total=total,
                    rows_done=done,
                    rows_failed=failed,
                    errors=errors,
                    heartbeat_at=func.now(),
                )
            )
            await session.commit()  # one transaction per batch: progress is visible, memory stays flat
    finally:
        await asyncio.to_thread(fh.close)

    return await _finish(session, job, "completed", errors, total, done, failed)


async def _finish(
    session: AsyncSession,
    job: ImportJob,
    status: str,
    errors: list[dict[str, Any]],
    total: int = 0,
    done: int = 0,
    failed: int = 0,
) -> ImportJob:
    await session.execute(
        update(ImportJob)
        .where(ImportJob.id == job.id)
        .values(
            status=status,
            rows_total=total,
            rows_done=done,
            rows_failed=failed,
            errors=errors,
            finished_at=func.now(),
        )
    )
    await session.commit()
    await session.refresh(job)
    await _remove(job.file_path)  # final state reached: the temp file is no longer needed
    return job


async def process_next_import(session: AsyncSession) -> bool:
    job = await claim_next_job(session)
    if job is None:
        return False
    try:
        await process_job(session, job)
    except Exception:  # pragma: no cover - logged; stale-job requeue will retry
        logger.exception("import %s crashed", job.id)
        await session.rollback()
    return True
