"""Months router — summary, close, reopen, and settlement queries."""
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.core import User
from app.models.settlement import Month, Settlement
from app.schemas.core import UserMini
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

    # Batch-load the unique payer users for all settlements (paid_marked_by is always
    # None at close time, so only from_user / to_user need resolving here).
    all_user_ids = {s.from_user for s in settlements} | {s.to_user for s in settlements}
    u_result = await db.execute(select(User).where(User.id.in_(all_user_ids)))
    users_by_id = {u.id: u for u in u_result.scalars().all()}

    return MonthCloseResponse(
        month_id=month.id,
        status=month.status,
        closed_at=month.closed_at,
        closed_by=UserMini(id=current_user.id, name=current_user.name),
        settlements=[
            SettlementResponse(
                id=s.id,
                month_id=s.month_id,
                from_user=UserMini(id=users_by_id[s.from_user].id, name=users_by_id[s.from_user].name),
                to_user=UserMini(id=users_by_id[s.to_user].id, name=users_by_id[s.to_user].name),
                amount=s.amount,
                paid=s.paid,
                paid_at=s.paid_at,
                paid_marked_by=None,
                created_at=s.created_at,
            )
            for s in settlements
        ],
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
        .options(
            selectinload(Settlement.from_user_obj),
            selectinload(Settlement.to_user_obj),
            selectinload(Settlement.paid_marked_by_user),
        )
        .order_by(Settlement.id)
    )
    return [
        SettlementResponse(
            id=s.id,
            month_id=s.month_id,
            from_user=UserMini(id=s.from_user_obj.id, name=s.from_user_obj.name),
            to_user=UserMini(id=s.to_user_obj.id, name=s.to_user_obj.name),
            amount=s.amount,
            paid=s.paid,
            paid_at=s.paid_at,
            paid_marked_by=UserMini(id=s.paid_marked_by_user.id, name=s.paid_marked_by_user.name) if s.paid_marked_by_user else None,
            created_at=s.created_at,
        )
        for s in result.scalars().all()
    ]
