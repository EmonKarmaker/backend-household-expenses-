"""Months router — summary, close, reopen, and settlement queries."""
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import User
from app.models.settlement import Month, Settlement
from app.schemas.settlement import MonthCloseResponse, MonthReopenResponse, SettlementResponse
from app.services.calculation import MonthSummary, calculate_month
from app.services.month_close import (
    MonthAlreadyClosed,
    MonthAlreadyOpen,
    MonthNotFound,
    close_month,
    reopen_month,
)
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()


@router.get("/{month_id}/summary", response_model=MonthSummary)
async def get_month_summary(
    month_id: str = Path(..., pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MonthSummary:
    return await calculate_month(month_id, current_user.household_id, db)


@router.post("/{month_id}/close", response_model=MonthCloseResponse, status_code=status.HTTP_200_OK)
async def close_month_endpoint(
    month_id: str = Path(..., pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MonthCloseResponse:
    try:
        month, settlements = await close_month(
            month_id, current_user.household_id, current_user.id, db
        )
    except MonthAlreadyClosed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Month is already closed")

    return MonthCloseResponse(
        month_id=month.id,
        status=month.status,
        closed_at=month.closed_at,
        closed_by=month.closed_by,
        settlements=[SettlementResponse.model_validate(s) for s in settlements],
    )


@router.post("/{month_id}/reopen", response_model=MonthReopenResponse, status_code=status.HTTP_200_OK)
async def reopen_month_endpoint(
    month_id: str = Path(..., pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MonthReopenResponse:
    try:
        month = await reopen_month(
            month_id, current_user.household_id, current_user.id, db
        )
    except MonthNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Month not found")
    except MonthAlreadyOpen:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Month is already open")

    return MonthReopenResponse(month_id=month.id, status=month.status)


@router.get("/{month_id}/settlements", response_model=list[SettlementResponse])
async def get_settlements(
    month_id: str = Path(..., pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SettlementResponse]:
    result = await db.execute(
        select(Settlement)
        .join(Month, Settlement.month_id == Month.id)
        .where(
            Settlement.month_id == month_id,
            Month.household_id == current_user.household_id,
        )
        .order_by(Settlement.id)
    )
    settlements = result.scalars().all()
    return [SettlementResponse.model_validate(s) for s in settlements]
