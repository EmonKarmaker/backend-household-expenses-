"""Grocery Fund router — a standalone pooled-money ledger.

This feature is intentionally isolated from the settlement engine: deposits
and expenses recorded here never flow into monthly settlements. A fund simply
tracks money members put in and money spent out of the pool, and reports the
running balance plus per-member contributions.
"""
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.core import User
from app.models.expenses import ShoppingEntry
from app.models.funds import Fund, FundDeposit, FundExpense
from app.schemas.core import UserMini
from app.schemas.funds import (
    FundCreate,
    FundDepositCreate,
    FundDepositResponse,
    FundDetailResponse,
    FundExpenseCreate,
    FundExpenseResponse,
    FundResponse,
    FundUpdate,
    MemberContribution,
)
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()

_TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Balance computation — all math in Decimal, serialised to 2dp strings
# ---------------------------------------------------------------------------

def _money(amount: Decimal) -> Decimal:
    """Quantize to 2 decimal places so serialisation is always e.g. '500.00'."""
    return amount.quantize(_TWO_PLACES)


def _compute_totals(fund: Fund) -> tuple[Decimal, Decimal, Decimal]:
    """Return (total_deposits, total_expenses, balance) for a fund.

    balance = total_deposits - total_expenses
    """
    total_deposits = sum((d.amount for d in fund.deposits), Decimal("0"))
    total_expenses = sum((e.amount for e in fund.expenses), Decimal("0"))
    balance = total_deposits - total_expenses
    return total_deposits, total_expenses, balance


def _compute_contributions(fund: Fund) -> list[MemberContribution]:
    """Group deposits by depositor and sum amounts — one entry per distinct user."""
    totals: dict[int, Decimal] = {}
    users: dict[int, User] = {}
    for d in fund.deposits:
        totals[d.user_id] = totals.get(d.user_id, Decimal("0")) + d.amount
        users[d.user_id] = d.user
    return [
        MemberContribution(
            user=UserMini(id=uid, name=users[uid].name),
            total=_money(totals[uid]),
        )
        for uid in sorted(totals)
    ]


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

def _build_deposit_response(d: FundDeposit, user: User | None = None) -> FundDepositResponse:
    u = user or d.user
    return FundDepositResponse(
        id=d.id,
        fund_id=d.fund_id,
        user=UserMini(id=u.id, name=u.name),
        amount=d.amount,
        deposited_at=d.deposited_at,
        note=d.note,
        created_at=d.created_at,
    )


def _build_expense_response(e: FundExpense, creator: User | None = None) -> FundExpenseResponse:
    u = creator or e.created_by_user
    return FundExpenseResponse(
        id=e.id,
        fund_id=e.fund_id,
        amount=e.amount,
        description=e.description,
        spent_at=e.spent_at,
        shopping_entry_id=e.shopping_entry_id,
        created_by_user=UserMini(id=u.id, name=u.name),
        created_at=e.created_at,
    )


def _build_fund_response(fund: Fund) -> FundResponse:
    total_deposits, total_expenses, balance = _compute_totals(fund)
    return FundResponse(
        id=fund.id,
        household_id=fund.household_id,
        name=fund.name,
        description=fund.description,
        status=fund.status,
        created_by_user=UserMini(id=fund.created_by_user.id, name=fund.created_by_user.name),
        created_at=fund.created_at,
        updated_at=fund.updated_at,
        total_deposits=_money(total_deposits),
        total_expenses=_money(total_expenses),
        balance=_money(balance),
        contributions=_compute_contributions(fund),
    )


def _build_fund_detail_response(fund: Fund) -> FundDetailResponse:
    base = _build_fund_response(fund)
    return FundDetailResponse(
        **base.model_dump(),
        deposits=[
            _build_deposit_response(d)
            for d in sorted(fund.deposits, key=lambda d: (d.deposited_at, d.id))
        ],
        expenses=[
            _build_expense_response(e)
            for e in sorted(fund.expenses, key=lambda e: (e.spent_at, e.id))
        ],
    )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

_FUND_LOAD_OPTIONS = (
    selectinload(Fund.created_by_user),
    selectinload(Fund.deposits).selectinload(FundDeposit.user),
    selectinload(Fund.expenses).selectinload(FundExpense.created_by_user),
)


async def _get_fund_or_404(fund_id: int, household_id: int, db: AsyncSession) -> Fund:
    result = await db.execute(
        select(Fund)
        .where(Fund.id == fund_id, Fund.household_id == household_id)
        .options(*_FUND_LOAD_OPTIONS)
        # Authoritative read: always reflect the current persisted state,
        # refreshing eager collections even if the Fund is already in the
        # identity map (a no-op with the per-request sessions used in prod).
        .execution_options(populate_existing=True)
    )
    fund = result.scalar_one_or_none()
    if fund is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fund not found")
    return fund


