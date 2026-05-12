"""Core models: household, rooms, users, room assignments."""
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampCreate, TimestampUpdate

if TYPE_CHECKING:
    from app.models.assets import AssetContribution, AssetRefund, SharedAsset
    from app.models.comments import Comment
    from app.models.deposits import SecurityDeposit
    from app.models.expenses import MealLog, ShoppingEntry, UtilityBill
    from app.models.settlement import AuditLog, Month, Settlement


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[TimestampCreate]

    rooms: Mapped[list["Room"]] = relationship(back_populates="household")
    users: Mapped[list["User"]] = relationship(back_populates="household")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    monthly_rent: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    monthly_service_charge: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]

    household: Mapped["Household"] = relationship(back_populates="rooms")
    assignments: Mapped[list["RoomAssignment"]] = relationship(back_populates="room")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    joined_at: Mapped[date] = mapped_column(Date, nullable=False)
    left_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]

    household: Mapped["Household"] = relationship(back_populates="users")
    assignments: Mapped[list["RoomAssignment"]] = relationship(back_populates="user")
    meal_logs: Mapped[list["MealLog"]] = relationship(back_populates="user")
    deposits: Mapped[list["SecurityDeposit"]] = relationship(
        foreign_keys="SecurityDeposit.user_id", back_populates="user"
    )


class RoomAssignment(Base):
    """Tracks which user is in which room, effective from a given month.

    The latest assignment (by effective_month) for a user is the active one.
    """

    __tablename__ = "room_assignments"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "effective_month", name="uq_room_assignment_user_month"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Format: "YYYY-MM" e.g. "2026-05"
    effective_month: Mapped[str] = mapped_column(String(7), nullable=False)
    created_at: Mapped[TimestampCreate]

    room: Mapped["Room"] = relationship(back_populates="assignments")
    user: Mapped["User"] = relationship(back_populates="assignments")
