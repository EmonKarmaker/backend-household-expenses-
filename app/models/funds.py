"""Grocery Fund models: standalone ledger of deposits and expenses.

This is a self-contained ledger feature — it does NOT feed into the
settlement engine. A fund collects deposits from members and tracks
expenses paid out of the pooled money.
"""
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampCreate, TimestampUpdate

if TYPE_CHECKING:
    from app.models.core import Household, User
    from app.models.expenses import ShoppingEntry


class Fund(Base):
    """A pooled grocery fund for a household."""

    __tablename__ = "funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "active" | "archived"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]

    household: Mapped["Household"] = relationship(
        foreign_keys=[household_id]
    )
    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by])
    deposits: Mapped[list["FundDeposit"]] = relationship(
        back_populates="fund", cascade="all, delete-orphan"
    )
    expenses: Mapped[list["FundExpense"]] = relationship(
        back_populates="fund", cascade="all, delete-orphan"
    )


class FundDeposit(Base):
    """A deposit made into a fund by a user."""

    __tablename__ = "fund_deposits"

    id: Mapped[int] = mapped_column(primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("funds.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    deposited_at: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[TimestampCreate]

    fund: Mapped["Fund"] = relationship(back_populates="deposits")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class FundExpense(Base):
    """An expense paid out of a fund.

    Optionally links to a shopping entry; if that entry is deleted the
    link is cleared (SET NULL) but the expense record remains.
    """

    __tablename__ = "fund_expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("funds.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    spent_at: Mapped[date] = mapped_column(Date, nullable=False)
    shopping_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("shopping_entries.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[TimestampCreate]

    fund: Mapped["Fund"] = relationship(back_populates="expenses")
    shopping_entry: Mapped["ShoppingEntry | None"] = relationship(
        foreign_keys=[shopping_entry_id]
    )
    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by])
