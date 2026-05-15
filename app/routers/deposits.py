"""Deposits router — view and record security deposits."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import User
from app.schemas.deposits import (
    SecurityDepositCreate,
    SecurityDepositResponseFull,
    SecurityDepositResponsePublic,
)
from app.services.deposit_svc import (
    DepositAlreadyExists,
    DepositUserInactive,
    DepositUserNotFound,
    create_deposit_svc,
    get_user_deposits_svc,
    list_deposits_svc,
)
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()


def _format(deposits, is_admin: bool) -> list:
    if is_admin:
        return [SecurityDepositResponseFull.model_validate(d) for d in deposits]
    return [SecurityDepositResponsePublic.model_validate(d) for d in deposits]


@router.get("")
async def list_deposits(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deposits = await list_deposits_svc(current_user.household_id, db)
    return _format(deposits, current_user.is_admin)


@router.get("/{target_user_id}")
async def get_user_deposits(
    target_user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        deposits = await get_user_deposits_svc(target_user_id, current_user.household_id, db)
    except DepositUserNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in household",
        )
    return _format(deposits, current_user.is_admin)


@router.post("", response_model=SecurityDepositResponseFull, status_code=status.HTTP_201_CREATED)
async def create_deposit(
    body: SecurityDepositCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SecurityDepositResponseFull:
    try:
        deposit = await create_deposit_svc(
            household_id=current_user.household_id,
            user_id=body.user_id,
            amount=body.amount,
            deposited_at=body.deposited_at,
            held_by_user_id=current_user.id,
            db=db,
        )
    except DepositUserNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in household",
        )
    except DepositUserInactive:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="User has already left the household",
        )
    except DepositAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already has an active deposit; refund it first",
        )
    return SecurityDepositResponseFull.model_validate(deposit)
