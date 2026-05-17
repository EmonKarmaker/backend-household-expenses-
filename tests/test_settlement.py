"""Tests for app/services/settlement.py."""
from decimal import Decimal

import pytest

from app.services.settlement import ZERO, Transfer, minimize_transfers


# ---------------------------------------------------------------------------
# Test 1 — three users, classic case
# ---------------------------------------------------------------------------

def test_three_users_classic():
    """User 1 is owed 100; users 2 and 3 each owe part of it."""
    balances = {1: Decimal("100"), 2: Decimal("-60"), 3: Decimal("-40")}
    transfers = minimize_transfers(balances)

    assert len(transfers) == 2
    assert sum(t.amount for t in transfers) == Decimal("100")

    # Verify settlement correctness by replaying
    result = dict(balances)
    for t in transfers:
        result[t.from_user_id] += t.amount
        result[t.to_user_id]   -= t.amount
    assert all(bal == ZERO for bal in result.values())


# ---------------------------------------------------------------------------
# Test 2 — already settled
# ---------------------------------------------------------------------------

def test_already_settled():
    balances = {1: ZERO, 2: ZERO, 3: ZERO}
    assert minimize_transfers(balances) == []


# ---------------------------------------------------------------------------
# Test 3 — two users, single transfer
# ---------------------------------------------------------------------------

def test_two_users():
    balances = {1: Decimal("500"), 2: Decimal("-500")}
    transfers = minimize_transfers(balances)

    assert len(transfers) == 1
    assert transfers[0] == Transfer(from_user_id=2, to_user_id=1, amount=Decimal("500"))


# ---------------------------------------------------------------------------
# Test 4 — imbalanced input raises ValueError
# ---------------------------------------------------------------------------

def test_imbalanced_raises():
    with pytest.raises(ValueError, match="balances do not sum to zero"):
        minimize_transfers({1: Decimal("100"), 2: Decimal("-50")})


# ---------------------------------------------------------------------------
# Test 5 — realistic four-person scenario
# ---------------------------------------------------------------------------

def test_realistic_four_person():
    """1 creditor, 3 debtors → at most N-1 = 3 transfers; all balances reach zero."""
    balances = {
        1: Decimal("1200"),
        2: Decimal("-500"),
        3: Decimal("-300"),
        4: Decimal("-400"),
    }
    transfers = minimize_transfers(balances)

    assert len(transfers) <= 3

    # Replay transfers and verify everyone is settled
    result = dict(balances)
    for t in transfers:
        result[t.from_user_id] += t.amount
        result[t.to_user_id]   -= t.amount
    for bal in result.values():
        assert bal == ZERO
