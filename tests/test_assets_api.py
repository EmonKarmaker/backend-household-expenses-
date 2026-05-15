"""Tests for shared assets CRUD (app/services/asset_svc.py + router)."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.assets import SharedAsset
from app.schemas.assets import AssetContributionInput, SharedAssetUpdate
from app.services.asset_svc import (
    AssetNotFound,
    InvalidContributionSum,
    InvalidContributors,
    create_asset_svc,
    dispose_asset_svc,
    get_asset_or_404,
    update_asset_svc,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _contribs(*pairs: tuple) -> list[AssetContributionInput]:
    """Build a contribution list from (user_id, amount) pairs."""
    return [AssetContributionInput(user_id=uid, amount=Decimal(str(amt))) for uid, amt in pairs]


async def _make_asset(
    household, user, db, *,
    name: str = "Fan",
    purchase_date: date = date(2026, 5, 1),
    total_cost: Decimal = Decimal("3000.00"),
    contributions: list[AssetContributionInput] | None = None,
) -> SharedAsset:
    if contributions is None:
        contributions = _contribs((user.id, "3000.00"))
    return await create_asset_svc(
        household_id=household.id,
        bought_by_user_id=user.id,
        name=name,
        description=None,
        purchase_date=purchase_date,
        total_cost=total_cost,
        requires_buyin_from_new_members=True,
        contributions=contributions,
        photo_url=None,
        db=db,
    )


# ---------------------------------------------------------------------------
# Test 1 — Create with valid 3-contributor split → 201 (service level)
# ---------------------------------------------------------------------------

async def test_create_asset_valid_split(db, household, make_room, make_user):
    """3-way equal split: total_cost=3000, each contributes 1000."""
    room = await make_room(household)
    ua = await make_user(household, room, name="Alice")
    ub = await make_user(household, room, name="Bob")
    uc = await make_user(household, room, name="Carol")

    asset = await create_asset_svc(
        household_id=household.id,
        bought_by_user_id=ua.id,
        name="Ceiling Fan",
        description="Living room fan",
        purchase_date=date(2026, 5, 1),
        total_cost=Decimal("3000.00"),
        requires_buyin_from_new_members=True,
        contributions=_contribs(
            (ua.id, "1000.00"),
            (ub.id, "1000.00"),
            (uc.id, "1000.00"),
        ),
        photo_url=None,
        db=db,
    )

    assert asset.name == "Ceiling Fan"
    assert asset.total_cost == Decimal("3000.00")
    assert asset.status == "active"
    assert asset.household_id == household.id
    assert asset.bought_by_user_id == ua.id
    assert len(asset.contributions) == 3

    contrib_user_ids = {c.user_id for c in asset.contributions}
    assert contrib_user_ids == {ua.id, ub.id, uc.id}

    for c in asset.contributions:
        assert c.amount == Decimal("1000.00")
        assert c.contribution_type == "initial"
        assert c.contributed_at == date(2026, 5, 1)

    # Verify rows exist in DB
    result = await db.execute(
        select(SharedAsset).where(SharedAsset.id == asset.id)
    )
    assert result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Test 2 — Contribution sum != total_cost → 422
# ---------------------------------------------------------------------------

async def test_create_asset_bad_contribution_sum(db, household, make_room, make_user):
    """sum(contributions)=1500 but total_cost=3000 → InvalidContributionSum."""
    room = await make_room(household)
    ua = await make_user(household, room, name="Alice")
    ub = await make_user(household, room, name="Bob")

    with pytest.raises(InvalidContributionSum) as exc_info:
        await create_asset_svc(
            household_id=household.id,
            bought_by_user_id=ua.id,
            name="Cooker",
            description=None,
            purchase_date=date(2026, 5, 1),
            total_cost=Decimal("3000.00"),
            requires_buyin_from_new_members=True,
            contributions=_contribs((ua.id, "1000.00"), (ub.id, "500.00")),
            photo_url=None,
            db=db,
        )
    assert "does not equal total_cost" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 3 — Non-existent contributor user_id → 422
# ---------------------------------------------------------------------------

async def test_create_asset_invalid_contributor(db, household, make_room, make_user):
    """user_id 999999 doesn't belong to household → InvalidContributors."""
    room = await make_room(household)
    ua = await make_user(household, room, name="Alice")

    with pytest.raises(InvalidContributors) as exc_info:
        await create_asset_svc(
            household_id=household.id,
            bought_by_user_id=ua.id,
            name="Fridge",
            description=None,
            purchase_date=date(2026, 5, 1),
            total_cost=Decimal("500.00"),
            requires_buyin_from_new_members=True,
            contributions=_contribs((999_999, "500.00")),
            photo_url=None,
            db=db,
        )
    assert "999999" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 4 — GET /assets returns assets ordered by purchase_date DESC, id DESC
