import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.deps import CurrentUser, SessionDep
from app.errors import BadRequest, NotFound
from app.models import ImportJob
from app.schemas import (
    Currency,
    DownloadUrlOut,
    ExpenseCreate,
    ExpenseOut,
    ExpensePage,
    ExpenseUpdate,
    ImportAccepted,
    ImportJobOut,
    ReceiptConfirmIn,
    UploadUrlIn,
    UploadUrlOut,
)
from app.services import expenses as svc
from app.services import idempotency, imports, receipts

router = APIRouter(tags=["expenses"])

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]
IfMatchHeader = Annotated[str | None, Header(alias="If-Match")]


def _with_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = svc.etag_for(version)


@router.post("/expenses", response_model=ExpenseOut, status_code=status.HTTP_201_CREATED)
async def create_expense(
    body: ExpenseCreate,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> ExpenseOut | JSONResponse:
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            raise BadRequest("Idempotency-Key must not be empty", code="invalid_idempotency_key")
        fingerprint = idempotency.request_fingerprint(body.model_dump(mode="json"))
        # May block here until a parallel request with the same key finishes - see services/idempotency.py
        if not await idempotency.claim_key(session, user.id, idempotency_key, fingerprint):
            status_code, payload = await idempotency.stored_response(session, user.id, idempotency_key, fingerprint)
            await session.rollback()
            headers = {"Idempotency-Replayed": "true"}
            if isinstance(payload, dict) and "version" in payload:
                headers["ETag"] = svc.etag_for(payload["version"])
                headers["Location"] = f"/api/v1/expenses/{payload['id']}"
            return JSONResponse(payload, status_code=status_code, headers=headers)

    expense = await svc.create_expense(session, user.id, body)
    out = ExpenseOut.model_validate(expense)
    if idempotency_key is not None:
        await idempotency.save_response(session, user.id, idempotency_key, 201, out.model_dump(mode="json"))
    await session.commit()  # expense + outbox event + idempotency record: one transaction
    _with_etag(response, expense.version)
    response.headers["Location"] = f"/api/v1/expenses/{expense.id}"
    return out


@router.get("/expenses", response_model=ExpensePage)
async def list_expenses(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    category_id: Annotated[int | None, Query(gt=0)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_amount: Annotated[Decimal | None, Query(ge=0, max_digits=14, decimal_places=2)] = None,
    max_amount: Annotated[Decimal | None, Query(ge=0, max_digits=14, decimal_places=2)] = None,
    currency: Annotated[Currency | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> ExpensePage:
    items, next_cursor = await svc.list_expenses(
        session,
        user.id,
        limit=limit,
        cursor=cursor,
        category_id=category_id,
        date_from=date_from,
        date_to=date_to,
        min_amount=min_amount,
        max_amount=max_amount,
        currency=currency,
        q=q,
    )
    return ExpensePage(items=[ExpenseOut.model_validate(e) for e in items], next_cursor=next_cursor)


@router.get("/expenses/{expense_id}", response_model=ExpenseOut)
async def get_expense(expense_id: int, user: CurrentUser, session: SessionDep, response: Response) -> ExpenseOut:
    expense = await svc.get_owned_expense(session, user.id, expense_id)
    _with_etag(response, expense.version)
    return ExpenseOut.model_validate(expense)


@router.patch("/expenses/{expense_id}", response_model=ExpenseOut)
async def update_expense(
    expense_id: int,
    body: ExpenseUpdate,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    if_match: IfMatchHeader = None,
) -> ExpenseOut:
    expected = svc.parse_if_match(if_match)
    expense = await svc.update_expense(session, user.id, expense_id, expected, body)
    _with_etag(response, expense.version)
    return ExpenseOut.model_validate(expense)


@router.delete("/expenses/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(expense_id: int, user: CurrentUser, session: SessionDep) -> Response:
    key = await svc.delete_expense(session, user.id, expense_id)
    if key:
        await receipts.try_delete_now(session, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------- receipts ----------------
@router.post("/expenses/{expense_id}/receipt/upload-url", response_model=UploadUrlOut)
async def receipt_upload_url(
    expense_id: int, body: UploadUrlIn, user: CurrentUser, session: SessionDep
) -> UploadUrlOut:
    return UploadUrlOut(**await receipts.create_upload_url(session, user.id, expense_id, body.content_type))


@router.post("/expenses/{expense_id}/receipt/confirm", response_model=ExpenseOut)
async def receipt_confirm(
    expense_id: int, body: ReceiptConfirmIn, user: CurrentUser, session: SessionDep, response: Response
) -> ExpenseOut:
    expense = await receipts.confirm_upload(session, user.id, expense_id, body.key)
    _with_etag(response, expense.version)
    return ExpenseOut.model_validate(expense)


@router.get("/expenses/{expense_id}/receipt", response_model=DownloadUrlOut)
async def receipt_download(expense_id: int, user: CurrentUser, session: SessionDep) -> DownloadUrlOut:
    return DownloadUrlOut(**await receipts.download_url(session, user.id, expense_id))


# ---------------- CSV import ----------------
@router.post("/expenses/import", response_model=ImportAccepted, status_code=status.HTTP_202_ACCEPTED)
async def import_expenses(
    user: CurrentUser, session: SessionDep, file: Annotated[UploadFile, File()]
) -> ImportAccepted:
    job, duplicate = await imports.accept_upload(session, user.id, file)
    return ImportAccepted(job_id=job.id, status=job.status, duplicate_of_previous_upload=duplicate)


@router.get("/imports/{job_id}", response_model=ImportJobOut)
async def import_status(job_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ImportJobOut:
    job = (
        await session.execute(select(ImportJob).where(ImportJob.id == job_id, ImportJob.user_id == user.id))
    ).scalar_one_or_none()
    if job is None:
        raise NotFound("Import job not found")
    return ImportJobOut.model_validate(job)
