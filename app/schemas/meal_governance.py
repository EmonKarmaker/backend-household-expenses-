"""Schemas for meal governance.

Two flows:
- the per-month meal-edit permission flow (request / approve / reject), and
- the meal dispute flow (a member disputes a meal log; an admin resolves it).
"""
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.schemas.core import MonthStr, UserMini
from app.schemas.expenses import MealLogResponse


# ---------------------------------------------------------------------------
# Permission — request bodies
# ---------------------------------------------------------------------------

class MealPermissionRequest(BaseModel):
    member_user_id: int
    month: MonthStr  # "YYYY-MM"


class MealPermissionReject(BaseModel):
    reason: str | None = None


# ---------------------------------------------------------------------------
# Permission — response
# ---------------------------------------------------------------------------

class MealPermissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    household_id: int
    admin_user: UserMini
    member_user: UserMini
    month: str
    status: str
    reject_reason: str | None = None
    requested_at: datetime
    resolved_at: datetime | None = None


# ---------------------------------------------------------------------------
# Dispute — request body
# ---------------------------------------------------------------------------

# Non-empty after trimming surrounding whitespace (blank reason → 422).
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MealDisputeCreate(BaseModel):
    meal_log_id: int
    reason: NonEmptyStr


# ---------------------------------------------------------------------------
# Dispute — response
# ---------------------------------------------------------------------------

class MealDisputeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    household_id: int
    meal_log_id: int
    raised_by_user: UserMini
    reason: str
    status: str
    resolved_by_user: UserMini | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    # Snapshot of the disputed log so the admin sees WHAT is being disputed
    # without a second fetch.
    meal_log: MealLogResponse
