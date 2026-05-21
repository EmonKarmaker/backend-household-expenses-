"""Security deposit business logic — create and query.

Refund/deduction processing is handled in the leaving workflow (Task 22).
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.core import User
from app.models.deposits import SecurityDeposit
from app.utils.audit import log_audit, model_to_dict


# ---------------------------------------------------------------------------
# Domain errors — routers convert these to HTTP responses
# ---------------------------------------------------------------------------

class DepositUserNotFound(Exception):
    pass


class DepositUserInactive(Exception):
    pass


class DepositAlreadyExists(Exception):
    pass


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

_DEPOSIT_USER_LOADS = [
    selectinload(SecurityDeposit.user),
    selectinload(SecurityDeposit.held_by_user),
]


async def list_deposits_svc(household_id: int, db: AsyncSession) -> list[SecurityDeposit]:
    result = await db.execute(
        select(SecurityDeposit)
        .join(User, SecurityDeposit.user_id == User.id)
        .where(User.household_id == household_id)
        .options(*_DEPOSIT_USER_LOADS)
        .order_by(SecurityDeposit.id.desc())
    )
    return list(result.scalars().all())


async def get_user_deposits_svc(
    target_user_id: int,
    household_id: int,
    db: AsyncSession,
) -> list[SecurityDeposit]:
    result = await db.execute(
        select(User).where(User.id == target_user_id, User.household_id == household_id)
    )
    if result.scalar_one_or_none() is None:
        raise DepositUserNotFound(f"User {target_user_id} not found in household")

    result = await db.execute(
        select(SecurityDeposit)
        .where(SecurityDeposit.user_id == target_user_id)
        .options(*_DEPOSIT_USER_LOADS)
        .order_by(SecurityDeposit.id.desc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create_deposit_svc(
    *,
    household_id: int,
    user_id: int,
    amount: Decimal,
    deposited_at: date,
    held_by_user_id: int,
    db: AsyncSession,
) -> SecurityDeposit:
    result = await db.execute(
        select(User).where(User.id == user_id, User.household_id == household_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise DepositUserNotFound(f"User {user_id} not found in household")
    if user.left_at is not None:
        raise DepositUserInactive(f"User {user_id} has already left the household")

    result = await db.execute(
        select(SecurityDeposit).where(
            SecurityDeposit.user_id == user_id,
            SecurityDeposit.status == "held",
        )
    )
    if result.scalar_one_or_none() is not None:
        raise DepositAlreadyExists(
            f"User {user_id} already has an active deposit; refund it first"
        )

    deposit = SecurityDeposit(
        user_id=user_id,
        amount=amount,
        deposited_at=deposited_at,
        held_by_user_id=held_by_user_id,
        status="held",
    )
    db.add(deposit)
    await db.flush()
    await db.refresh(deposit)

    await log_audit(
        db, action="create", entity="SecurityDeposit",
        entity_id=deposit.id, user_id=held_by_user_id,
        after=model_to_dict(deposit),
    )

    return deposit
