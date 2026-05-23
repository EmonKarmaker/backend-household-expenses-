"""Tests for app/services/calculation.py — written BEFORE the engine.

These tests define what "correct" means. The engine must make them pass.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.models.core import Household, Room, User
from app.services.calculation import MonthSummary, calculate_month
from app.services.month_close import close_month

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

    assert summary.status == "open"
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


# ---------------------------------------------------------------------------
# Test 5 — utility bills split equally
# ---------------------------------------------------------------------------

async def test_utility_bills_split_equally(
    db, household, make_room, make_user, make_bill
):
    """2700 BDT in bills (A paid 1500 electricity, B paid 1200 internet) split 3 ways."""
    room = await make_room(household, rent=Decimal("0"), service=Decimal("0"))
    user_a = await make_user(household, room, name="Alice", month=MONTH)
    user_b = await make_user(household, room, name="Bob",   month=MONTH)
    user_c = await make_user(household, room, name="Carol", month=MONTH)

    await make_bill(household, user_a, MONTH, Decimal("1500"), "electricity")
    await make_bill(household, user_b, MONTH, Decimal("1200"), "internet")

    summary = await calculate_month(MONTH, household.id, db)
    users = by_id(summary)

    # 2700 / 3 = 900.00 exactly — no rounding residual
    assert users[user_a.id].utility_owed == Decimal("900.00")
    assert users[user_b.id].utility_owed == Decimal("900.00")
    assert users[user_c.id].utility_owed == Decimal("900.00")

    total_utility_owed = sum(u.utility_owed for u in summary.users)
    assert total_utility_owed == Decimal("2700.00")

    assert users[user_a.id].total_paid == Decimal("1500.00")
    assert users[user_b.id].total_paid == Decimal("1200.00")
    assert users[user_c.id].total_paid == Decimal("0.00")

    # A paid 1500, owes 900 → net +600
    assert users[user_a.id].balance == Decimal("600.00")


# ---------------------------------------------------------------------------
# Test 6 — household shopping items split equally
# ---------------------------------------------------------------------------

async def test_household_items_split_equally(
    db, household, make_room, make_user, make_shopping_entry
):
    """300 BDT of household items (paid by A) split equally → 100 each."""
    room = await make_room(household, rent=Decimal("0"), service=Decimal("0"))
    user_a = await make_user(household, room, name="Alice", month=MONTH)
    user_b = await make_user(household, room, name="Bob",   month=MONTH)
    user_c = await make_user(household, room, name="Carol", month=MONTH)

    await make_shopping_entry(household, user_a, MONTH, [
        {"name": "Vim liquid",     "price": "200", "category": "household"},
        {"name": "Toilet paper",   "price": "100", "category": "household"},
    ])

    summary = await calculate_month(MONTH, household.id, db)
    users = by_id(summary)

    assert users[user_a.id].household_owed == Decimal("100.00")
    assert users[user_b.id].household_owed == Decimal("100.00")
    assert users[user_c.id].household_owed == Decimal("100.00")

    assert sum(u.household_owed for u in summary.users) == Decimal("300.00")

    assert users[user_a.id].total_paid == Decimal("300.00")
    # A paid 300, owes 100 → net +200
    assert users[user_a.id].balance == Decimal("200.00")


# ---------------------------------------------------------------------------
# Test 7 — personal items charged only to target user
# ---------------------------------------------------------------------------

async def test_personal_items_charged_to_target(
    db, household, make_room, make_user, make_shopping_entry
):
    """Shampoo 250 → B, Snacks 80 → A. C owes nothing personal."""
    room = await make_room(household, rent=Decimal("0"), service=Decimal("0"))
    user_a = await make_user(household, room, name="Alice", month=MONTH)
    user_b = await make_user(household, room, name="Bob",   month=MONTH)
    user_c = await make_user(household, room, name="Carol", month=MONTH)

    await make_shopping_entry(household, user_a, MONTH, [
        {"name": "Shampoo", "price": "250", "category": "personal",
         "target_user_id": user_b.id},
        {"name": "Snacks",  "price": "80",  "category": "personal",
         "target_user_id": user_a.id},
    ])

    summary = await calculate_month(MONTH, household.id, db)
    users = by_id(summary)

    assert users[user_b.id].personal_owed == Decimal("250.00")
    assert users[user_a.id].personal_owed == Decimal("80.00")
    assert users[user_c.id].personal_owed == Decimal("0.00")

    assert users[user_a.id].total_paid == Decimal("330.00")

    assert summary.meal_pool == Decimal("0.00")
    assert users[user_a.id].household_owed == Decimal("0.00")
    assert users[user_b.id].household_owed == Decimal("0.00")
    assert users[user_c.id].household_owed == Decimal("0.00")


# ---------------------------------------------------------------------------
# Test 8 — mixed realistic scenario, end-to-end invariants
# ---------------------------------------------------------------------------

async def test_mixed_realistic_scenario(
    db, household, make_room, make_user, make_shopping_entry, make_meal_log, make_bill
):
    """Full scenario: 2 rooms, rent, utilities, mixed shopping, meal logs.

    Meal weights: A=20, B=40, C=30 (total 90), pool=6000.
      raw_a = 1333.33..., raw_b = 2666.66..., raw_c = 2000.00
      B gets the residual penny → A=1333.33, B=2666.67, C=2000.00
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

    summary = await calculate_month(MONTH, household.id, db)
    users = by_id(summary)

    # --- Pool invariants ---
    assert sum(u.rent_owed      for u in summary.users) == Decimal("11700.00")
    assert sum(u.utility_owed   for u in summary.users) == Decimal("2700.00")
    assert sum(u.household_owed for u in summary.users) == Decimal("300.00")
    assert sum(u.personal_owed  for u in summary.users) == Decimal("200.00")
    assert sum(u.meal_owed      for u in summary.users) == Decimal("6000.00")

    # --- Per-user rent ---
    assert users[user_a.id].rent_owed == Decimal("5300.00")
    assert users[user_b.id].rent_owed == Decimal("3200.00")
    assert users[user_c.id].rent_owed == Decimal("3200.00")

    # --- Per-user utility (900 each, exact) ---
    assert users[user_a.id].utility_owed == Decimal("900.00")
    assert users[user_b.id].utility_owed == Decimal("900.00")
    assert users[user_c.id].utility_owed == Decimal("900.00")

    # --- Meal split: A=1333.33, B=2666.67 (gets residual penny), C=2000.00 ---
    assert users[user_a.id].meal_owed == Decimal("1333.33")
    assert users[user_b.id].meal_owed == Decimal("2666.67")
    assert users[user_c.id].meal_owed == Decimal("2000.00")

    # --- Personal: only B owes the shampoo ---
    assert users[user_b.id].personal_owed == Decimal("200.00")
    assert users[user_a.id].personal_owed == Decimal("0.00")
    assert users[user_c.id].personal_owed == Decimal("0.00")

    # --- Paid totals ---
    # A: 1500 (util) + 4500+1500+300+200 (shopping) = 8000
    assert users[user_a.id].total_paid == Decimal("8000.00")
    assert users[user_b.id].total_paid == Decimal("1200.00")
    assert users[user_c.id].total_paid == Decimal("0.00")

    # --- Balance sum: rent is owed to landlord but not tracked in total_paid,
    #     so sum(balances) == -sum(rent_owed) == -11700, not zero. ---
    balance_sum = sum(u.balance for u in summary.users)
    assert balance_sum == Decimal("-11700.00")


