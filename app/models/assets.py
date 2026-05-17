"""Shared asset models: durable purchases like fans, cookers, furniture."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampCreate, TimestampUpdate

AssetStatus = Enum("active", "disposed", name="asset_status")

ContributionType = Enum(
    "initial",  # Original purchase contribution
    "buyin",  # New member joining buys into existing asset
    "replacement",  # New member fills the slot of a leaver
    name="contribution_type",
)


class SharedAsset(Base):
    """A durable household purchase (fan, cooker, furniture, etc.)."""

    __tablename__ = "shared_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Admin flag: do new joiners need to buy in?
    requires_buyin_from_new_members: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    status: Mapped[str] = mapped_column(AssetStatus, nullable=False, default="active")
    bought_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[TimestampCreate]
    updated_at: Mapped[TimestampUpdate]

    contributions: Mapped[list["AssetContribution"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )
    refunds: Mapped[list["AssetRefund"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class AssetContribution(Base):
    """How much each user contributed to a shared asset."""

    __tablename__ = "asset_contributions"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("shared_assets.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    contributed_at: Mapped[date] = mapped_column(Date, nullable=False)
    contribution_type: Mapped[str] = mapped_column(ContributionType, nullable=False)
    created_at: Mapped[TimestampCreate]

    asset: Mapped["SharedAsset"] = relationship(back_populates="contributions")


class AssetRefund(Base):
    """Records when a leaver was refunded their contribution to an asset.

    replaced_by_user_id is set when a new member takes the leaver's slot
    and pays for it. Until then, the flat head holds the refund liability.
    """

    __tablename__ = "asset_refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("shared_assets.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    refunded_at: Mapped[date] = mapped_column(Date, nullable=False)
    paid_by_user: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    replaced_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[TimestampCreate]

    asset: Mapped["SharedAsset"] = relationship(back_populates="refunds")