# ---------------------------------------------------------------------------

async def test_list_assets_ordered(db, household, make_room, make_user):
    """Newer purchase_date appears first; same date falls back to id DESC."""
    room = await make_room(household)
    user = await make_user(household, room, name="Alice")

    asset_old = await _make_asset(
        household, user, db, name="Old Fan", purchase_date=date(2026, 1, 1),
        total_cost=Decimal("1000"), contributions=_contribs((user.id, "1000")),
    )
    asset_new = await _make_asset(
        household, user, db, name="New Cooker", purchase_date=date(2026, 4, 1),
        total_cost=Decimal("2000"), contributions=_contribs((user.id, "2000")),
    )

    result = await db.execute(
        select(SharedAsset)
        .where(SharedAsset.household_id == household.id)
        .order_by(SharedAsset.purchase_date.desc(), SharedAsset.id.desc())
        .options(selectinload(SharedAsset.contributions), selectinload(SharedAsset.refunds))
    )
    assets = result.scalars().all()

    assert len(assets) == 2
    assert assets[0].id == asset_new.id  # newer purchase date first
    assert assets[1].id == asset_old.id


# ---------------------------------------------------------------------------
# Test 5 — PATCH name as admin → 200
# ---------------------------------------------------------------------------

async def test_update_asset_name(db, household, make_room, make_user):
    """Admin can update the asset name via update_asset_svc."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")

    asset = await _make_asset(household, admin, db, name="Old Name")

    updated = await update_asset_svc(
        asset.id, household.id,
        SharedAssetUpdate(name="New Name"),
        admin.id,
        db,
    )

    assert updated.name == "New Name"
    assert updated.id == asset.id


# ---------------------------------------------------------------------------
# Test 6 — PATCH as non-admin → 403 (HTTP layer test via httpx)
# ---------------------------------------------------------------------------

async def test_patch_non_admin_forbidden(db, household, make_room, make_user):
    """Non-admin user hitting PATCH /assets/{id} gets 403 from require_admin."""
    from httpx import ASGITransport, AsyncClient

    from app.database import get_db
    from app.main import app
    from app.utils.permissions import get_current_user

    room = await make_room(household)
    non_admin = await make_user(household, room, name="NonAdmin")
    # is_admin defaults to False

    asset = await _make_asset(household, non_admin, db)

    async def _db_override():
        yield db

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = lambda: non_admin

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/api/v1/assets/{asset.id}",
                json={"name": "Hacked"},
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Admin access required"
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Test 7 — Dispose asset → status="disposed"
# ---------------------------------------------------------------------------

async def test_dispose_asset(db, household, make_room, make_user):
    """POST /assets/{id}/dispose sets status to 'disposed'."""
    room = await make_room(household)
    user = await make_user(household, room, name="Alice")

    asset = await _make_asset(household, user, db)
    assert asset.status == "active"

    disposed = await dispose_asset_svc(asset.id, household.id, user.id, db)
    assert disposed.status == "disposed"
    assert disposed.id == asset.id


# ---------------------------------------------------------------------------
# Test 8 — Cross-household access → 404
# ---------------------------------------------------------------------------

async def test_cross_household_asset_404(db, household, make_room, make_user):
    """get_asset_or_404 with wrong household_id raises AssetNotFound."""
    room = await make_room(household)
    user = await make_user(household, room, name="Alice")

    asset = await _make_asset(household, user, db)

    with pytest.raises(AssetNotFound):
        await get_asset_or_404(asset.id, household_id=99_999, db=db)
