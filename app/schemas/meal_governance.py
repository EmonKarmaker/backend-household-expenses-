"""Schemas for meal governance — the per-month meal-edit permission flow.

Disputes are handled in a separate prompt/module; this file covers only the
MealEditPermission request / approve / reject flow.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.core import MonthStr, UserMini


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
