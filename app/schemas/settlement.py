"""Schemas for Month and Settlement. AuditLog is internal — no schema."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

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
    from_user: int
    to_user: int
    amount: MoneyDecimal
    paid: bool
    paid_at: datetime | None = None
    paid_marked_by: int | None = None
    created_at: datetime
