"""Users router — list, invite, update, room assignment, admin transfer, leaving."""
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.core import Room, RoomAssignment, User
from app.models.deposits import SecurityDeposit
from app.schemas.core import (
    AssignRoomRequest,
    InviteUserRequest,
    InviteUserResponse,
    ProcessLeavingRequest,
    RoomAssignmentResponse,
    UserPatchRequest,
    UserResponse,
)
from app.services.email import send_invite_email
from app.utils.audit import log_audit, model_to_dict
from app.utils.auth import hash_password
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_user_or_404(user_id: int, household_id: int, db: AsyncSession) -> User:
    result = await db.execute(
        select(User).where(User.id == user_id, User.household_id == household_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


async def _get_room_or_404(room_id: int, household_id: int, db: AsyncSession) -> Room:
    result = await db.execute(
        select(Room).where(Room.id == room_id, Room.household_id == household_id)
    )
    room = result.scalar_one_or_none()
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
    return room


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[UserResponse])
async def list_users(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserResponse]:
    result = await db.execute(
        select(User)
        .where(User.household_id == current_user.household_id)
        .order_by(User.name)
    )
    return [UserResponse.model_validate(u) for u in result.scalars().all()]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await _get_user_or_404(user_id, current_user.household_id, db)
    return UserResponse.model_validate(user)


@router.post("/invite", response_model=InviteUserResponse, status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: InviteUserRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> InviteUserResponse:
    await _get_room_or_404(body.room_id, current_user.household_id, db)

    existing = await db.scalar(
        select(User.id).where(func.lower(User.email) == body.email.strip().lower())
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists",
        )

    temp_password = secrets.token_urlsafe(12)
    today_utc = datetime.now(timezone.utc).date()
    effective_month = body.effective_month or _current_month()

    user = User(
        household_id=current_user.household_id,
        name=body.name,
        email=body.email.strip().lower(),
        password_hash=hash_password(temp_password),
        joined_at=today_utc,
        is_admin=False,
        must_change_password=True,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    db.add(RoomAssignment(
        room_id=body.room_id,
        user_id=user.id,
        effective_month=effective_month,
    ))
    db.add(SecurityDeposit(
        user_id=user.id,
        amount=body.deposit_amount,
        deposited_at=today_utc,
        held_by_user_id=current_user.id,
        status="held",
        deduction_amount=Decimal("0"),
        applied_to_dues=Decimal("0"),
        final_refund=Decimal("0"),
    ))
    await db.flush()

    await log_audit(
        db,
        action="create",
        entity="User",
        entity_id=user.id,
        user_id=current_user.id,
        after=model_to_dict(user),
    )
    logger.info("User id=%s invited by admin user_id=%s", user.id, current_user.id)

    email_sent = False
    try:
        await send_invite_email(
            to_email=user.email,
            to_name=user.name,
            temp_password=temp_password,
            login_url=f"{settings.frontend_url}/login",
        )
        email_sent = True
    except RuntimeError:
        logger.error("Invite email failed for user_id=%s", user.id)

    return InviteUserResponse(user=UserResponse.model_validate(user), email_sent=email_sent)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    body: UserPatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    if current_user.id != user_id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    user = await _get_user_or_404(user_id, current_user.household_id, db)
    before = model_to_dict(user)

    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}

    if not updates:
        return UserResponse.model_validate(user)

    if "email" in updates:
        new_email = updates["email"].strip().lower()
        clash = await db.scalar(
            select(User.id).where(
                func.lower(User.email) == new_email,
                User.id != user_id,
            )
        )
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with that email already exists",
            )
        updates["email"] = new_email

    for field, value in updates.items():
        setattr(user, field, value)

    await db.flush()
    await db.refresh(user)

    await log_audit(
        db,
        action="update",
        entity="User",
        entity_id=user.id,
        user_id=current_user.id,
        before=before,
        after=model_to_dict(user),
    )
    logger.info("User id=%s updated by user_id=%s", user.id, current_user.id)
    return UserResponse.model_validate(user)


@router.post(
    "/{user_id}/assign-room",
    response_model=RoomAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def assign_room(
    user_id: int,
    body: AssignRoomRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RoomAssignmentResponse:
    await _get_user_or_404(user_id, current_user.household_id, db)
    await _get_room_or_404(body.room_id, current_user.household_id, db)

    assignment = RoomAssignment(
        room_id=body.room_id,
        user_id=user_id,
        effective_month=body.effective_month,
    )
    db.add(assignment)

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already has a room assignment for that month",
        )

    await db.refresh(assignment)

    await log_audit(
        db,
        action="create",
        entity="RoomAssignment",
        entity_id=assignment.id,
        user_id=current_user.id,
        after=model_to_dict(assignment),
    )
    logger.info(
        "RoomAssignment id=%s created for user_id=%s by admin user_id=%s",
        assignment.id, user_id, current_user.id,
    )
    return RoomAssignmentResponse.model_validate(assignment)


@router.post("/{user_id}/transfer-admin", response_model=UserResponse)
async def transfer_admin(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot transfer admin role to yourself",
        )

    target = await _get_user_or_404(user_id, current_user.household_id, db)

    if target.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot transfer admin role to a user who has left",
        )

    before_current = model_to_dict(current_user)
    before_target = model_to_dict(target)

    current_user.is_admin = False
    target.is_admin = True

    await db.flush()
    await db.refresh(current_user)
    await db.refresh(target)

    note = f"admin role transferred from user_id={current_user.id} to user_id={target.id}"
    await log_audit(
        db,
        action="update",
        entity="User",
        entity_id=current_user.id,
        user_id=current_user.id,
        before=before_current,
        after=model_to_dict(current_user),
        note=note,
    )
    await log_audit(
        db,
        action="update",
        entity="User",
        entity_id=target.id,
        user_id=current_user.id,
        before=before_target,
        after=model_to_dict(target),
        note=note,
    )
    logger.info(
        "Admin transferred from user_id=%s to user_id=%s", current_user.id, target.id
    )
    return UserResponse.model_validate(target)


@router.post("/{user_id}/process-leaving", response_model=UserResponse)
async def process_leaving(
    user_id: int,
    body: ProcessLeavingRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await _get_user_or_404(user_id, current_user.household_id, db)

    if user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transfer the admin role before processing this user's departure",
        )

    active_count = await db.scalar(
        select(func.count()).select_from(User).where(
            User.household_id == current_user.household_id,
            User.left_at.is_(None),
        )
    )
    if active_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the last active member of the household",
        )

    today_utc = datetime.now(timezone.utc).date()
    if body.leave_date > today_utc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Leave date cannot be in the future",
        )
    if body.leave_date < user.joined_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Leave date cannot be before the user joined",
        )

    before_user = model_to_dict(user)
    user.left_at = body.leave_date
    await db.flush()
    await db.refresh(user)

    await log_audit(
        db,
        action="update",
        entity="User",
        entity_id=user.id,
        user_id=current_user.id,
        before=before_user,
        after=model_to_dict(user),
        note="processed leaving",
    )

    # Reconcile the most recent held deposit, if any
    deposit_result = await db.execute(
        select(SecurityDeposit)
        .where(SecurityDeposit.user_id == user.id, SecurityDeposit.status == "held")
        .order_by(SecurityDeposit.id.desc())
        .limit(1)
    )
    deposit = deposit_result.scalar_one_or_none()

    if deposit is not None:
        before_deposit = model_to_dict(deposit)
        deposit.deduction_amount = body.deductions
        deposit.deduction_reason = body.deduction_reason
        deposit.refunded_at = body.leave_date
        deposit.final_refund = max(
            Decimal("0"),
            deposit.amount - body.deductions - deposit.applied_to_dues,
        )
        deposit.status = "partial" if body.deductions > Decimal("0") else "refunded"

        await db.flush()
        await db.refresh(deposit)

        await log_audit(
            db,
            action="update",
            entity="SecurityDeposit",
            entity_id=deposit.id,
            user_id=current_user.id,
            before=before_deposit,
            after=model_to_dict(deposit),
            note="reconciled on leaving",
        )
        logger.info(
            "SecurityDeposit id=%s reconciled for user_id=%s by admin user_id=%s",
            deposit.id, user.id, current_user.id,
        )

    logger.info("User id=%s processed as left by admin user_id=%s", user.id, current_user.id)
    return UserResponse.model_validate(user)
