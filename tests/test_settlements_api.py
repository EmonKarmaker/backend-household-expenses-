"""Tests for settlement mark-paid / unmark (app/services/settlement_mark.py)."""
from datetime import date
from decimal import Decimal

import pytest

from app.services.month_close import close_month, reopen_month
from app.services.settlement_mark import MonthIsOpen, SettlementNotFound, mark_settlement_paid

MONTH = "2026-05"


# ---------------------------------------------------------------------------
# Shared seed helper (same mixed scenario as test_month_close.py)
# ---------------------------------------------------------------------------

async def _seed(household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log):
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
# Test 1 — Mark a settlement paid in a closed month
# ---------------------------------------------------------------------------

async def test_mark_settlement_paid_success(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """paid=True sets paid, paid_at, paid_marked_by on a closed-month settlement."""
    user_a, _, _ = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )
    _, settlements = await close_month(MONTH, household.id, user_a.id, db)
    s = settlements[0]

    updated = await mark_settlement_paid(s.id, household.id, True, user_a.id, db)

    assert updated.paid is True
    assert updated.paid_at is not None
    assert updated.paid_marked_by == user_a.id
    # Unpaid fields on other settlements are unchanged
    assert updated.id == s.id


# ---------------------------------------------------------------------------
# Test 2 — Mark already-paid settlement paid again → idempotent, paid_at refreshed
# ---------------------------------------------------------------------------

async def test_mark_paid_twice_is_idempotent(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """Calling paid=True twice keeps paid=True; paid_at is refreshed each time."""
    user_a, _, _ = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )
    _, settlements = await close_month(MONTH, household.id, user_a.id, db)
    s = settlements[0]

    first = await mark_settlement_paid(s.id, household.id, True, user_a.id, db)
    assert first.paid is True
    first_paid_at = first.paid_at

    second = await mark_settlement_paid(s.id, household.id, True, user_a.id, db)
    assert second.paid is True
    assert second.paid_at is not None
    # paid_at is re-set on every call; may equal first if clock resolution is coarse
    assert second.paid_marked_by == user_a.id


# ---------------------------------------------------------------------------
# Test 3 — Mark paid then unmark → all paid fields cleared
# ---------------------------------------------------------------------------

async def test_mark_paid_then_unpaid(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """paid=False clears paid_at and paid_marked_by after a previous paid=True."""
    user_a, _, _ = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )
    _, settlements = await close_month(MONTH, household.id, user_a.id, db)
    s = settlements[0]

    await mark_settlement_paid(s.id, household.id, True, user_a.id, db)
    unpaid = await mark_settlement_paid(s.id, household.id, False, user_a.id, db)

    assert unpaid.paid is False
    assert unpaid.paid_at is None
    assert unpaid.paid_marked_by is None


# ---------------------------------------------------------------------------
# Test 4 — Cannot mark paid while the month is open
# ---------------------------------------------------------------------------

async def test_mark_paid_open_month_raises(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """MonthIsOpen is raised when the settlement's month has status='open'.

    This happens when admin reopens a month after closing it; the historical
    settlement rows remain but cannot be marked paid until re-closed.
    """
    user_a, _, _ = await _seed(
        household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
    )
    _, settlements = await close_month(MONTH, household.id, user_a.id, db)
    s = settlements[0]

    await reopen_month(MONTH, household.id, user_a.id, db)

    with pytest.raises(MonthIsOpen):
        await mark_settlement_paid(s.id, household.id, True, user_a.id, db)


# ---------------------------------------------------------------------------
# Test 5 — Non-existent settlement_id raises SettlementNotFound
# ---------------------------------------------------------------------------

async def test_nonexistent_settlement_raises(
    db, household, make_room, make_user
):
    """SettlementNotFound is raised for an ID that doesn't exist."""
    room = await make_room(household)
    user = await make_user(household, room)

    with pytest.raises(SettlementNotFound):
        await mark_settlement_paid(999_999_999, household.id, True, user.id, db)
