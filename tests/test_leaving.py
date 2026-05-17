"""Tests for the process-leaving service (app/services/leaving.py)."""
from datetime import date
from decimal import Decimal

import pytest

from app.models.settlement import Month, Settlement
from app.services.asset_svc import create_asset_svc
from app.services.deposit_svc import create_deposit_svc
from app.services.leaving import (
    InvalidLeaveDate,
    LeavingResult,
    UserAlreadyLeft,
    process_leaving_svc,
)
from app.schemas.assets import AssetContributionInput


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _contribs(*pairs) -> list[AssetContributionInput]:
    return [AssetContributionInput(user_id=uid, amount=Decimal(str(amt))) for uid, amt in pairs]


async def _make_closed_month_with_settlement(db, household, from_user, to_user, amount):
    """Insert a closed Month and an unpaid Settlement for it."""
    month = Month(id="2026-01", household_id=household.id, status="closed")
    db.add(month)
    await db.flush()
    s = Settlement(
        month_id="2026-01",
        from_user=from_user.id,
        to_user=to_user.id,
        amount=Decimal(str(amount)),
        paid=False,
    )
    db.add(s)
    await db.flush()
    return s


async def _leave(db, household, user, admin, leave_date=date(2026, 5, 1)):
    return await process_leaving_svc(
        user_id=user.id,
        household_id=household.id,
        leave_date=leave_date,
        admin_id=admin.id,
        db=db,
    )


# ---------------------------------------------------------------------------
# Test 1 — No deposit, no dues, no assets → just sets left_at
# ---------------------------------------------------------------------------

async def test_leave_no_deposit_no_dues_no_assets(db, household, make_room, make_user):
    """Simplest leaving: zero dues, no deposit, no assets."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")

    result = await _leave(db, household, alice, admin)

    assert isinstance(result, LeavingResult)
    assert result.user_id == alice.id
    assert result.left_at == date(2026, 5, 1)
    assert result.dues == Decimal("0")
    assert result.deposit_amount == Decimal("0")
    assert result.deposit_applied_to_dues == Decimal("0")
    assert result.deposit_cash_refund == Decimal("0")
    assert result.remaining_dues == Decimal("0")
    assert result.asset_refunds == []
    assert result.total_cash_owed_to_leaver == Decimal("0")


# ---------------------------------------------------------------------------
# Test 2 — deposit > dues: partial applied, cash returned
# ---------------------------------------------------------------------------

async def test_leave_deposit_greater_than_dues(db, household, make_room, make_user):
    """deposit=5000, dues=1000 → applied=1000, cash_refund=4000, remaining_dues=0."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")

    await create_deposit_svc(
        household_id=household.id,
        user_id=alice.id,
        amount=Decimal("5000"),
        deposited_at=date(2026, 1, 1),
        held_by_user_id=admin.id,
        db=db,
    )
    await _make_closed_month_with_settlement(db, household, from_user=alice, to_user=bob, amount=1000)

    result = await _leave(db, household, alice, admin)

    assert result.dues == Decimal("1000")
    assert result.deposit_amount == Decimal("5000")
    assert result.deposit_applied_to_dues == Decimal("1000")
    assert result.deposit_cash_refund == Decimal("4000")
    assert result.remaining_dues == Decimal("0")
    assert result.asset_refunds == []
    assert result.total_cash_owed_to_leaver == Decimal("4000")


# ---------------------------------------------------------------------------
# Test 3 — deposit < dues: deposit fully consumed, remaining dues remain
# ---------------------------------------------------------------------------

async def test_leave_deposit_less_than_dues(db, household, make_room, make_user):
    """deposit=2000, dues=3000 → applied=2000, cash_refund=0, remaining_dues=1000."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")

    await create_deposit_svc(
        household_id=household.id,
        user_id=alice.id,
        amount=Decimal("2000"),
        deposited_at=date(2026, 1, 1),
        held_by_user_id=admin.id,
        db=db,
    )
    await _make_closed_month_with_settlement(db, household, from_user=alice, to_user=bob, amount=3000)

    result = await _leave(db, household, alice, admin)

    assert result.dues == Decimal("3000")
    assert result.deposit_amount == Decimal("2000")
    assert result.deposit_applied_to_dues == Decimal("2000")
    assert result.deposit_cash_refund == Decimal("0")
    assert result.remaining_dues == Decimal("1000")
    assert result.total_cash_owed_to_leaver == Decimal("0")


# ---------------------------------------------------------------------------
# Test 4 — Asset with buy-in required: AssetRefund has paid_by_user=NULL
# ---------------------------------------------------------------------------

async def test_leave_asset_buyin_required_refund_pending(db, household, make_room, make_user):
    """3 users each contribute 1000 to a fan (buy-in required).
    User A leaves → AssetRefund(amount=1000, paid_by_user=None)."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")

    await create_asset_svc(
        household_id=household.id,
        bought_by_user_id=admin.id,
        name="Fan",
        description=None,
        purchase_date=date(2026, 1, 1),
        total_cost=Decimal("3000"),
        requires_buyin_from_new_members=True,
        contributions=_contribs(
            (admin.id, "1000"),
            (alice.id, "1000"),
            (bob.id, "1000"),
        ),
        photo_url=None,
        db=db,
    )

    result = await _leave(db, household, alice, admin)

    assert len(result.asset_refunds) == 1
    refund = result.asset_refunds[0]
    assert refund.asset_name == "Fan"
    assert refund.amount == Decimal("1000")
    assert refund.paid_by_user is None  # pending — new member will pay later
    assert result.total_cash_owed_to_leaver == Decimal("0")


