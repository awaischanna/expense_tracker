"""seed exchange rates for the last 90 days

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28

Rates are realistic sample values with a small deterministic daily wobble (no network
call during a migration). rate_to_usd = how many USD one unit of the currency is worth.
In production a daily job would load real rates from a provider instead.
"""

import math
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BASE_RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.0850"),
    "AED": Decimal("0.2722940000"),  # pegged, so no wobble
    "PKR": Decimal("0.0035900000"),
}
DAYS = 90


def build_rows(today: date) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for offset in range(DAYS):
        day = today - timedelta(days=offset)
        for currency, base in BASE_RATES.items():
            if currency in ("USD", "AED"):
                rate = base
            else:
                wobble = Decimal(str(round(math.sin(offset / 7) * 0.004, 6)))
                rate = base * (Decimal(1) + wobble)
            rows.append(
                {
                    "currency": currency,
                    "valid_on": day,
                    "rate_to_usd": rate.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN),
                }
            )
    return rows


def upgrade() -> None:
    table = sa.table(
        "exchange_rates",
        sa.column("currency", sa.String),
        sa.column("valid_on", sa.Date),
        sa.column("rate_to_usd", sa.Numeric(20, 10)),
    )
    op.bulk_insert(table, build_rows(date.today()))


def downgrade() -> None:
    op.execute("DELETE FROM exchange_rates WHERE currency IN ('USD', 'EUR', 'AED', 'PKR')")
