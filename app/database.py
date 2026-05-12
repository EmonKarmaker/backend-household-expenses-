"""Async SQLAlchemy database setup."""
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Annotated

from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

settings = get_settings()

# Neon (and most managed Postgres) require SSL.
# asyncpg uses `ssl=True` in connect_args instead of sslmode in the URL.
_connect_args = {}
if "neon.tech" in settings.database_url or "supabase" in settings.database_url:
    _connect_args["ssl"] = "require"

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_env == "development",
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all ORM models."""

    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }


# Common column types used across models
TimestampCreate = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
]

TimestampUpdate = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
]


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session.

    Always rolls back on exception to prevent dead transactions
    (a hard-won lesson from the Dixon project).
    """
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def utcnow() -> datetime:
    """Use this everywhere instead of datetime.utcnow() (which is deprecated)."""
    return datetime.now(timezone.utc)
