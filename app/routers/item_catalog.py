"""Item catalog router — autocomplete suggestions (read-only)."""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import User
from app.models.expenses import ItemCatalog
from app.schemas.expenses import ItemCatalogResponse
from app.utils.permissions import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


@router.get("", response_model=list[ItemCatalogResponse])
async def search_catalog(
    q: str | None = Query(None, max_length=200),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ItemCatalogResponse]:
    stmt = (
        select(ItemCatalog)
        .where(ItemCatalog.household_id == current_user.household_id)
        .order_by(
            ItemCatalog.use_count.desc(),
            ItemCatalog.last_used_at.desc(),
            ItemCatalog.name.asc(),
        )
        .limit(limit)
    )

    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(ItemCatalog.name).like(pattern))

    result = await db.execute(stmt)
    return [ItemCatalogResponse.model_validate(row) for row in result.scalars().all()]
