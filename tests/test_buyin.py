"""Tests for the asset buy-in service (app/services/buyin.py)."""
from datetime import date
from decimal import Decimal

import pytest

from app.models.assets import SharedAsset
from app.schemas.assets import AssetContributionInput
from app.services.asset_svc import create_asset_svc, dispose_asset_svc
from app.services.buyin import (
    AssetNotActive,
    AssetNotInHousehold,
    BuyinResult,
    InvalidBuyinAmount,
    NoPendingRefunds,
    UserAlreadyLeft,
    UserNotInHousehold,
    buyin_to_asset_svc,
)
from app.services.leaving import process_leaving_svc


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _contribs(*pairs) -> list[AssetContributionInput]:
    return [AssetContributionInput(user_id=uid, amount=Decimal(str(amt))) for uid, amt in pairs]


async def _create_fan(db, household, admin, *contributors, requires_buyin=True):
    n = len(contributors)
    share = Decimal("1000")
    contribs = _contribs(*[(u.id, share) for u in contributors])
    return await create_asset_svc(
        household_id=household.id,
        bought_by_user_id=admin.id,
        name="Fan",
        description=None,
        purchase_date=date(2026, 1, 1),
        total_cost=share * n,
        requires_buyin_from_new_members=requires_buyin,
        contributions=contribs,
        photo_url=None,
        db=db,
    )


async def _buyin(db, household, admin, new_member, asset, amount=Decimal("1000")):
    return await buyin_to_asset_svc(
        new_member_id=new_member.id,
        household_id=household.id,
        asset_id=asset.id,
        amount=amount,
        admin_id=admin.id,
        db=db,
    )


async def _leave(db, household, admin, user):
    return await process_leaving_svc(
        user_id=user.id,
        household_id=household.id,
        leave_date=date(2026, 5, 1),
        admin_id=admin.id,
        db=db,
    )


# ---------------------------------------------------------------------------
# Test 1 — Happy path: one pending refund is fulfilled
# ---------------------------------------------------------------------------

async def test_buyin_basic(db, household, make_room, make_user):
    """New member buys into an asset that has one pending refund."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")
    charlie = await make_user(household, room, name="Charlie")

    # Admin + Alice + Bob each contribute 1000; buy-in required
    asset = await _create_fan(db, household, admin, admin, alice, bob)

    # Alice leaves → pending refund (paid_by_user=None)
    await _leave(db, household, admin, alice)

    # Charlie buys in
    result = await _buyin(db, household, admin, charlie, asset)

    assert isinstance(result, BuyinResult)
    assert result.amount == Decimal("1000")
    assert len(result.fulfilled_refunds) == 1
    assert result.contribution_id > 0


# ---------------------------------------------------------------------------
# Test 2 — Multiple pending refunds all fulfilled
# ---------------------------------------------------------------------------

async def test_buyin_fulfils_multiple_refunds(db, household, make_room, make_user):
    """Two members leave; a single buyer fulfils both pending refunds."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")
    charlie = await make_user(household, room, name="Charlie")

    asset = await _create_fan(db, household, admin, admin, alice, bob)

    await _leave(db, household, admin, alice)
    await _leave(db, household, admin, bob)

    result = await _buyin(db, household, admin, charlie, asset, amount=Decimal("2000"))

    assert len(result.fulfilled_refunds) == 2


# ---------------------------------------------------------------------------
# Test 3 — User not in household → UserNotInHousehold
# ---------------------------------------------------------------------------

async def test_buyin_user_not_in_household(db, household, make_room, make_user):
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")

    asset = await _create_fan(db, household, admin, admin, alice)
    await _leave(db, household, admin, alice)

    with pytest.raises(UserNotInHousehold):
        await buyin_to_asset_svc(
            new_member_id=999_999,
            household_id=household.id,
            asset_id=asset.id,
            amount=Decimal("1000"),
            admin_id=admin.id,
            db=db,
        )


# ---------------------------------------------------------------------------
# Test 4 — User already left → UserAlreadyLeft
# ---------------------------------------------------------------------------

async def test_buyin_user_already_left(db, household, make_room, make_user):
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")
    charlie = await make_user(household, room, name="Charlie")

    asset = await _create_fan(db, household, admin, admin, alice, bob)
    await _leave(db, household, admin, alice)
    # Charlie leaves too before buying in
    await _leave(db, household, admin, charlie)

    with pytest.raises(UserAlreadyLeft):
        await _buyin(db, household, admin, charlie, asset)


