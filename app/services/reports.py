"""Spend calculations in the user's base currency - all done in SQL.

Conversion rule: amount_in_base = amount * rate_to_usd(expense currency) / rate_to_usd(base),
using for each currency the latest rate with valid_on <= spent_on ("no rate that day ->
use the latest rate before it"). The lookup is a LATERAL subquery with ORDER BY valid_on
DESC LIMIT 1, which is a single backward index probe on the exchange_rates primary key
(currency, valid_on).

Rounding: per-expense values are NOT rounded; only final totals are rounded to 2 places,
so rounding errors do not add up.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import Unprocessable

# Reusable CTE body: every expense of :user_id between :from_date (incl) and :to_date (excl)
# converted to :base. Used by the monthly report, the category list and the budget worker.
CONVERTED_EXPENSES_SQL = """
    SELECT e.category_id,
           date_trunc('month', e.spent_on)::date AS month_start,
           CASE WHEN e.currency = :base THEN e.amount
                ELSE e.amount * fx.rate_to_usd / bx.rate_to_usd
           END AS amount_base
    FROM expenses e
    LEFT JOIN LATERAL (
        SELECT r.rate_to_usd FROM exchange_rates r
        WHERE r.currency = e.currency AND r.valid_on <= e.spent_on
        ORDER BY r.valid_on DESC LIMIT 1
    ) fx ON TRUE
    LEFT JOIN LATERAL (
        SELECT r.rate_to_usd FROM exchange_rates r
        WHERE r.currency = :base AND r.valid_on <= e.spent_on
        ORDER BY r.valid_on DESC LIMIT 1
    ) bx ON TRUE
    WHERE e.user_id = :user_id
      AND e.spent_on >= :from_date AND e.spent_on < :to_date
"""

MONTHLY_REPORT_SQL = f"""
WITH months AS (
    SELECT CAST(:prev_start AS date) AS month_start
    UNION ALL
    SELECT CAST(:cur_start AS date)
),
converted AS ({CONVERTED_EXPENSES_SQL}),
grid AS (
    -- every category x both months, so a month with no spend still gives 0 (and LAG works)
    SELECT c.id AS category_id, c.name, c.monthly_budget, m.month_start,
           COALESCE(SUM(cv.amount_base), 0) AS total
    FROM categories c
    CROSS JOIN months m
    LEFT JOIN converted cv ON cv.category_id = c.id AND cv.month_start = m.month_start
    WHERE c.user_id = :user_id
    GROUP BY c.id, c.name, c.monthly_budget, m.month_start
),
with_prev AS (
    SELECT g.*,
           LAG(g.total, 1, 0) OVER (PARTITION BY g.category_id ORDER BY g.month_start) AS previous_total
    FROM grid g
)
SELECT category_id,
       name AS category_name,
       monthly_budget AS budget,
       ROUND(total, 2) AS total,
       ROUND(previous_total, 2) AS previous_total,
       ROUND(total - previous_total, 2) AS change,
       CASE WHEN previous_total > 0
            THEN ROUND((total - previous_total) * 100 / previous_total, 2) END AS change_percent,
       CASE WHEN monthly_budget > 0
            THEN ROUND(total * 100 / monthly_budget, 2) END AS percent_used,
       ROUND(SUM(total) OVER (), 2) AS grand_total
FROM with_prev
WHERE month_start = :cur_start
ORDER BY total DESC, name
"""

CATEGORY_SPEND_SQL = f"""
WITH converted AS ({CONVERTED_EXPENSES_SQL})
SELECT c.id, c.name, c.monthly_budget,
       ROUND(COALESCE(SUM(cv.amount_base), 0), 2) AS spent_this_month,
       CASE WHEN c.monthly_budget > 0
            THEN ROUND(COALESCE(SUM(cv.amount_base), 0) * 100 / c.monthly_budget, 2) END AS percent_used
FROM categories c
LEFT JOIN converted cv ON cv.category_id = c.id
WHERE c.user_id = :user_id {{category_filter}}
GROUP BY c.id, c.name, c.monthly_budget
ORDER BY c.name
"""


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start, end


def previous_month_start(start: date) -> date:
    return date(start.year - (start.month == 1), (start.month - 2) % 12 + 1, 1)


async def monthly_report(
    session: AsyncSession, user_id: int, base: str, year: int, month: int
) -> tuple[Decimal, list[dict[str, Any]]]:
    cur_start, next_start = month_bounds(year, month)
    prev_start = previous_month_start(cur_start)
    result = await session.execute(
        text(MONTHLY_REPORT_SQL),
        {
            "user_id": user_id,
            "base": base,
            "prev_start": prev_start,
            "cur_start": cur_start,
            "from_date": prev_start,
            "to_date": next_start,
        },
    )
    rows = [dict(r) for r in result.mappings()]
    grand_total = rows[0].pop("grand_total") if rows else Decimal("0.00")
    for r in rows[1:]:
        r.pop("grand_total")
    return grand_total, rows


async def category_spend(
    session: AsyncSession, user_id: int, base: str, month_start: date, category_id: int | None = None
) -> list[dict[str, Any]]:
    _, next_start = month_bounds(month_start.year, month_start.month)
    params: dict[str, Any] = {"user_id": user_id, "base": base, "from_date": month_start, "to_date": next_start}
    category_filter = ""
    if category_id is not None:
        category_filter = "AND c.id = :category_id"
        params["category_id"] = category_id
    sql = CATEGORY_SPEND_SQL.replace("{category_filter}", category_filter)
    result = await session.execute(text(sql), params)
    return [dict(r) for r in result.mappings()]


async def rates_start_date(session: AsyncSession) -> date | None:
    """First day on which EVERY currency has a rate. Before it, some conversion is impossible."""
    currencies = get_settings().supported_currencies
    result = await session.execute(
        text(
            "SELECT CASE WHEN COUNT(*) = :n THEN MAX(first_day) END FROM ("
            "  SELECT currency, MIN(valid_on) AS first_day FROM exchange_rates"
            "  WHERE currency = ANY(:currencies) GROUP BY currency) t"
        ),
        {"currencies": currencies, "n": len(currencies)},
    )
    return result.scalar_one_or_none()


async def ensure_rates_cover(session: AsyncSession, spent_on: date) -> None:
    start = await rates_start_date(session)
    if start is None or spent_on < start:
        raise Unprocessable(
            "No exchange rate is available for this date",
            code="no_exchange_rate",
            details=[{"loc": ["spent_on"], "msg": f"earliest supported date is {start}"}],
        )