# ---------------------------------------------------------------------------
# Test 5 — Asset without buy-in: paid_by_user = admin.id
# ---------------------------------------------------------------------------

async def test_leave_asset_no_buyin_admin_assumes_liability(db, household, make_room, make_user):
    """Same 3-user fan, but requires_buyin_from_new_members=False.
    User A leaves → AssetRefund(amount=1000, paid_by_user=admin.id)."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")

    await create_asset_svc(
        household_id=household.id,
        bought_by_user_id=admin.id,
        name="Cooker",
        description=None,
        purchase_date=date(2026, 1, 1),
        total_cost=Decimal("3000"),
        requires_buyin_from_new_members=False,
        contributions=_contribs(
            (admin.id, "1000"),
            (alice.id, "1000"),
            (bob.id, "1000"),
        ),
        photo_url=None,
        db=db,
    )

    result = await _leave(db, household, alice, admin)

    assert len(result.asset_refunds) == 1
    refund = result.asset_refunds[0]
    assert refund.asset_name == "Cooker"
    assert refund.amount == Decimal("1000")
    assert refund.paid_by_user == admin.id  # admin assumes liability immediately
    assert result.total_cash_owed_to_leaver == Decimal("1000")


# ---------------------------------------------------------------------------
# Test 6 — Personal asset (sole contributor) → no refund created
# ---------------------------------------------------------------------------

async def test_leave_sole_contributor_asset_skipped(db, household, make_room, make_user):
    """Alice contributed 500 alone to a personal item → no AssetRefund created."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")

    await create_asset_svc(
        household_id=household.id,
        bought_by_user_id=alice.id,
        name="Personal Lamp",
        description=None,
        purchase_date=date(2026, 1, 1),
        total_cost=Decimal("500"),
        requires_buyin_from_new_members=True,
        contributions=_contribs((alice.id, "500")),
        photo_url=None,
        db=db,
    )

    result = await _leave(db, household, alice, admin)

    assert result.asset_refunds == []
    assert result.total_cash_owed_to_leaver == Decimal("0")


# ---------------------------------------------------------------------------
# Test 7 — Disposed asset → skipped (no refund)
# ---------------------------------------------------------------------------

async def test_leave_disposed_asset_skipped(db, household, make_room, make_user):
    """Asset with status='disposed' generates no refund even if user contributed."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")

    from app.services.asset_svc import dispose_asset_svc
    asset = await create_asset_svc(
        household_id=household.id,
        bought_by_user_id=admin.id,
        name="Broken Fan",
        description=None,
        purchase_date=date(2026, 1, 1),
        total_cost=Decimal("2000"),
        requires_buyin_from_new_members=True,
        contributions=_contribs((admin.id, "1000"), (alice.id, "1000")),
        photo_url=None,
        db=db,
    )
    await dispose_asset_svc(asset.id, household.id, admin.id, db)

    result = await _leave(db, household, alice, admin)

    assert result.asset_refunds == []


# ---------------------------------------------------------------------------
# Test 8 — Future leave_date → InvalidLeaveDate
# ---------------------------------------------------------------------------

async def test_leave_future_date_raises(db, household, make_room, make_user):
    """leave_date in the future raises InvalidLeaveDate."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")

    from datetime import timedelta
    future = date.today() + timedelta(days=1)

    with pytest.raises(InvalidLeaveDate):
        await process_leaving_svc(
            user_id=alice.id,
            household_id=household.id,
            leave_date=future,
            admin_id=admin.id,
            db=db,
        )


# ---------------------------------------------------------------------------
# Test 9 — Already-left user → UserAlreadyLeft
# ---------------------------------------------------------------------------

async def test_leave_already_left_raises(db, household, make_room, make_user):
    """Calling process_leaving_svc on a user who already left raises UserAlreadyLeft."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")

    # First leaving — succeeds
    await _leave(db, household, alice, admin)

    # Second attempt — raises
    with pytest.raises(UserAlreadyLeft):
        await _leave(db, household, alice, admin)
