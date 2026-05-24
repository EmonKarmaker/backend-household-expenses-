"""Password reset token model."""
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampCreate

if TYPE_CHECKING:
    from app.models.core import User


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 hex digest of the raw token — raw token is never persisted.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[TimestampCreate]

    user: Mapped["User"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<PasswordResetToken id={self.id} user_id={self.user_id}"
            f" used={self.used_at is not None}>"
        )
