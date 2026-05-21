"""Schemas for Household, Room, RoomAssignment, and User."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer

# Decimal serialised as string in all JSON output paths (Pydantic v2 compatible).
# json_encoders is kept for belt-and-suspenders with model_dump_json().
MoneyDecimal = Annotated[Decimal, PlainSerializer(str, return_type=str)]

# "YYYY-MM" month strings validated at the boundary.
MonthStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]


# ---------------------------------------------------------------------------
# Household
# ---------------------------------------------------------------------------

class HouseholdCreate(BaseModel):
    name: str


class HouseholdResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------

class RoomCreate(BaseModel):
    name: str
    monthly_rent: Decimal
    monthly_service_charge: Decimal = Decimal("0")


class RoomUpdate(BaseModel):
    name: str | None = None
    monthly_rent: Decimal | None = None
    monthly_service_charge: Decimal | None = None


class RoomResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_encoders={Decimal: str},
    )

    id: int
    household_id: int
    name: str
    monthly_rent: MoneyDecimal
    monthly_service_charge: MoneyDecimal
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# UserMini  — minimal user reference for embedding in response schemas
# ---------------------------------------------------------------------------

class UserMini(BaseModel):
    """Minimal user reference for embedding in other response schemas."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


# ---------------------------------------------------------------------------
# User  — password_hash is NEVER included in any response schema
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    name: str
    email: EmailStr
    joined_at: date


class UserUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    joined_at: date | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    household_id: int
    name: str
    email: str
    joined_at: date
    left_at: date | None = None
    is_admin: bool
    must_change_password: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# RoomAssignment
# ---------------------------------------------------------------------------

class RoomAssignmentCreate(BaseModel):
    room_id: int
    user_id: int
    effective_month: MonthStr


class RoomAssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    room_id: int
    user: UserMini
    effective_month: str
    created_at: datetime


# ---------------------------------------------------------------------------
# User invite / management
# ---------------------------------------------------------------------------

class UserPatchRequest(BaseModel):
    name: str | None = None
    email: EmailStr | None = None


class InviteUserRequest(BaseModel):
    name: str
    email: EmailStr
    room_id: int
    deposit_amount: Decimal
    effective_month: MonthStr | None = None


class InviteUserResponse(BaseModel):
    user: UserResponse
    email_sent: bool


class AssignRoomRequest(BaseModel):
    room_id: int
    effective_month: MonthStr


class ProcessLeavingRequest(BaseModel):
    leave_date: date


class ProcessLeavingAssetRefund(BaseModel):
    asset_id: int
    asset_name: str
    amount: MoneyDecimal
    paid_by_user: UserMini | None  # None = pending new-member buy-in


class ProcessLeavingResponse(BaseModel):
    user_id: int
    left_at: date
    dues: MoneyDecimal
    deposit_amount: MoneyDecimal
    deposit_applied_to_dues: MoneyDecimal
    deposit_cash_refund: MoneyDecimal
    remaining_dues: MoneyDecimal
    asset_refunds: list[ProcessLeavingAssetRefund]
    total_cash_owed_to_leaver: MoneyDecimal
