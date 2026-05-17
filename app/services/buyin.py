"""Asset buy-in service — new member joins an existing shared asset.

Symmetric operation to process_leaving_svc: when a leaver created a pending
AssetRefund, a new member buying in fulfils that refund and records their
own AssetContribution.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assets import AssetContribution, AssetRefund, SharedAsset
from app.models.core import User
from app.utils.audit import log_audit, model_to_dict

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------

class UserNotInHousehold(Exception):
    pass


class UserAlreadyLeft(Exception):
    pass


class AssetNotInHousehold(Exception):
    pass


class AssetNotActive(Exception):
    pass


class InvalidBuyinAmount(Exception):
    pass


class NoPendingRefunds(Exception):
    pass


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class BuyinResult:
    contribution_id: int
    amount: Decimal
    fulfilled_refunds: list[int]  # AssetRefund IDs that received a replacement


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

async def buyin_to_asset_svc(
    new_member_id: int,
    household_id: int,
    asset_id: int,
    amount: Decimal,
    admin_id: int,
    db: AsyncSession,
) -> BuyinResult:
    # 1. Load and validate the new member
    result = await db.execute(
        select(User).where(User.id == new_member_id, User.household_id == household_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotInHousehold(f"User {new_member_id} not found in household")
    if user.left_at is not None:
        raise UserAlreadyLeft(f"User {new_member_id} has already left the household")

    # 2. Load and validate the asset
    result = await db.execute(
        select(SharedAsset).where(
            SharedAsset.id == asset_id,
            SharedAsset.household_id == household_id,
        )
    )
    asset = result.scalar_one_or_none()
    if asset is None:
        raise AssetNotInHousehold(f"Asset {asset_id} not found in household")
    if asset.status != "active":
        raise AssetNotActive(f"Asset {asset_id} is disposed and cannot be bought into")

    # 3. Validate amount
    if amount <= ZERO:
        raise InvalidBuyinAmount("amount must be positive")

    # 4. Find pending refunds (replaced_by_user_id IS NULL), oldest first
    result = await db.execute(
        select(AssetRefund)
        .where(
            AssetRefund.asset_id == asset_id,
            AssetRefund.replaced_by_user_id.is_(None),
        )
        .order_by(AssetRefund.id)
    )
    pending_refunds = list(result.scalars().all())
    if not pending_refunds:
        raise NoPendingRefunds(f"Asset {asset_id} has no pending refunds to buy into")

    # 5. Record the new member's contribution
    contribution = AssetContribution(
        asset_id=asset_id,
        user_id=new_member_id,
        amount=amount,
        contributed_at=date.today(),
        contribution_type="buyin",
    )
    db.add(contribution)
    await db.flush()
    await db.refresh(contribution)

    await log_audit(
        db, action="create", entity="AssetContribution",
        entity_id=contribution.id, user_id=admin_id,
        after=model_to_dict(contribution),
    )

    # 6. Fulfil every pending refund — snapshot before, mutate, batch flush, then audit
    before_snapshots = [model_to_dict(r) for r in pending_refunds]

    for refund in pending_refunds:
        refund.replaced_by_user_id = new_member_id
        refund.paid_by_user = new_member_id

    await db.flush()

    fulfilled_ids: list[int] = []
    for refund, before in zip(pending_refunds, before_snapshots):
        await db.refresh(refund)
        await log_audit(
            db, action="update", entity="AssetRefund",
            entity_id=refund.id, user_id=admin_id,
            before=before, after=model_to_dict(refund),
        )
        fulfilled_ids.append(refund.id)

    return BuyinResult(
        contribution_id=contribution.id,
        amount=amount,
        fulfilled_refunds=fulfilled_ids,
    )
