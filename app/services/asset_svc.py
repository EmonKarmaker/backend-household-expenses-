"""Shared asset business logic — create, update, dispose.

Extracted from the router so tests can call these directly.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.assets import AssetContribution, SharedAsset
from app.models.core import User
from app.schemas.assets import AssetContributionInput, SharedAssetUpdate
from app.utils.audit import log_audit, model_to_dict

ZERO = Decimal("0")
CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------

class AssetNotFound(Exception):
    pass


class InvalidTotalCost(Exception):
    pass


class InvalidContributionSum(Exception):
    pass


class InvalidContributors(Exception):
    pass


# ---------------------------------------------------------------------------
# Shared loader
# ---------------------------------------------------------------------------

async def get_asset_or_404(
    asset_id: int,
    household_id: int,
    db: AsyncSession,
) -> SharedAsset:
    result = await db.execute(
        select(SharedAsset)
        .where(
            SharedAsset.id == asset_id,
            SharedAsset.household_id == household_id,
        )
        .options(
            selectinload(SharedAsset.contributions),
            selectinload(SharedAsset.refunds),
        )
    )
    asset = result.scalar_one_or_none()
    if asset is None:
        raise AssetNotFound(f"Asset {asset_id} not found")
    return asset


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create_asset_svc(
    *,
    household_id: int,
    bought_by_user_id: int,
    name: str,
    description: str | None,
    purchase_date: date,
    total_cost: Decimal,
    requires_buyin_from_new_members: bool,
    contributions: list[AssetContributionInput],
    photo_url: str | None,
    db: AsyncSession,
) -> SharedAsset:
    if total_cost <= ZERO:
        raise InvalidTotalCost("total_cost must be positive")

    if not contributions:
        raise InvalidContributionSum("At least one contribution is required")

    contrib_sum = sum(c.amount for c in contributions)
    if abs(contrib_sum - total_cost) > CENT:
        raise InvalidContributionSum(
            f"Contributions sum ({contrib_sum}) does not equal total_cost ({total_cost})"
        )

    requested_ids = {c.user_id for c in contributions}
    result = await db.execute(
        select(User.id).where(
            User.id.in_(requested_ids),
            User.household_id == household_id,
            User.left_at.is_(None),
        )
    )
    valid_ids = {row[0] for row in result.all()}
    invalid = requested_ids - valid_ids
    if invalid:
        raise InvalidContributors(
            f"User IDs not found in household or inactive: {sorted(invalid)}"
        )

    asset = SharedAsset(
        household_id=household_id,
        name=name,
        description=description,
        purchase_date=purchase_date,
        total_cost=total_cost,
        requires_buyin_from_new_members=requires_buyin_from_new_members,
        status="active",
        bought_by_user_id=bought_by_user_id,
        photo_url=photo_url,
    )
    db.add(asset)
    await db.flush()

    contrib_rows: list[AssetContribution] = []
    for c in contributions:
        row = AssetContribution(
            asset_id=asset.id,
            user_id=c.user_id,
            amount=c.amount,
            contributed_at=purchase_date,
            contribution_type="initial",
        )
        db.add(row)
        contrib_rows.append(row)
    await db.flush()

    await log_audit(
        db, action="create", entity="SharedAsset",
        entity_id=asset.id, user_id=bought_by_user_id,
        after=model_to_dict(asset),
    )
    for row in contrib_rows:
        await log_audit(
            db, action="create", entity="AssetContribution",
            entity_id=row.id, user_id=bought_by_user_id,
            after=model_to_dict(row),
        )

    # Expire so the re-fetch below picks up server-side created_at / updated_at.
    # Capture id first — expire marks ALL attributes (including id) as stale.
    asset_id = asset.id
    db.expire(asset)
    return await get_asset_or_404(asset_id, household_id, db)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

async def update_asset_svc(
    asset_id: int,
    household_id: int,
    updates: SharedAssetUpdate,
    user_id: int,
    db: AsyncSession,
) -> SharedAsset:
    asset = await get_asset_or_404(asset_id, household_id, db)
    before = model_to_dict(asset)

    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(asset, field, value)
    await db.flush()
    await db.refresh(asset)

    await log_audit(
        db, action="update", entity="SharedAsset",
        entity_id=asset.id, user_id=user_id,
        before=before, after=model_to_dict(asset),
    )

    db.expire(asset)
    return await get_asset_or_404(asset_id, household_id, db)


# ---------------------------------------------------------------------------
# Dispose
# ---------------------------------------------------------------------------

async def dispose_asset_svc(
    asset_id: int,
    household_id: int,
    user_id: int,
    db: AsyncSession,
) -> SharedAsset:
    asset = await get_asset_or_404(asset_id, household_id, db)
    before = model_to_dict(asset)

    asset.status = "disposed"
    await db.flush()
    await db.refresh(asset)

    await log_audit(
        db, action="update", entity="SharedAsset",
        entity_id=asset_id, user_id=user_id,
        before=before, after=model_to_dict(asset),
    )

    db.expire(asset)
    return await get_asset_or_404(asset_id, household_id, db)
