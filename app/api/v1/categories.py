from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy.exc import IntegrityError

from app.deps import CurrentUser, SessionDep
from app.errors import Conflict
from app.models import Category
from app.schemas import CategoryCreate, CategoryOut
from app.services.reports import category_spend

router = APIRouter(prefix="/categories", tags=["categories"])


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
async def create_category(body: CategoryCreate, user: CurrentUser, session: SessionDep) -> CategoryOut:
    category = Category(user_id=user.id, name=body.name, monthly_budget=body.monthly_budget)
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:  # unique (user_id, lower(name))
        await session.rollback()
        raise Conflict("A category with this name already exists", code="category_exists") from exc
    return CategoryOut(id=category.id, name=category.name, monthly_budget=category.monthly_budget)


@router.get("", response_model=list[CategoryOut])
async def list_categories(user: CurrentUser, session: SessionDep) -> list[CategoryOut]:
    """All my categories with this month's spend (in base currency) vs budget - one SQL query."""
    month_start = datetime.now(UTC).date().replace(day=1)
    rows = await category_spend(session, user.id, user.base_currency, month_start)
    return [
        CategoryOut(
            id=r["id"],
            name=r["name"],
            monthly_budget=r["monthly_budget"],
            spent_this_month=r["spent_this_month"],
            percent_used=r["percent_used"],
        )
        for r in rows
    ]
