"""Security deposit model."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Enum, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampCreate, TimestampUpdate

DepositStatus = Enum("held", "refunded", "partial", name="deposit_status")


class SecurityDeposit(Base):
    """A user's security deposit, held by the flat head admin.

    On leaving, the deposit can be used to settle unpaid dues, with
    any deductions for damage, then the remainder is refunded.
    """

    __tablename__ = "security_deposits"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    deposited_at: Mapped[date] = mapped_column(Date, nullable=False)
    held_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(DepositStatus, nullable=False, default="held")

    # Filled in when the user leaves
    refunded_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    deduction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deduction_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    applied_to_dues: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    final_refund: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )

    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]

    user: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys=[user_id], back_populates="deposits"
    )
    held_by_user: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys=[held_by_user_id]
    )
