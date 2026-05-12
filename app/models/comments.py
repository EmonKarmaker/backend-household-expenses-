"""Comments on expenses, bills, and assets for transparency."""
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TimestampCreate

CommentEntryType = Enum("shopping", "bill", "asset", name="comment_entry_type")


class Comment(Base):
    """A comment attached to a shopping entry, utility bill, or asset.

    Any flatmate can comment — used for flagging or clarifying entries
    before the month is closed.
    """

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_type: Mapped[str] = mapped_column(CommentEntryType, nullable=False)
    entry_id: Mapped[int] = mapped_column(nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[TimestampCreate]
