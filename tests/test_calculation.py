"""Tests for app/services/calculation.py — written BEFORE the engine.

These tests define what "correct" means. The engine must make them pass.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.models.core import Household, Room, User
from app.services.calculation import MonthSummary, calculate_month

MONTH = "2026-05"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def by_id(summary: MonthSummary) -> dict:
    return {u.id: u for u in summary.users}


# ---------------------------------------------------------------------------
# Test 1 — basic meal split, no rent, no utilities
# ---------------------------------------------------------------------------

async def test_basic_meal_split(
    db, household, make_room, make_user, make_shopping_entry, make_meal_log
):
    """9000 BDT of meal shopping split proportionally across A=30, B=60, C=25 meals.

    Invariant: sum(meal_owed) == meal_pool exactly (largest-remainder rounding).
    """
    room = await make_room(household, rent=Decimal("0"), service=Decimal("0"))
    user_a = await make_user(household, room, name="Alice", month=MONTH)
    user_b = await make_user(household, room, name="Bob",   month=MONTH)
    user_c = await make_user(household, room, name="Carol", month=MONTH)

    await make_shopping_entry(household, user_a, MONTH, [
        {"name": "Rice", "price": "9000", "category": "meal"},
    ])

    await make_meal_log(user_a, date(2026, 5, 1), meal_count=30)
    await make_meal_log(user_b, date(2026, 5, 1), meal_count=60)
    await make_meal_log(user_c, date(2026, 5, 1), meal_count=25)

    summary = await calculate_month(MONTH, household.id, db)

    assert summary.meal_pool  == Decimal("9000.00")
    assert summary.total_meals == Decimal("115")

    users = by_id(summary)
    assert users[user_a.id].meal_owed == Decimal("2347.83")
    assert users[user_b.id].meal_owed == Decimal("4695.65")
    assert users[user_c.id].meal_owed == Decimal("1956.52")

    # Penny-perfect invariant — must hold for every month close
    total_meal_owed = sum(u.meal_owed for u in summary.users)
    assert total_meal_owed == Decimal("9000.00"), (
        f"meal_owed sum {total_meal_owed} != pool 9000.00 — rounding lost a paisa"
    )


# ---------------------------------------------------------------------------
# Test 2 — rent split per room
# ---------------------------------------------------------------------------

async def test_rent_split_per_room(db, household, make_room, make_user):
    """Room 1 (A alone): 5000+300=5300. Room 2 (B+C shared): 6000+400=6400 / 2 = 3200 each."""
    room1 = await make_room(household, rent=Decimal("5000"), service=Decimal("300"), name="Room 1")
    room2 = await make_room(household, rent=Decimal("6000"), service=Decimal("400"), name="Room 2")

    user_a = await make_user(household, room1, name="Alice", month=MONTH)
    user_b = await make_user(household, room2, name="Bob",   month=MONTH)
    user_c = await make_user(household, room2, name="Carol", month=MONTH)

    summary = await calculate_month(MONTH, household.id, db)

    users = by_id(summary)
    assert users[user_a.id].rent_owed == Decimal("5300.00")
    assert users[user_b.id].rent_owed == Decimal("3200.00")
    assert users[user_c.id].rent_owed == Decimal("3200.00")


# ---------------------------------------------------------------------------
# Test 3 — penny-perfect: 10000 / 3 must total exactly 10000.00
# ---------------------------------------------------------------------------

async def test_penny_perfect_rent(db, household, make_room, make_user):
    """10000 / 3 = 3333.333...; largest-remainder must give exactly one 3333.34."""
    room = await make_room(household, rent=Decimal("10000"), service=Decimal("0"))

    user_a = await make_user(household, room, name="Alice", month=MONTH)
    user_b = await make_user(household, room, name="Bob",   month=MONTH)
    user_c = await make_user(household, room, name="Carol", month=MONTH)

    summary = await calculate_month(MONTH, household.id, db)

    users = by_id(summary)
    rents = sorted(users[u.id].rent_owed for u in [user_a, user_b, user_c])

    # Total is exact
    assert sum(rents) == Decimal("10000.00"), (
        f"rent sum {sum(rents)} != 10000.00 — paisa lost to rounding"
    )
    # Distribution: two users pay 3333.33, one pays 3333.34
    assert rents == [Decimal("3333.33"), Decimal("3333.33"), Decimal("3333.34")]


# ---------------------------------------------------------------------------
# Test 4 — _distribute unit test: precision, edge cases
# ---------------------------------------------------------------------------

def test_distribute_no_precision_loss():
    """Direct unit test of _distribute — verifies penny-perfect on awkward inputs."""
    from app.services.calculation import ZERO, _distribute

    # Three identical weights summing to 100.00 → 33.33 / 33.33 / 33.34
    result = _distribute(Decimal("100.00"), [Decimal("1")] * 3)
    assert sum(result) == Decimal("100.00")
    assert sorted(result) == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]

    # Awkward weights with a small residual
    result = _distribute(Decimal("10.00"), [Decimal("1"), Decimal("2"), Decimal("3")])
    assert sum(result) == Decimal("10.00")

    # Zero total
    result = _distribute(ZERO, [Decimal("1"), Decimal("2")])
    assert result == [ZERO, ZERO]

    # Single item gets everything
    result = _distribute(Decimal("99.99"), [Decimal("1")])
    assert result == [Decimal("99.99")]
