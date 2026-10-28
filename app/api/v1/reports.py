from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import CurrentUser, SessionDep
from app.models import Notification
from app.schemas import MonthlyReport, NotificationOut, ReportRow
from app.services.reports import monthly_report

router = APIRouter(tags=["reports"])


@router.get("/reports/monthly", response_model=MonthlyReport)
async def get_monthly_report(
    user: CurrentUser,
    session: SessionDep,
    year: Annotated[int, Query(ge=2000, le=2100)],
    month: Annotated[int, Query(ge=1, le=12)],
) -> MonthlyReport:
    total, rows = await monthly_report(session, user.id, user.base_currency, year, month)
    return MonthlyReport(
        year=year,
        month=month,
        base_currency=user.base_currency,
        total=total,
        categories=[ReportRow(**r) for r in rows],
    )


@router.get("/notifications", response_model=list[NotificationOut], tags=["notifications"])
async def list_notifications(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(gt=0)] = None,
) -> list[NotificationOut]:
    """Newest first. Pass the last id you got as before_id to load older ones."""
    stmt = select(Notification).where(Notification.user_id == user.id)
    if before_id is not None:
        stmt = stmt.where(Notification.id < before_id)
    rows = (await session.execute(stmt.order_by(Notification.id.desc()).limit(limit))).scalars().all()
    return [NotificationOut.model_validate(n) for n in rows]
