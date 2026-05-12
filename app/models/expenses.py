"""Expense-related models: bills, shopping entries, items, item catalog, meal logs."""
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampCreate, TimestampUpdate

if TYPE_CHECKING:
    from app.models.core import User


# --- Enums (stored as strings in Postgres for readability) ---

UtilityType = Enum(
    "electricity",
    "internet",
    "gas",
    "water",
    "other",
    name="utility_type",
)

ItemCategory = Enum(
    "meal",
    "household",
    "personal",
    name="item_category",
)


class UtilityBill(Base):
    __tablename__ = "utility_bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # "YYYY-MM"
    type: Mapped[str] = mapped_column(UtilityType, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    paid_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    paid_at: Mapped[date] = mapped_column(Date, nullable=False)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]


class ShoppingEntry(Base):
    """A single shopping trip (one receipt).

    Total amount is computed from items, not stored — items are the source of truth.
    """

    __tablename__ = "shopping_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    paid_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]

    items: Mapped[list["ShoppingItem"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )


class ShoppingItem(Base):
    """One line item from a shopping trip with its own category."""

    __tablename__ = "shopping_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("shopping_entries.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False, default=Decimal("1")
    )
    category: Mapped[str] = mapped_column(ItemCategory, nullable=False)
    # Only set when category == 'personal'
    target_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[TimestampCreate]

    entry: Mapped["ShoppingEntry"] = relationship(back_populates="items")

    @property
    def line_total(self) -> Decimal:
        return self.price * self.quantity


class ItemCatalog(Base):
    """Autocomplete suggestions for shopping items.

    Updated each time a user adds an item — remembers name + default category
    so the next time someone types 'Vim' the form auto-fills the category.
    """

    __tablename__ = "item_catalog"
    __table_args__ = (
        UniqueConstraint("household_id", "name", name="uq_item_catalog_household_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    default_category: Mapped[str] = mapped_column(ItemCategory, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    use_count: Mapped[int] = mapped_column(nullable=False, default=1)


class MealLog(Base):
    """One row per user per day they logged meals."""

    __tablename__ = "meal_logs"
    __table_args__ = (
        UniqueConstraint("user_id", "log_date", name="uq_meal_log_user_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Numeric so half-meals work (e.g. small lunch = 0.5)
    meal_count: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("0")
    )
    guest_meals: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("0")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]

    user: Mapped["User"] = relationship(back_populates="meal_logs")

    @property
    def total_meals(self) -> Decimal:
        return self.meal_count + self.guest_meals