# ---------------------------------------------------------------------------
# Fund endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=FundResponse, status_code=status.HTTP_201_CREATED)
async def create_fund(
    body: FundCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FundResponse:
    fund = Fund(
        household_id=current_user.household_id,
        name=body.name,
        description=body.description,
        status="active",
        created_by=current_user.id,
    )
    db.add(fund)
    await db.flush()
    fund = await _get_fund_or_404(fund.id, current_user.household_id, db)
    return _build_fund_response(fund)


@router.get("", response_model=list[FundResponse])
async def list_funds(
    status_filter: Literal["active", "archived"] | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FundResponse]:
    stmt = select(Fund).where(Fund.household_id == current_user.household_id)
    if status_filter is not None:
        stmt = stmt.where(Fund.status == status_filter)
    stmt = (
        stmt.order_by(Fund.created_at.desc(), Fund.id.desc())
        .options(*_FUND_LOAD_OPTIONS)
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return [_build_fund_response(f) for f in result.scalars().all()]


@router.get("/{fund_id}", response_model=FundDetailResponse)
async def get_fund(
    fund_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FundDetailResponse:
    fund = await _get_fund_or_404(fund_id, current_user.household_id, db)
    return _build_fund_detail_response(fund)


@router.patch("/{fund_id}", response_model=FundResponse)
async def update_fund(
    fund_id: int,
    body: FundUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FundResponse:
    fund = await _get_fund_or_404(fund_id, current_user.household_id, db)
    if body.name is not None:
        fund.name = body.name
    if body.description is not None:
        fund.description = body.description
    if body.status is not None:
        fund.status = body.status
    await db.flush()
    return _build_fund_response(fund)


# ---------------------------------------------------------------------------
# Deposit endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/{fund_id}/deposits",
    response_model=FundDepositResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_deposit(
    fund_id: int,
    body: FundDepositCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FundDepositResponse:
    fund = await _get_fund_or_404(fund_id, current_user.household_id, db)
    if fund.status != "active":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot deposit to an archived fund",
        )
    if body.amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Amount must be greater than zero",
        )
    depositor = await db.get(User, body.user_id)
    if (
        depositor is None
        or depositor.household_id != current_user.household_id
        or depositor.left_at is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_id must be an active member of the household",
        )

    deposit = FundDeposit(
        fund_id=fund.id,
        user_id=depositor.id,
        amount=body.amount,
        deposited_at=body.deposited_at,
        note=body.note,
    )
    db.add(deposit)
    await db.flush()
    return _build_deposit_response(deposit, user=depositor)


@router.delete("/{fund_id}/deposits/{deposit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deposit(
    fund_id: int,
    deposit_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    fund = await _get_fund_or_404(fund_id, current_user.household_id, db)
    deposit = await db.get(FundDeposit, deposit_id)
    if deposit is None or deposit.fund_id != fund.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deposit not found")
    if deposit.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the depositor or an admin can delete this deposit",
        )
    await db.delete(deposit)
    await db.flush()


# ---------------------------------------------------------------------------
# Expense endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/{fund_id}/expenses",
    response_model=FundExpenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_expense(
    fund_id: int,
    body: FundExpenseCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FundExpenseResponse:
    fund = await _get_fund_or_404(fund_id, current_user.household_id, db)
    if fund.status != "active":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot log an expense against an archived fund",
        )
    if body.amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Amount must be greater than zero",
        )
    if body.shopping_entry_id is not None:
        entry = await db.get(ShoppingEntry, body.shopping_entry_id)
        if entry is None or entry.household_id != current_user.household_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="shopping_entry_id does not belong to this household",
            )

    expense = FundExpense(
        fund_id=fund.id,
        amount=body.amount,
        description=body.description,
        spent_at=body.spent_at,
        shopping_entry_id=body.shopping_entry_id,
        created_by=current_user.id,
    )
    db.add(expense)
    await db.flush()
    return _build_expense_response(expense, creator=current_user)


@router.delete("/{fund_id}/expenses/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(
    fund_id: int,
    expense_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    fund = await _get_fund_or_404(fund_id, current_user.household_id, db)
    expense = await db.get(FundExpense, expense_id)
    if expense is None or expense.fund_id != fund.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")
    if expense.created_by != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the creator or an admin can delete this expense",
        )
    await db.delete(expense)
    await db.flush()
