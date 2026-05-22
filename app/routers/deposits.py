"""Deposits router — view and record security deposits."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import User
from app.schemas.core import UserMini
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
        return [
            SecurityDepositResponseFull(
                id=d.id,
                user=UserMini(id=d.user.id, name=d.user.name),
                amount=d.amount,
                deposited_at=d.deposited_at,
                held_by=UserMini(id=d.held_by_user.id, name=d.held_by_user.name),
                status=d.status,
                refunded_at=d.refunded_at,
                deduction_reason=d.deduction_reason,
                deduction_amount=d.deduction_amount,
                applied_to_dues=d.applied_to_dues,
                final_refund=d.final_refund,
                created_at=d.created_at,
                updated_at=d.updated_at,
            )
            for d in deposits
        ]
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
    target_user = await db.get(User, body.user_id)
    return SecurityDepositResponseFull(
        id=deposit.id,
        user=UserMini(id=target_user.id, name=target_user.name),
        amount=deposit.amount,
        deposited_at=deposit.deposited_at,
        held_by=UserMini(id=current_user.id, name=current_user.name),
        status=deposit.status,
        refunded_at=deposit.refunded_at,
        deduction_reason=deposit.deduction_reason,
        deduction_amount=deposit.deduction_amount,
        applied_to_dues=deposit.applied_to_dues,
        final_refund=deposit.final_refund,
        created_at=deposit.created_at,
        updated_at=deposit.updated_at,
    )
