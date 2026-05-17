"""Schemas for SharedAsset, AssetContribution, AssetRefund."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

MoneyDecimal = Annotated[Decimal, PlainSerializer(str, return_type=str)]


# ---------------------------------------------------------------------------
# AssetContribution
# ---------------------------------------------------------------------------

class AssetContributionInput(BaseModel):
    """Input for a single contribution when creating an asset via the API."""
    user_id: int
    amount: Decimal


class AssetContributionCreate(BaseModel):
    user_id: int
    amount: Decimal
    contributed_at: date
    contribution_type: Literal["initial", "buyin", "replacement"]


class AssetContributionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    asset_id: int
    user_id: int
    amount: MoneyDecimal
    contributed_at: date
    contribution_type: str
    created_at: datetime


# ---------------------------------------------------------------------------
# AssetRefund
# ---------------------------------------------------------------------------

class AssetRefundCreate(BaseModel):
    user_id: int
    amount: Decimal
    refunded_at: date
    paid_by_user: int | None = None
    replaced_by_user_id: int | None = None


class AssetRefundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    asset_id: int
    user_id: int
    amount: MoneyDecimal
    refunded_at: date
    paid_by_user: int | None = None
    replaced_by_user_id: int | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# SharedAsset
# ---------------------------------------------------------------------------

class SharedAssetCreate(BaseModel):
    name: str
    description: str | None = None
    purchase_date: date
    total_cost: Decimal
    requires_buyin_from_new_members: bool = True
    bought_by_user_id: int


class SharedAssetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: Literal["active", "disposed"] | None = None
    requires_buyin_from_new_members: bool | None = None


class SharedAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    household_id: int
    name: str
    description: str | None = None
    photo_url: str | None = None
    purchase_date: date
    total_cost: MoneyDecimal
    requires_buyin_from_new_members: bool
    status: str
    bought_by_user_id: int
    created_at: datetime
    updated_at: datetime
    contributions: list[AssetContributionResponse] = []
    refunds: list[AssetRefundResponse] = []


# ---------------------------------------------------------------------------
# Buy-in
# ---------------------------------------------------------------------------

class AssetBuyinRequest(BaseModel):
    asset_id: int
    amount: Decimal


class AssetBuyinResponse(BaseModel):
    contribution_id: int
    amount: MoneyDecimal
    fulfilled_refunds: list[int]
