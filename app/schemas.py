"""Request/response schemas (Pydantic v2).

Money is always Decimal. In JSON responses Pydantic v2 serialises Decimal as a string
("12.50"), so clients never get a lossy float either.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings


def normalize_currency(value: str) -> str:
    value = value.strip().upper()
    if value not in get_settings().supported_currencies:
        raise ValueError(f"Unsupported currency. Use one of: {', '.join(get_settings().supported_currencies)}")
    return value


Currency = Annotated[str, AfterValidator(normalize_currency)]
# > 0, at most 2 decimal places, at most 12 digits before the point (matches NUMERIC(14,2)).
Money = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class InModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- auth ----------
class RegisterIn(InModel):
    email: str = Field(max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    base_currency: Currency

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class LoginIn(InModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=128)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class RefreshIn(InModel):
    refresh_token: str = Field(min_length=10, max_length=2048)


class UserOut(OutModel):
    id: int
    email: str
    base_currency: str
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


# ---------- categories ----------
class CategoryCreate(InModel):
    name: str = Field(min_length=1, max_length=100)
    monthly_budget: Money | None = None


class CategoryOut(OutModel):
    id: int
    name: str
    monthly_budget: Decimal | None
    spent_this_month: Decimal = Decimal("0.00")
    percent_used: Decimal | None = None


# ---------- expenses ----------
class ExpenseCreate(InModel):
    category_id: int = Field(gt=0)
    amount: Money
    currency: Currency
    description: str = Field(default="", max_length=500)
    spent_on: date


class ExpenseUpdate(InModel):
    category_id: int | None = Field(default=None, gt=0)
    amount: Money | None = None
    currency: Currency | None = None
    description: str | None = Field(default=None, max_length=500)
    spent_on: date | None = None


class ExpenseOut(OutModel):
    id: int
    category_id: int
    amount: Decimal
    currency: str
    description: str
    spent_on: date
    receipt_key: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ExpensePage(BaseModel):
    items: list[ExpenseOut]
    next_cursor: str | None


# ---------- reports ----------
class ReportRow(BaseModel):
    category_id: int
    category_name: str
    total: Decimal
    budget: Decimal | None
    percent_used: Decimal | None
    previous_total: Decimal
    change: Decimal
    change_percent: Decimal | None


class MonthlyReport(BaseModel):
    year: int
    month: int
    base_currency: str
    total: Decimal
    categories: list[ReportRow]


# ---------- notifications ----------
class NotificationOut(OutModel):
    id: int
    category_id: int
    period: date
    level: int
    message: str
    spent: Decimal
    budget: Decimal
    created_at: datetime


# ---------- imports ----------
class ImportAccepted(BaseModel):
    job_id: uuid.UUID
    status: str
    duplicate_of_previous_upload: bool = False


class ImportJobOut(OutModel):
    job_id: uuid.UUID = Field(validation_alias="id")
    status: str
    rows_total: int
    rows_done: int
    rows_failed: int
    errors: list[Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# ---------- receipts ----------
ReceiptContentType = Literal["image/jpeg", "image/png", "application/pdf"]


class UploadUrlIn(InModel):
    content_type: ReceiptContentType


class UploadUrlOut(BaseModel):
    url: str
    fields: dict[str, str]
    key: str
    expires_in: int
    max_bytes: int


class ReceiptConfirmIn(InModel):
    key: str = Field(min_length=1, max_length=512)


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int
