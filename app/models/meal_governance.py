"""Meal governance models: per-month edit permissions and meal disputes.

Two related but independent features:

- MealEditPermission: an admin requests permission to edit a specific
  member's meals for a given month. Once approved, the admin may edit that
  member's meals freely for that month. Permission is granted per member per
  month (it resets monthly — a new month needs a new grant).
- MealDispute: any member may dispute a meal log entry they believe is
  wrong; an admin resolves it.
"""
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampCreate

if TYPE_CHECKING:
    from app.models.core import Household, User
    from app.models.expenses import MealLog


class MealEditPermission(Base):
    """An admin's request to edit a member's meals for one month.

    One record per member per month — enforced by a unique constraint on
    (member_user_id, month).
    """

    __tablename__ = "meal_edit_permissions"
    __table_args__ = (
        UniqueConstraint(
            "member_user_id", "month", name="uq_meal_edit_permission_member_month"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    # Who requested permission.
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Whose meals the permission applies to.
    member_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # "YYYY-MM"
    # "pending" | "approved" | "rejected"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[TimestampCreate]
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    household: Mapped["Household"] = relationship(foreign_keys=[household_id])
    admin_user: Mapped["User"] = relationship(foreign_keys=[admin_user_id])
    member_user: Mapped["User"] = relationship(foreign_keys=[member_user_id])


class MealDispute(Base):
    """A member's dispute over a meal log entry, resolved by an admin."""

    __tablename__ = "meal_disputes"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    # The disputed entry.
    meal_log_id: Mapped[int] = mapped_column(
        ForeignKey("meal_logs.id", ondelete="CASCADE"), nullable=False
    )
    # The member raising the dispute.
    raised_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # "open" | "resolved"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    # The admin who resolved it (null while open).
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[TimestampCreate]
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    household: Mapped["Household"] = relationship(foreign_keys=[household_id])
    meal_log: Mapped["MealLog"] = relationship(foreign_keys=[meal_log_id])
    raised_by_user: Mapped["User"] = relationship(foreign_keys=[raised_by])
    resolved_by_user: Mapped["User | None"] = relationship(
        foreign_keys=[resolved_by]
    )
