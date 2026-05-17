"""Process-leaving service — atomic user departure workflow.

Handles dues reconciliation against the security deposit and records
asset refunds for any shared assets the user contributed to.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.assets import AssetContribution, AssetRefund, SharedAsset
from app.models.core import User
from app.models.deposits import SecurityDeposit
from app.models.settlement import Month, Settlement
from app.utils.audit import log_audit, model_to_dict

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Domain errors — routers convert these to HTTP responses
# ---------------------------------------------------------------------------

class UserNotInHousehold(Exception):
    pass


class UserAlreadyLeft(Exception):
    pass


class InvalidLeaveDate(Exception):
    pass


class UserIsAdmin(Exception):
    pass


class LastActiveMember(Exception):
    pass


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class AssetRefundData:
    asset_id: int
    asset_name: str
    amount: Decimal
    paid_by_user: int | None


@dataclass
class LeavingResult:
    user_id: int
    left_at: date
    dues: Decimal
    deposit_amount: Decimal
    deposit_applied_to_dues: Decimal
    deposit_cash_refund: Decimal
    remaining_dues: Decimal
    asset_refunds: list[AssetRefundData]
    total_cash_owed_to_leaver: Decimal


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

async def process_leaving_svc(
    user_id: int,
    household_id: int,
    leave_date: date,
    admin_id: int,
    db: AsyncSession,
) -> LeavingResult:
    # -----------------------------------------------------------------------
    # 1. Load and validate user
    # -----------------------------------------------------------------------
    result = await db.execute(
        select(User).where(User.id == user_id, User.household_id == household_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotInHousehold(f"User {user_id} not found in household")
    if user.left_at is not None:
        raise UserAlreadyLeft(f"User {user_id} has already left")
    if user.is_admin:
        raise UserIsAdmin(
            f"Transfer the admin role before processing user {user_id}'s departure"
        )

    active_count = await db.scalar(
        select(func.count()).select_from(User).where(
            User.household_id == household_id,
            User.left_at.is_(None),
        )
    )
    if active_count <= 1:
        raise LastActiveMember("Cannot remove the last active member of the household")

    today = date.today()
    if leave_date > today:
        raise InvalidLeaveDate("Leave date cannot be in the future")
    if leave_date < user.joined_at:
        raise InvalidLeaveDate("Leave date cannot be before the user joined")

    # -----------------------------------------------------------------------
    # 2. Compute dues from unpaid settlements in closed months
    # -----------------------------------------------------------------------
    dues_raw = await db.scalar(
        select(func.sum(Settlement.amount))
        .join(Month, Settlement.month_id == Month.id)
        .where(
            Settlement.from_user == user_id,
            Settlement.paid == False,  # noqa: E712
            Month.status == "closed",
            Month.household_id == household_id,
        )
    )
    dues = dues_raw if dues_raw is not None else ZERO

    # -----------------------------------------------------------------------
    # 3. Load held deposit and compute reconciliation amounts
    # -----------------------------------------------------------------------
    dep_result = await db.execute(
        select(SecurityDeposit)
        .where(SecurityDeposit.user_id == user_id, SecurityDeposit.status == "held")
        .order_by(SecurityDeposit.id.desc())
        .limit(1)
    )
    deposit = dep_result.scalar_one_or_none()

    deposit_amount = deposit.amount if deposit is not None else ZERO
    deposit_applied = min(deposit_amount, dues)
    cash_refund = max(ZERO, deposit_amount - dues)
    remaining_dues = max(ZERO, dues - deposit_amount)

    # -----------------------------------------------------------------------
    # 4. Asset refunds
    # -----------------------------------------------------------------------
    aid_result = await db.execute(
        select(AssetContribution.asset_id)
        .join(SharedAsset, AssetContribution.asset_id == SharedAsset.id)
        .where(
            AssetContribution.user_id == user_id,
            SharedAsset.household_id == household_id,
            SharedAsset.status == "active",
        )
        .distinct()
    )
    asset_ids = [row[0] for row in aid_result.all()]

    asset_refund_data: list[AssetRefundData] = []

    if asset_ids:
        assets_result = await db.execute(
            select(SharedAsset)
            .where(SharedAsset.id.in_(asset_ids))
            .options(selectinload(SharedAsset.contributions))
        )
        assets = list(assets_result.scalars().all())

        new_refund_rows: list[tuple[AssetRefund, str]] = []

        for asset in assets:
            all_contributor_ids = {c.user_id for c in asset.contributions}
            if len(all_contributor_ids) <= 1:
                continue  # sole contributor → personal asset, skip

            user_contrib_sum = sum(
                (c.amount for c in asset.contributions if c.user_id == user_id),
                ZERO,
            )

            paid_by = None if asset.requires_buyin_from_new_members else admin_id

            refund = AssetRefund(
                asset_id=asset.id,
                user_id=user_id,
                amount=user_contrib_sum,
                refunded_at=leave_date,
                paid_by_user=paid_by,
                replaced_by_user_id=None,
            )
            db.add(refund)
            new_refund_rows.append((refund, asset.name))

        if new_refund_rows:
            await db.flush()
            for refund, asset_name in new_refund_rows:
                await db.refresh(refund)
                await log_audit(
                    db, action="create", entity="AssetRefund",
                    entity_id=refund.id, user_id=admin_id,
                    after=model_to_dict(refund),
                )
                asset_refund_data.append(AssetRefundData(
                    asset_id=refund.asset_id,
                    asset_name=asset_name,
                    amount=refund.amount,
                    paid_by_user=refund.paid_by_user,
                ))

    # -----------------------------------------------------------------------
    # 5. Mark user as left
    # -----------------------------------------------------------------------
    before_user = model_to_dict(user)
    user.left_at = leave_date
    await db.flush()
    await db.refresh(user)
    await log_audit(
        db, action="update", entity="User",
        entity_id=user.id, user_id=admin_id,
        before=before_user, after=model_to_dict(user),
        note="processed leaving",
    )

    # -----------------------------------------------------------------------
    # 6. Reconcile deposit
    # -----------------------------------------------------------------------
    if deposit is not None:
        before_deposit = model_to_dict(deposit)
        deposit.applied_to_dues = deposit_applied
        deposit.final_refund = cash_refund
        deposit.refunded_at = leave_date
        # "refunded" = nothing was applied to dues (full cash return)
        # "partial"  = some or all deposit consumed by dues
        deposit.status = "refunded" if deposit_applied == ZERO else "partial"
        await db.flush()
        await db.refresh(deposit)
        await log_audit(
            db, action="update", entity="SecurityDeposit",
            entity_id=deposit.id, user_id=admin_id,
            before=before_deposit, after=model_to_dict(deposit),
            note="reconciled on leaving",
        )

    # -----------------------------------------------------------------------
    # 7. Build result
    # -----------------------------------------------------------------------
    total_cash_owed = cash_refund + sum(
        r.amount for r in asset_refund_data if r.paid_by_user is not None
    )

    return LeavingResult(
        user_id=user.id,
        left_at=leave_date,
        dues=dues,
        deposit_amount=deposit_amount,
        deposit_applied_to_dues=deposit_applied,
        deposit_cash_refund=cash_refund,
        remaining_dues=remaining_dues,
        asset_refunds=asset_refund_data,
        total_cash_owed_to_leaver=total_cash_owed,
    )
