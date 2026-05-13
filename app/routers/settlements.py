"""Settlements router — mark transfers paid or unpaid."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import User
from app.schemas.settlement import MarkSettlementPaidRequest, SettlementResponse
from app.services.settlement_mark import MonthIsOpen, SettlementNotFound, mark_settlement_paid
from app.utils.permissions import require_admin

router = APIRouter()


@router.patch("/{settlement_id}", response_model=SettlementResponse)
async def mark_settlement(
    settlement_id: int,
    body: MarkSettlementPaidRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SettlementResponse:
    try:
        settlement = await mark_settlement_paid(
            settlement_id,
            current_user.household_id,
            body.paid,
            current_user.id,
            db,
        )
    except SettlementNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    except MonthIsOpen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot mark settlement paid in an open month",
        )

    return SettlementResponse.model_validate(settlement)
