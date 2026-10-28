"""Receipt upload flow.

1. upload-url : API makes a key receipts/{user_id}/{expense_id}/{uuid}.{ext} and a
                pre-signed POST that only accepts that key, that Content-Type and 1..5 MB.
2. client     : uploads straight to S3 (the API never touches the bytes).
3. confirm    : API checks the object exists, its size, and its REAL type from the first
                bytes (magic numbers) - a .exe renamed to .png is rejected and deleted.
                Only then is receipt_key saved on the expense.
"""

import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import get_settings
from app.errors import NotFound, Unprocessable
from app.models import Expense, S3Deletion
from app.services.expenses import get_owned_expense

logger = logging.getLogger(__name__)

EXT_BY_CONTENT_TYPE = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}


def sniff_type(head: bytes) -> str | None:
    """Return the file extension from the file's magic bytes, or None if not allowed."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"%PDF-"):
        return "pdf"
    return None


def _key_pattern(user_id: int, expense_id: int) -> re.Pattern[str]:
    return re.compile(
        rf"^receipts/{user_id}/{expense_id}/[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}\.(jpg|png|pdf)$"
    )


async def create_upload_url(session: AsyncSession, user_id: int, expense_id: int, content_type: str) -> dict:
    await get_owned_expense(session, user_id, expense_id)
    s = get_settings()
    key = f"receipts/{user_id}/{expense_id}/{uuid.uuid4()}.{EXT_BY_CONTENT_TYPE[content_type]}"
    post = await storage.presigned_upload(key, content_type, s.receipt_max_bytes, s.upload_url_ttl_seconds)
    return {
        "url": post["url"],
        "fields": post["fields"],
        "key": key,
        "expires_in": s.upload_url_ttl_seconds,
        "max_bytes": s.receipt_max_bytes,
    }


async def confirm_upload(session: AsyncSession, user_id: int, expense_id: int, key: str) -> Expense:
    await get_owned_expense(session, user_id, expense_id)
    match = _key_pattern(user_id, expense_id).match(key)
    if not match:
        # Prevents attaching another user's (or another expense's) file.
        raise Unprocessable("Key does not belong to this expense", code="invalid_receipt_key")

    size = await storage.object_size(key)
    if size is None:
        raise Unprocessable("File was not uploaded yet", code="receipt_not_found")
    s = get_settings()
    if size > s.receipt_max_bytes or size == 0:
        await storage.delete_object(key)
        raise Unprocessable("File must be between 1 byte and 5 MB", code="receipt_too_large")

    real_ext = sniff_type(await storage.read_first_bytes(key, 16))
    if real_ext is None or real_ext != match.group(1):
        await storage.delete_object(key)
        raise Unprocessable(
            "File content is not a valid JPG, PNG or PDF (or does not match its extension)",
            code="invalid_file_type",
        )

    # Lock the row so a parallel confirm cannot lose the old key we must clean up.
    old_key = (
        await session.execute(
            select(Expense.receipt_key).where(Expense.id == expense_id, Expense.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    expense = (
        await session.execute(
            update(Expense)
            .where(Expense.id == expense_id, Expense.user_id == user_id)
            .values(receipt_key=key, version=Expense.version + 1, updated_at=func.now())
            .returning(Expense)
        )
    ).scalar_one_or_none()
    if expense is None:  # deleted between the checks above and now
        await session.rollback()
        await storage.delete_object(key)
        raise NotFound("Expense not found")
    if old_key and old_key != key:
        session.add(S3Deletion(key=old_key, next_attempt_at=datetime.now(UTC)))
    await session.commit()
    return expense


async def download_url(session: AsyncSession, user_id: int, expense_id: int) -> dict:
    expense = await get_owned_expense(session, user_id, expense_id)
    if not expense.receipt_key:
        raise NotFound("This expense has no receipt", code="receipt_not_found")
    ttl = get_settings().download_url_ttl_seconds
    return {"url": await storage.presigned_download(expense.receipt_key, ttl), "expires_in": ttl}


async def try_delete_now(session: AsyncSession, key: str) -> bool:
    """Best-effort immediate delete after an expense is removed.

    The s3_deletions row was committed with the expense delete. On success we remove it;
    on failure we leave it and the worker retries with backoff.
    """
    try:
        await storage.delete_object(key)
    except Exception:  # noqa: BLE001 - any S3 failure means "retry later"
        logger.warning("S3 delete failed for %s, worker will retry", key)
        return False
    await session.execute(delete(S3Deletion).where(S3Deletion.key == key))
    await session.commit()
    return True
