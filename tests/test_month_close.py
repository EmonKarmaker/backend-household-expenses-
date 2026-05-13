"""Tests for app/services/month_close.py — close, reopen, and re-close."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settlement import Month, Settlement
from app.services.month_close import (
    MonthAlreadyClosed,
    MonthAlreadyOpen,
    MonthNotFound,
    close_month,
    reopen_month,
)

MONTH = "2026-05"


# ---------------------------------------------------------------------------
# Shared seed helper
# ---------------------------------------------------------------------------

async def _seed(household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log):
    """3-user, 2-room full scenario used by all close/reopen tests.

    Settlement balances (excluding rent):
      A = +5666.67  (big creditor — paid utilities + all shopping)
      B = -2666.67  (debtor)
      C = -3000.00  (debtor)
    Greedy minimisation → 2 transfers: C→A 3000.00, B→A 2666.67
    """
    room1 = await make_room(household, rent=Decimal("5000"), service=Decimal("300"), name="Room 1")
    room2 = await make_room(household, rent=Decimal("6000"), service=Decimal("400"), name="Room 2")

    user_a = await make_user(household, room1, name="Alice", month=MONTH)
    user_b = await make_user(household, room2, name="Bob",   month=MONTH)
    user_c = await make_user(household, room2, name="Carol", month=MONTH)

    await make_bill(household, user_a, MONTH, Decimal("1500"), "electricity")
    await make_bill(household, user_b, MONTH, Decimal("1200"), "internet")

    await make_shopping_entry(household, user_a, MONTH, [
        {"name": "Rice",       "price": "4500", "category": "meal"},
        {"name": "Vegetables", "price": "1500", "category": "meal"},
        {"name": "Vim",        "price": "300",  "category": "household"},
        {"name": "Shampoo",    "price": "200",  "category": "personal",
         "target_user_id": user_b.id},
    ])

    await make_meal_log(user_a, date(2026, 5, 1), meal_count=20)
    await make_meal_log(user_b, date(2026, 5, 1), meal_count=40)
    await make_meal_log(user_c, date(2026, 5, 1), meal_count=30)

    return user_a, user_b, user_c


# ---------------------------------------------------------------------------
# Test 1 — Close a fresh month (no existing Month row)
# ---------------------------------------------------------------------------

async def test_close_fresh_month(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """close_month() creates the Month row, generates correct Settlement rows."""
    user_a, user_b, user_c = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )

    month, settlements = await close_month(MONTH, household.id, user_a.id, db)

    # Month state
    assert month.status == "closed"
    assert month.closed_at is not None
    assert month.closed_by == user_a.id
    assert month.id == MONTH
    assert month.household_id == household.id

    # Settlement count: 3 users → at most N-1 = 2 transfers
    assert 1 <= len(settlements) <= 2

    # Amounts and direction
    # A is the sole creditor (settlement_balance +5666.67)
    amounts = {(s.from_user, s.to_user): s.amount for s in settlements}
    total_to_a = sum(s.amount for s in settlements if s.to_user == user_a.id)
    assert total_to_a == Decimal("5666.67")

    # All settlements are unpaid at creation
    assert all(s.paid is False for s in settlements)
    assert all(s.paid_at is None for s in settlements)

    # DB confirms the rows exist with correct month_id
    result = await db.execute(
        select(Settlement).where(Settlement.month_id == MONTH)
    )
    db_rows = result.scalars().all()
    assert len(db_rows) == len(settlements)


# ---------------------------------------------------------------------------
# Test 2 — Closing an already-closed month raises MonthAlreadyClosed
# ---------------------------------------------------------------------------

async def test_close_already_closed_raises(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """Second call to close_month() on a closed month raises MonthAlreadyClosed."""
    user_a, *_ = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )

    await close_month(MONTH, household.id, user_a.id, db)

    with pytest.raises(MonthAlreadyClosed):
        await close_month(MONTH, household.id, user_a.id, db)


# ---------------------------------------------------------------------------
# Test 3 — Reopen restores writability; settlements kept as history
# ---------------------------------------------------------------------------

async def test_reopen_restores_writability(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """After reopen: Month.status='open', closed_* cleared; Settlement rows survive."""
    user_a, *_ = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )

    _, initial_settlements = await close_month(MONTH, household.id, user_a.id, db)
    initial_count = len(initial_settlements)

    month = await reopen_month(MONTH, household.id, user_a.id, db)

    assert month.status == "open"
    assert month.closed_at is None
    assert month.closed_by is None

    # Historical settlement rows must still be present
    result = await db.execute(
        select(Settlement).where(Settlement.month_id == MONTH)
    )
    surviving = result.scalars().all()
    assert len(surviving) == initial_count

    # Reopening an already-open month raises
    with pytest.raises(MonthAlreadyOpen):
        await reopen_month(MONTH, household.id, user_a.id, db)


# ---------------------------------------------------------------------------
# Test 4 — Close after reopen deletes stale settlements and regenerates them
# ---------------------------------------------------------------------------

async def test_close_after_reopen_regenerates_settlements(
    db: AsyncSession,
    household,
    make_room,
    make_user,
    make_bill,
    make_shopping_entry,
    make_meal_log,
):
    """Re-closing after a reopen produces fresh settlements matching new balances.

    After adding 6000 BDT of household spending paid by C:
      New balances: A=+3666.67, B=-4666.67, C=+1000.00
      New transfers: B→A 3666.67, B→C 1000.00
      Total transferred: 4666.67  (was 5666.67 initially)
    """
    user_a, user_b, user_c = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )

    # --- First close ---
    _, first_settlements = await close_month(MONTH, household.id, user_a.id, db)
    first_total = sum(s.amount for s in first_settlements)
    first_ids = {s.id for s in first_settlements}

    # --- Reopen ---
    await reopen_month(MONTH, household.id, user_a.id, db)

    # --- Add extra spending that shifts balances ---
    # C pays 6000 BDT household; this makes C a creditor (+1000) and increases B's debt
    await make_shopping_entry(household, user_c, MONTH, [
        {"name": "Cleaning supplies", "price": "6000", "category": "household"},
    ])

    # --- Second close ---
    month2, second_settlements = await close_month(MONTH, household.id, user_a.id, db)

    assert month2.status == "closed"

    # Old settlement IDs must be gone, new rows have higher IDs
    second_ids = {s.id for s in second_settlements}
    assert first_ids.isdisjoint(second_ids), "Old settlement rows were not replaced"

    # Amounts changed (total transferred is now 4666.67, not 5666.67)
    second_total = sum(s.amount for s in second_settlements)
    assert second_total != first_total, (
        f"Settlements unchanged after reopen+new data: {second_total} == {first_total}"
    )
    assert second_total == Decimal("4666.67")

    # DB has exactly the new rows (old ones deleted)
    result = await db.execute(
        select(Settlement).where(Settlement.month_id == MONTH)
    )
    db_rows = result.scalars().all()
    db_ids = {r.id for r in db_rows}
    assert db_ids == second_ids

    # Reopen-then-reopen-when-not-found guard
    with pytest.raises(MonthNotFound):
        await reopen_month("2099-01", household.id, user_a.id, db)