# ---------------------------------------------------------------------------
# Test 9 — settlement_balance sums to zero (the invariant minimize_transfers needs)
# ---------------------------------------------------------------------------

async def test_settlement_balance_sums_to_zero(
    db, household, make_room, make_user, make_shopping_entry, make_meal_log, make_bill
):
    """Same scenario as test_mixed_realistic, but assert the SETTLEMENT balance
    (excluding rent) sums to zero — this is the invariant minimize_transfers() requires.
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

    summary = await calculate_month(MONTH, household.id, db)
    users = by_id(summary)

    # Settlement zone (without rent) must sum to zero — this is what
    # minimize_transfers consumes.
    total_settlement_balance = sum(u.settlement_balance for u in summary.users)
    assert total_settlement_balance == Decimal("0.00"), (
        f"settlement_balance sum {total_settlement_balance} != 0.00 — "
        "money cannot appear or disappear in the settlement zone"
    )

    # Settlement owed:
    # All users:  900 (util) + 100 (household)  = 1000
    # User B:    + 200 (personal shampoo)        = 1200
    # Plus meal: A=1333.33, B=2666.67, C=2000
    #
    # A: 1000 + 1333.33 = 2333.33
    # B: 1200 + 2666.67 = 3866.67
    # C: 1000 + 2000.00 = 3000.00
    assert users[user_a.id].settlement_owed == Decimal("2333.33")
    assert users[user_b.id].settlement_owed == Decimal("3866.67")
    assert users[user_c.id].settlement_owed == Decimal("3000.00")

    # Settlement balance = paid - settlement_owed
    #   A: 8000 - 2333.33 = +5666.67
    #   B: 1200 - 3866.67 = -2666.67
    #   C:    0 - 3000.00 = -3000.00
    # Sum: +5666.67 - 2666.67 - 3000.00 = 0.00 ✓
    assert users[user_a.id].settlement_balance == Decimal("5666.67")
    assert users[user_b.id].settlement_balance == Decimal("-2666.67")
    assert users[user_c.id].settlement_balance == Decimal("-3000.00")


# ---------------------------------------------------------------------------
# Test 10 — settlement_balance feeds directly into minimize_transfers
# ---------------------------------------------------------------------------

async def test_settlement_balance_feeds_minimize_transfers(
    db, household, make_room, make_user, make_shopping_entry, make_meal_log, make_bill
):
    """End-to-end: calculation engine output → settlement minimization."""
    from app.services.settlement import minimize_transfers

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

    summary = await calculate_month(MONTH, household.id, db)

    # Feed settlement_balance directly into minimize_transfers — must not raise
    settlement_balances = {u.id: u.settlement_balance for u in summary.users}
    transfers = minimize_transfers(settlement_balances)

    # At most N-1 = 2 transfers for 3 users
    assert len(transfers) <= 2

    # Replay transfers and verify everyone reaches zero
    result = dict(settlement_balances)
    for t in transfers:
        result[t.from_user_id] += t.amount
        result[t.to_user_id]   -= t.amount
    for bal in result.values():
        assert bal == Decimal("0.00")


# ---------------------------------------------------------------------------
# Test 11 — summary status reflects open/closed Month row
# ---------------------------------------------------------------------------

async def test_summary_status_open_and_closed(
    db, household, make_room, make_user, make_bill, make_shopping_entry, make_meal_log
):
    """status == 'open' before close, 'closed' after close."""
    room = await make_room(household, rent=Decimal("0"), service=Decimal("0"))
    user_a = await make_user(household, room, name="Alice", month=MONTH)
    user_b = await make_user(household, room, name="Bob",   month=MONTH)

    await make_bill(household, user_a, MONTH, Decimal("1000"), "electricity")
    await make_meal_log(user_a, date(2026, 5, 1), meal_count=10)
    await make_meal_log(user_b, date(2026, 5, 1), meal_count=10)

    open_summary = await calculate_month(MONTH, household.id, db)
    assert open_summary.status == "open"

    await close_month(MONTH, household.id, user_a.id, db)

    closed_summary = await calculate_month(MONTH, household.id, db)
    assert closed_summary.status == "closed"
