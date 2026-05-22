"""SecurityDeposit schemas — full admin view and limited public view."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

from app.schemas.core import UserMini

MoneyDecimal = Annotated[Decimal, PlainSerializer(str, return_type=str)]


class SecurityDepositCreate(BaseModel):
    user_id: int
    amount: Decimal
    deposited_at: date


class SecurityDepositUpdate(BaseModel):
    """Admin-only patch: record a refund or deductions when a user leaves."""
    status: Literal["held", "refunded", "partial"] | None = None
    refunded_at: date | None = None
    deduction_reason: str | None = None
    deduction_amount: Decimal | None = None
    applied_to_dues: Decimal | None = None
    final_refund: Decimal | None = None


class SecurityDepositResponsePublic(BaseModel):
    """Visible to all flatmates: confirms a deposit exists without exposing deductions."""
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    user_id: int
    amount: MoneyDecimal
    deposited_at: date
    status: str


class SecurityDepositResponseFull(BaseModel):
    """Admin-only view: all fields including deduction breakdown."""
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    user: UserMini
    amount: MoneyDecimal
    deposited_at: date
    held_by: UserMini
    status: str
    refunded_at: date | None = None
    deduction_reason: str | None = None
    deduction_amount: MoneyDecimal
    applied_to_dues: MoneyDecimal
    final_refund: MoneyDecimal
    created_at: datetime
    updated_at: datetime
