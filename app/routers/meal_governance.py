"""Meal governance router — the per-month meal-edit permission flow.

An admin requests permission to edit a specific member's meals for a month
("YYYY-MM"). The member approves or rejects (with an optional reason). One
grant per member per month is enforced by a unique constraint; a rejected
request may be re-raised, which resets it to pending.

This module covers ONLY the permission flow. Disputes and the actual gating
of meal-edit endpoints are handled in later prompts. The reusable
`has_meal_edit_permission` helper is exported here for that later gating.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db, utcnow
from app.models.core import User
from app.models.meal_governance import MealEditPermission
from app.schemas.core import UserMini
from app.schemas.meal_governance import (
    MealPermissionReject,
    MealPermissionRequest,
    MealPermissionResponse,
)
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()

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
