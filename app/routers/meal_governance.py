"""Meal governance router — meal-edit permissions and meal disputes.

Permission flow: an admin requests permission to edit a specific member's
meals for a month ("YYYY-MM"); the member approves or rejects. One grant per
member per month (unique constraint); a rejected request may be re-raised.

Dispute flow: any member disputes a meal log entry they believe is wrong; an
admin reviews open disputes and resolves them (resolution just closes the
dispute — it does not auto-edit the meal log).

The reusable `has_meal_edit_permission` helper is exported here for the
meal-edit gating in app/routers/meals.py.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db, utcnow
from app.models.core import User
from app.models.expenses import MealLog
from app.models.meal_governance import MealDispute, MealEditPermission
from app.schemas.core import UserMini
from app.schemas.expenses import MealLogResponse
from app.schemas.meal_governance import (
    MealDisputeCreate,
    MealDisputeResponse,
    MealPermissionReject,
    MealPermissionRequest,
    MealPermissionResponse,
)
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()
dispute_router = APIRouter()

_PERMISSION_LOAD_OPTIONS = (
    selectinload(MealEditPermission.admin_user),
    selectinload(MealEditPermission.member_user),
)


# ---------------------------------------------------------------------------
# Reusable helper — imported by the meal-edit gating in a later prompt
# ---------------------------------------------------------------------------

async def has_meal_edit_permission(
    db: AsyncSession, household_id: int, member_user_id: int, month: str
) -> bool:
    """Return True if an approved meal-edit grant exists for this member+month.

    A grant is scoped to one member for one "YYYY-MM" month within a
    household. Once approved, an admin may edit that member's meals for that
    month.
    """
    result = await db.execute(
        select(MealEditPermission.id).where(
            MealEditPermission.household_id == household_id,
            MealEditPermission.member_user_id == member_user_id,
            MealEditPermission.month == month,
            MealEditPermission.status == "approved",
        )
    )
    return result.first() is not None


# ---------------------------------------------------------------------------
# Loaders / builders
# ---------------------------------------------------------------------------

async def _get_permission_or_404(
    permission_id: int, household_id: int, db: AsyncSession
) -> MealEditPermission:
    result = await db.execute(
        select(MealEditPermission)
        .where(
            MealEditPermission.id == permission_id,
            MealEditPermission.household_id == household_id,
        )
        .options(*_PERMISSION_LOAD_OPTIONS)
        .execution_options(populate_existing=True)
    )
    perm = result.scalar_one_or_none()
    if perm is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Permission request not found"
        )
    return perm


def _build_permission_response(perm: MealEditPermission) -> MealPermissionResponse:
    return MealPermissionResponse(
        id=perm.id,
        household_id=perm.household_id,
        admin_user=UserMini(id=perm.admin_user.id, name=perm.admin_user.name),
        member_user=UserMini(id=perm.member_user.id, name=perm.member_user.name),
        month=perm.month,
        status=perm.status,
        reject_reason=perm.reject_reason,
        requested_at=perm.requested_at,
        resolved_at=perm.resolved_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=MealPermissionResponse)
async def request_permission(
    body: MealPermissionRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MealPermissionResponse:
    if body.member_user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot request permission to edit your own meals",
        )

    member = await db.get(User, body.member_user_id)
    if (
        member is None
        or member.household_id != current_user.household_id
        or member.left_at is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in this household",
        )

    # Upsert against the unique (member_user_id, month) constraint.
    existing = await db.execute(
        select(MealEditPermission).where(
            MealEditPermission.member_user_id == body.member_user_id,
            MealEditPermission.month == body.month,
        )
    )
    perm = existing.scalar_one_or_none()

    if perm is not None:
        if perm.status == "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Permission already granted for this member and month",
            )
        if perm.status == "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A permission request is already pending for this member and month",
            )
        # status == "rejected" → allow re-requesting: reset to pending.
        perm.status = "pending"
        perm.reject_reason = None
        perm.resolved_at = None
        perm.admin_user_id = current_user.id
        perm.requested_at = utcnow()
    else:
        perm = MealEditPermission(
            household_id=current_user.household_id,
            admin_user_id=current_user.id,
            member_user_id=body.member_user_id,
            month=body.month,
            status="pending",
        )
        db.add(perm)

    await db.flush()
    perm = await _get_permission_or_404(perm.id, current_user.household_id, db)
    return _build_permission_response(perm)


@router.get("", response_model=list[MealPermissionResponse])
async def list_permissions(
    month: str | None = Query(None),
    status_filter: Literal["pending", "approved", "rejected"] | None = Query(
        None, alias="status"
    ),
    member_user_id: int | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MealPermissionResponse]:
    stmt = select(MealEditPermission).where(
        MealEditPermission.household_id == current_user.household_id
    )
    # A regular member only sees requests directed at them; an admin sees all.
    if not current_user.is_admin:
        stmt = stmt.where(MealEditPermission.member_user_id == current_user.id)

    if month is not None:
        stmt = stmt.where(MealEditPermission.month == month)
    if status_filter is not None:
        stmt = stmt.where(MealEditPermission.status == status_filter)
    if member_user_id is not None:
        stmt = stmt.where(MealEditPermission.member_user_id == member_user_id)

    stmt = (
        stmt.order_by(MealEditPermission.requested_at.desc(), MealEditPermission.id.desc())
        .options(*_PERMISSION_LOAD_OPTIONS)
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return [_build_permission_response(p) for p in result.scalars().all()]


@router.post("/{permission_id}/approve", response_model=MealPermissionResponse)
async def approve_permission(
    permission_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MealPermissionResponse:
    perm = await _get_permission_or_404(permission_id, current_user.household_id, db)
    if perm.member_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the member whose meals are affected can approve this request",
        )
    if perm.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a pending request can be approved",
        )
    perm.status = "approved"
    perm.reject_reason = None
    perm.resolved_at = utcnow()
    await db.flush()
    return _build_permission_response(perm)


@router.post("/{permission_id}/reject", response_model=MealPermissionResponse)
async def reject_permission(
    permission_id: int,
    body: MealPermissionReject,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MealPermissionResponse:
    perm = await _get_permission_or_404(permission_id, current_user.household_id, db)
    if perm.member_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the member whose meals are affected can reject this request",
        )
    if perm.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a pending request can be rejected",
        )
    perm.status = "rejected"
    perm.reject_reason = body.reason
    perm.resolved_at = utcnow()
    await db.flush()
    return _build_permission_response(perm)


# ===========================================================================
# Meal disputes  (mounted under /api/v1/meal-disputes)
# ===========================================================================

_DISPUTE_LOAD_OPTIONS = (
    selectinload(MealDispute.raised_by_user),
    selectinload(MealDispute.resolved_by_user),
    selectinload(MealDispute.meal_log).selectinload(MealLog.user),
)


async def _get_household_meal_log(
    meal_log_id: int, household_id: int, db: AsyncSession
) -> MealLog | None:
    """Return the meal log if it belongs to this household, else None."""
    result = await db.execute(
        select(MealLog)
        .join(User, MealLog.user_id == User.id)
        .where(MealLog.id == meal_log_id, User.household_id == household_id)
    )
    return result.scalar_one_or_none()


async def _get_dispute_or_404(
    dispute_id: int, household_id: int, db: AsyncSession
) -> MealDispute:
    result = await db.execute(
        select(MealDispute)
        .where(
            MealDispute.id == dispute_id,
            MealDispute.household_id == household_id,
        )
        .options(*_DISPUTE_LOAD_OPTIONS)
        .execution_options(populate_existing=True)
    )
    dispute = result.scalar_one_or_none()
    if dispute is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found"
        )
    return dispute


def _build_dispute_response(d: MealDispute) -> MealDisputeResponse:
    log = d.meal_log
    return MealDisputeResponse(
        id=d.id,
        household_id=d.household_id,
        meal_log_id=d.meal_log_id,
        raised_by_user=UserMini(id=d.raised_by_user.id, name=d.raised_by_user.name),
        reason=d.reason,
        status=d.status,
        resolved_by_user=(
            UserMini(id=d.resolved_by_user.id, name=d.resolved_by_user.name)
            if d.resolved_by_user is not None
            else None
        ),
        created_at=d.created_at,
        resolved_at=d.resolved_at,
        meal_log=MealLogResponse(
            id=log.id,
            user=UserMini(id=log.user.id, name=log.user.name),
            log_date=log.log_date,
            meal_count=log.meal_count,
            guest_meals=log.guest_meals,
            total_meals=log.total_meals,
            note=log.note,
            created_at=log.created_at,
            updated_at=log.updated_at,
        ),
    )


@dispute_router.post("", response_model=MealDisputeResponse)
async def raise_dispute(
    body: MealDisputeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MealDisputeResponse:
    log = await _get_household_meal_log(body.meal_log_id, current_user.household_id, db)
    if log is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meal log not found in this household",
        )

    # One open dispute per (user, log) — re-raising while one is open is a no-op.
    existing = await db.execute(
        select(MealDispute.id).where(
            MealDispute.meal_log_id == body.meal_log_id,
            MealDispute.raised_by == current_user.id,
            MealDispute.status == "open",
        )
    )
    if existing.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an open dispute on this meal log",
        )

    dispute = MealDispute(
        household_id=current_user.household_id,
        meal_log_id=body.meal_log_id,
        raised_by=current_user.id,
        reason=body.reason,
        status="open",
    )
    db.add(dispute)
    await db.flush()
    dispute = await _get_dispute_or_404(dispute.id, current_user.household_id, db)
    return _build_dispute_response(dispute)


@dispute_router.get("", response_model=list[MealDisputeResponse])
async def list_disputes(
    status_filter: Literal["open", "resolved"] | None = Query(None, alias="status"),
    meal_log_id: int | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MealDisputeResponse]:
    stmt = select(MealDispute).where(
        MealDispute.household_id == current_user.household_id
    )
    # Admin sees all household disputes; a member sees disputes they raised
    # plus disputes on their own meal logs.
    if not current_user.is_admin:
        stmt = stmt.join(MealLog, MealDispute.meal_log_id == MealLog.id).where(
            or_(
                MealDispute.raised_by == current_user.id,
                MealLog.user_id == current_user.id,
            )
        )

    if status_filter is not None:
        stmt = stmt.where(MealDispute.status == status_filter)
    if meal_log_id is not None:
        stmt = stmt.where(MealDispute.meal_log_id == meal_log_id)

    stmt = (
        stmt.order_by(MealDispute.created_at.desc(), MealDispute.id.desc())
        .options(*_DISPUTE_LOAD_OPTIONS)
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return [_build_dispute_response(d) for d in result.scalars().all()]


@dispute_router.post("/{dispute_id}/resolve", response_model=MealDisputeResponse)
async def resolve_dispute(
    dispute_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MealDisputeResponse:
    dispute = await _get_dispute_or_404(dispute_id, current_user.household_id, db)
    if dispute.status != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an open dispute can be resolved",
        )
    dispute.status = "resolved"
    dispute.resolved_by = current_user.id
    dispute.resolved_at = utcnow()
    await db.flush()
    dispute = await _get_dispute_or_404(dispute.id, current_user.household_id, db)
    return _build_dispute_response(dispute)


@dispute_router.delete("/{dispute_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_dispute(
    dispute_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    dispute = await _get_dispute_or_404(dispute_id, current_user.household_id, db)
    if dispute.raised_by != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the member who raised the dispute or an admin can withdraw it",
        )
    if dispute.status != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resolved dispute cannot be withdrawn",
        )
    await db.delete(dispute)
    await db.flush()
