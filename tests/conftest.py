"""Pytest fixtures for the calculation engine test suite.

Schema is created once before any tests run via pytest_sessionstart (asyncio.run).
Each test gets a fresh AsyncSession that is rolled back at teardown — no data
leaks between tests, no cross-event-loop sharing.

Override the DB by setting TEST_DATABASE_URL in the environment.
"""
import asyncio
import os
import uuid
from datetime import date
from decimal import Decimal

import bcrypt
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import Base
from app.models import *  # noqa: F401,F403 — registers all ORM models
from app.models.core import Household, Room, RoomAssignment, User
from app.models.expenses import MealLog, ShoppingEntry, ShoppingItem, UtilityBill

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5433/household_test",
)

# NullPool: no connection caching across tests.
# asyncpg connections are bound to the event loop that created them;
# pytest-asyncio gives each test its own loop, so cached connections
# from the previous test would be broken. NullPool avoids this.
_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)

# Pre-compute one bcrypt hash; rounds=4 keeps tests fast (~30 ms total)
_TEST_PW_HASH: str = bcrypt.hashpw(b"testpass", bcrypt.gensalt(rounds=4)).decode()


# ---------------------------------------------------------------------------
# Schema lifecycle — runs once per pytest session, outside any test loop
# ---------------------------------------------------------------------------

async def _create_schema() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        # drop_all leaves named Postgres ENUM types behind — remove them
        result = await conn.execute(
            text(
                "SELECT typname FROM pg_type "
                "WHERE typtype = 'e' "
                "AND typnamespace = 'public'::regnamespace"
            )
        )
        for row in result:
            await conn.execute(text(f'DROP TYPE IF EXISTS "{row[0]}" CASCADE'))
        await conn.run_sync(Base.metadata.create_all)
    # Dispose the pool so every test gets connections in its own event loop
    await _engine.dispose()


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    asyncio.run(_create_schema())


# ---------------------------------------------------------------------------
# Per-test session — rolls back at teardown
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db() -> AsyncSession:
    async with AsyncSession(_engine, expire_on_commit=False, autoflush=False) as s:
        yield s
        await s.rollback()


# ---------------------------------------------------------------------------
# Core data fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def household(db: AsyncSession) -> Household:
    h = Household(name="Test Household")
    db.add(h)
    await db.flush()
    return h


# ---------------------------------------------------------------------------
# Factory fixtures — sync functions returning async callables
# ---------------------------------------------------------------------------

@pytest.fixture
def make_room(db: AsyncSession):
    async def _make(
        household: Household,
        *,
        rent: Decimal = Decimal("0"),
        service: Decimal = Decimal("0"),
        name: str = "Room",
    ) -> Room:
        room = Room(
            household_id=household.id,
            name=name,
            monthly_rent=rent,
            monthly_service_charge=service,
        )
        db.add(room)
        await db.flush()
        return room

    return _make


@pytest.fixture
def make_user(db: AsyncSession):
    async def _make(
        household: Household,
        room: Room,
        *,
        name: str | None = None,
        month: str = "2026-05",
        joined_at: date = date(2026, 1, 1),
    ) -> User:
        display_name = name or f"User_{uuid.uuid4().hex[:6]}"
        user = User(
            household_id=household.id,
            name=display_name,
            email=f"{uuid.uuid4().hex[:10]}@test.com",
            password_hash=_TEST_PW_HASH,
            joined_at=joined_at,
        )
        db.add(user)
        await db.flush()
        db.add(RoomAssignment(
            room_id=room.id,
            user_id=user.id,
            effective_month=month,
        ))
        await db.flush()
        return user

    return _make


@pytest.fixture
def make_shopping_entry(db: AsyncSession):
    async def _make(
        household: Household,
        paid_by: User,
        month: str,
        items: list[dict],
    ) -> ShoppingEntry:
        """items dicts: {name, price, category, qty=1, target_user_id=None}"""
        entry = ShoppingEntry(
            household_id=household.id,
            month=month,
            paid_by=paid_by.id,
        )
        db.add(entry)
        await db.flush()
        for it in items:
            db.add(ShoppingItem(
                entry_id=entry.id,
                name=it["name"],
                price=Decimal(str(it["price"])),
                quantity=Decimal(str(it.get("qty", "1"))),
                category=it["category"],
                target_user_id=it.get("target_user_id"),
            ))
        await db.flush()
        return entry

    return _make


@pytest.fixture
def make_bill(db: AsyncSession):
    async def _make(
        household: Household,
        paid_by: User,
        month: str,
        amount: Decimal,
        bill_type: str = "electricity",
    ) -> UtilityBill:
        year, mo = int(month[:4]), int(month[5:])
        bill = UtilityBill(
            household_id=household.id,
            month=month,
            type=bill_type,
            amount=Decimal(str(amount)),
            paid_by=paid_by.id,
            paid_at=date(year, mo, 1),
        )
        db.add(bill)
        await db.flush()
        return bill

    return _make


@pytest.fixture
def make_meal_log(db: AsyncSession):
    async def _make(
        user: User,
        log_date: date,
        meal_count: int | Decimal,
        guest_meals: int | Decimal = 0,
    ) -> MealLog:
        log = MealLog(
            user_id=user.id,
            log_date=log_date,
            meal_count=Decimal(str(meal_count)),
            guest_meals=Decimal(str(guest_meals)),
        )
        db.add(log)
        await db.flush()
        return log

    return _make