# ---------------------------------------------------------------------------
# Test 5 — Asset not in household → AssetNotInHousehold
# ---------------------------------------------------------------------------

async def test_buyin_asset_not_in_household(db, household, make_room, make_user):
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    charlie = await make_user(household, room, name="Charlie")

    with pytest.raises(AssetNotInHousehold):
        await buyin_to_asset_svc(
            new_member_id=charlie.id,
            household_id=household.id,
            asset_id=999_999,
            amount=Decimal("1000"),
            admin_id=admin.id,
            db=db,
        )


# ---------------------------------------------------------------------------
# Test 6 — Asset disposed → AssetNotActive
# ---------------------------------------------------------------------------

async def test_buyin_asset_disposed(db, household, make_room, make_user):
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    charlie = await make_user(household, room, name="Charlie")

    asset = await _create_fan(db, household, admin, admin, alice)
    await _leave(db, household, admin, alice)
    await dispose_asset_svc(asset.id, household.id, admin.id, db)

    with pytest.raises(AssetNotActive):
        await _buyin(db, household, admin, charlie, asset)


# ---------------------------------------------------------------------------
# Test 7 — Amount ≤ 0 → InvalidBuyinAmount
# ---------------------------------------------------------------------------

async def test_buyin_invalid_amount(db, household, make_room, make_user):
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    charlie = await make_user(household, room, name="Charlie")

    asset = await _create_fan(db, household, admin, admin, alice)
    await _leave(db, household, admin, alice)

    with pytest.raises(InvalidBuyinAmount):
        await _buyin(db, household, admin, charlie, asset, amount=Decimal("0"))


# ---------------------------------------------------------------------------
# Test 8 — No pending refunds → NoPendingRefunds
# ---------------------------------------------------------------------------

async def test_buyin_no_pending_refunds(db, household, make_room, make_user):
    """Asset with no pending refunds raises NoPendingRefunds."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")

    # Asset with only admin — no leaving, so no pending refunds
    asset = await _create_fan(db, household, admin, admin)
    # alice never left, so asset.refunds is empty
    with pytest.raises(NoPendingRefunds):
        await _buyin(db, household, admin, alice, asset)


# ---------------------------------------------------------------------------
# Test 9 — End-to-end lifecycle: leaver's refund row points to the buyer
# ---------------------------------------------------------------------------

async def test_buyin_end_to_end_lifecycle(db, household, make_room, make_user):
    """A and B co-own a fan. B leaves (pending refund). C buys in.
    Verify B's AssetRefund row now has C as paid_by_user AND replaced_by_user_id,
    and a new buyin-type AssetContribution exists for C.
    """
    from app.models.assets import AssetContribution, AssetRefund
    from sqlalchemy import select

    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")
    charlie = await make_user(household, room, name="Charlie")

    # Admin + Alice + Bob each put in 1000, buy-in required
    asset = await _create_fan(db, household, admin, admin, alice, bob)

    # Bob leaves → pending refund of 1000, paid_by_user=None
    await _leave(db, household, admin, bob)

    # Verify the pending refund exists and is unfulfilled
    result = await db.execute(
        select(AssetRefund).where(
            AssetRefund.asset_id == asset.id,
            AssetRefund.user_id == bob.id,
        )
    )
    bob_refund = result.scalar_one()
    assert bob_refund.amount == Decimal("1000")
    assert bob_refund.paid_by_user is None
    assert bob_refund.replaced_by_user_id is None

    # Charlie buys in
    buyin_result = await _buyin(db, household, admin, charlie, asset, amount=Decimal("1000"))

    # Re-fetch Bob's refund — it must now point to Charlie
    await db.refresh(bob_refund)
    assert bob_refund.paid_by_user == charlie.id, (
        "Leaver's refund must be payable by the new member after buy-in"
    )
    assert bob_refund.replaced_by_user_id == charlie.id, (
        "Leaver's refund must record who replaced them"
    )

    # Charlie's buyin contribution must exist with the right type
    result = await db.execute(
        select(AssetContribution).where(
            AssetContribution.id == buyin_result.contribution_id
        )
    )
    charlie_contrib = result.scalar_one()
    assert charlie_contrib.user_id == charlie.id
    assert charlie_contrib.asset_id == asset.id
    assert charlie_contrib.amount == Decimal("1000")
    assert charlie_contrib.contribution_type == "buyin"

    # The fulfilled_refunds list must reference Bob's actual refund row
    assert bob_refund.id in buyin_result.fulfilled_refunds
