"""Schemas for the standalone Grocery Fund ledger (Fund, FundDeposit, FundExpense).

Money is serialised as a string in every response path (project convention),
using the same MoneyDecimal annotated type as the other schema modules.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

from app.schemas.core import UserMini

MoneyDecimal = Annotated[Decimal, PlainSerializer(str, return_type=str)]


# ---------------------------------------------------------------------------
# Fund — create / update
# ---------------------------------------------------------------------------

class FundCreate(BaseModel):
    name: str
    description: str | None = None


class FundUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: Literal["active", "archived"] | None = None


# ---------------------------------------------------------------------------
# Deposit / Expense — create
# ---------------------------------------------------------------------------

class FundDepositCreate(BaseModel):
    user_id: int  # the depositor (any member may record a deposit for any member)
    amount: Decimal
    deposited_at: date
    note: str | None = None


class FundExpenseCreate(BaseModel):
    amount: Decimal
    description: str
    spent_at: date
    shopping_entry_id: int | None = None


# ---------------------------------------------------------------------------
# Deposit / Expense — responses
# ---------------------------------------------------------------------------

class FundDepositResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    fund_id: int
    user: UserMini
    amount: MoneyDecimal
    deposited_at: date
    note: str | None = None
    created_at: datetime


class FundExpenseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    fund_id: int
    amount: MoneyDecimal
    description: str
    spent_at: date
    shopping_entry_id: int | None = None
    created_by_user: UserMini
    created_at: datetime


# ---------------------------------------------------------------------------
# Fund — responses
# ---------------------------------------------------------------------------

class MemberContribution(BaseModel):
    model_config = ConfigDict(json_encoders={Decimal: str})

    user: UserMini
    total: MoneyDecimal


class FundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    household_id: int
    name: str
    description: str | None = None
    status: str
    created_by_user: UserMini
    created_at: datetime
    updated_at: datetime
    total_deposits: MoneyDecimal
    total_expenses: MoneyDecimal
    balance: MoneyDecimal
    contributions: list[MemberContribution] = []


class FundDetailResponse(FundResponse):
    deposits: list[FundDepositResponse] = []
    expenses: list[FundExpenseResponse] = []
