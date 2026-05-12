"""Settlement and month-tracking models."""
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TimestampCreate

MonthStatus = Enum("open", "closed", name="month_status")

AuditAction = Enum("create", "update", "delete", name="audit_action")


class Month(Base):
    """One row per calendar month for the household.

    id is the month key in 'YYYY-MM' format.
    """

    __tablename__ = "months"

    id: Mapped[str] = mapped_column(String(7), primary_key=True)  # "YYYY-MM"
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(MonthStatus, nullable=False, default="open")
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[TimestampCreate]


class Settlement(Base):
    """A transfer from one user to another, generated at month close.

    Computed by minimize_transfers() — for N users there are at most N-1 rows.
    """

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    month_id: Mapped[str] = mapped_column(
        ForeignKey("months.id", ondelete="CASCADE"), nullable=False
    )
    from_user: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    to_user: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_marked_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[TimestampCreate]


class AuditLog(Base):
    """Captures every create/update/delete for trust and dispute resolution.

    Populated by a middleware/service in app/utils/audit.py.
    Retention: keep 6 months, archive older.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(AuditAction, nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int] = mapped_column(nullable=False)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
