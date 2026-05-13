"""Months router — monthly expense summary."""
from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import User
from app.services.calculation import MonthSummary, calculate_month
from app.utils.permissions import get_current_user

router = APIRouter()


@router.get("/{month_id}/summary", response_model=MonthSummary)
async def get_month_summary(
    month_id: str = Path(..., pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MonthSummary:
    return await calculate_month(month_id, current_user.household_id, db)
