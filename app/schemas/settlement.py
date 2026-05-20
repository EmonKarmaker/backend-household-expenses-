"""Schemas for Month and Settlement. AuditLog is internal — no schema."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

from app.schemas.core import UserMini

MoneyDecimal = Annotated[Decimal, PlainSerializer(str, return_type=str)]


# ---------------------------------------------------------------------------
# Month
# ---------------------------------------------------------------------------

class MonthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str  # "YYYY-MM"
    household_id: int
    status: Literal["open", "closed"]
    closed_at: datetime | None = None
    closed_by: int | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

class SettlementResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_encoders={Decimal: str},
    )

    id: int
    month_id: str
    from_user: UserMini
    to_user: UserMini
    amount: MoneyDecimal
    paid: bool
    paid_at: datetime | None = None
    paid_marked_by: UserMini | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Month close response
# ---------------------------------------------------------------------------

class MonthCloseResponse(BaseModel):
    month_id: str
    status: str
    closed_at: datetime
    closed_by: UserMini
    settlements: list[SettlementResponse]


class MonthReopenResponse(BaseModel):
    month_id: str
    status: str


# ---------------------------------------------------------------------------
# Settlement mark-paid request
# ---------------------------------------------------------------------------

class MarkSettlementPaidRequest(BaseModel):
    paid: bool
