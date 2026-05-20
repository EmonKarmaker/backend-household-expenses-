"""Schemas for UtilityBill, ShoppingEntry/Item, ItemCatalog, MealLog."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

MoneyDecimal = Annotated[Decimal, PlainSerializer(str, return_type=str)]
MonthStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]
BillTypeStr = Annotated[str, Field(min_length=1, max_length=50)]


# ---------------------------------------------------------------------------
# UtilityBill
# ---------------------------------------------------------------------------

class UtilityBillCreate(BaseModel):
    month: MonthStr
    type: BillTypeStr
    amount: Decimal
    paid_at: date
    note: str | None = None


class UtilityBillUpdate(BaseModel):
    month: MonthStr | None = None
    type: BillTypeStr | None = None
    amount: Decimal | None = None
    paid_at: date | None = None
    note: str | None = None


class UtilityBillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    household_id: int
    month: str
    type: str
    amount: MoneyDecimal
    paid_by: int
    paid_at: date
    photo_url: str | None = None
    note: str | None = None
    created_at: datetime
    updated_at: datetime


class BillTypesResponse(BaseModel):
    types: list[str]


# ---------------------------------------------------------------------------
# ShoppingItem  (defined first — referenced by ShoppingEntry schemas)
# ---------------------------------------------------------------------------

class ShoppingItemCreate(BaseModel):
    name: str
    price: Decimal
    quantity: Decimal = Decimal("1")
    category: Literal["meal", "household", "personal"]
    target_user_id: int | None = None


class ShoppingItemUpdate(BaseModel):
    name: str | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    category: Literal["meal", "household", "personal"] | None = None
    target_user_id: int | None = None  # explicitly nullable — send null to clear


class ShoppingItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    entry_id: int
    name: str
    price: MoneyDecimal
    quantity: MoneyDecimal
    category: str
    target_user_id: int | None = None
    line_total: MoneyDecimal  # computed @property on the ORM model
    created_at: datetime


# ---------------------------------------------------------------------------
# ShoppingEntry  (items[] included in same request per plan)
# ---------------------------------------------------------------------------

class ShoppingEntryCreate(BaseModel):
    month: MonthStr
    items: list[ShoppingItemCreate] = Field(min_length=1)
    note: str | None = None


class ShoppingEntryUpdate(BaseModel):
    """PATCH accepts multipart form (note + optional photo); this schema documents the updatable fields."""
    note: str | None = None


class ShoppingEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    household_id: int
    month: str
    paid_by: int
    photo_url: str | None = None
    note: str | None = None
    items: list[ShoppingItemResponse] = []
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# ItemCatalog  (read-only — written by the shopping router automatically)
# ---------------------------------------------------------------------------

class ItemCatalogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    household_id: int
    name: str
    default_category: str
    last_used_at: datetime
    use_count: int


# ---------------------------------------------------------------------------
# MealLog
# ---------------------------------------------------------------------------

class MealLogUpsert(BaseModel):
    """Single entry for bulk upsert. user_id + log_date is the unique key."""
    user_id: int
    log_date: date
    meal_count: Decimal = Field(default=Decimal("0"), ge=0, le=10)
    guest_meals: Decimal = Field(default=Decimal("0"), ge=0, le=20)
    note: str | None = None


class MealLogBulkUpsertRequest(BaseModel):
    entries: list[MealLogUpsert] = Field(min_length=1)


class MealLogUpdate(BaseModel):
    meal_count: Annotated[Decimal, Field(ge=0, le=10)] | None = None
    guest_meals: Annotated[Decimal, Field(ge=0, le=20)] | None = None
    note: str | None = None


class MealLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})

    id: int
    user_id: int
    log_date: date
    meal_count: MoneyDecimal
    guest_meals: MoneyDecimal
    total_meals: MoneyDecimal  # computed @property on the ORM model
    note: str | None = None
    created_at: datetime
    updated_at: datetime
