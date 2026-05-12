"""Rooms router — CRUD, admin-only writes."""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import Room, RoomAssignment, User
from app.schemas.core import RoomCreate, RoomResponse, RoomUpdate
from app.utils.audit import log_audit, model_to_dict
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_room_or_404(room_id: int, household_id: int, db: AsyncSession) -> Room:
    """Load a room that belongs to the given household, or raise 404.

    Deliberately returns the same 404 whether the room doesn't exist or belongs
    to a different household — prevents household enumeration.
    """
    result = await db.execute(
        select(Room).where(Room.id == room_id, Room.household_id == household_id)
    )
    room = result.scalar_one_or_none()
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
    return room


async def _has_any_assignment(room_id: int, db: AsyncSession) -> bool:
    """Return True if any room_assignment row references this room.

    v1 simplification: we check for *any* assignment, not just the most-recent
    one per user.  This is conservative — a user who moved out still has a
    historical row, so a room with ex-occupants cannot be deleted until those
    rows are cleaned up.  Refine to a per-user latest-assignment check later.
    """
    hit = await db.scalar(
        select(RoomAssignment.id)
        .where(RoomAssignment.room_id == room_id)
        .limit(1)
    )
    return hit is not None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[RoomResponse])
async def list_rooms(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RoomResponse]:
    result = await db.execute(
        select(Room)
        .where(Room.household_id == current_user.household_id)
        .order_by(Room.name)
    )
    return [RoomResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/{room_id}", response_model=RoomResponse)
async def get_room(
    room_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomResponse:
    room = await _get_room_or_404(room_id, current_user.household_id, db)
    return RoomResponse.model_validate(room)


@router.post("", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
async def create_room(
    body: RoomCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RoomResponse:
    room = Room(household_id=current_user.household_id, **body.model_dump())
    db.add(room)
    await db.flush()   # assigns room.id
    await db.refresh(room)  # load server_default timestamps

    await log_audit(
        db,
        action="create",
        entity="Room",
        entity_id=room.id,
        user_id=current_user.id,
        after=model_to_dict(room),
    )
    logger.info("Room id=%s created by user_id=%s", room.id, current_user.id)
    return RoomResponse.model_validate(room)


@router.patch("/{room_id}", response_model=RoomResponse)
async def update_room(
    room_id: int,
    body: RoomUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RoomResponse:
    room = await _get_room_or_404(room_id, current_user.household_id, db)
    before = model_to_dict(room)

    # exclude_unset=True: only touch fields the caller explicitly sent.
    # Skip None so an explicit null for a non-nullable column doesn't
    # blow up at flush time (Pydantic allows it, the DB doesn't).
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    for field, value in updates.items():
        setattr(room, field, value)

    await db.flush()
    await db.refresh(room)  # reload server-side updated_at before snapshotting

    await log_audit(
        db,
        action="update",
        entity="Room",
        entity_id=room.id,
        user_id=current_user.id,
        before=before,
        after=model_to_dict(room),
    )
    logger.info("Room id=%s updated by user_id=%s", room.id, current_user.id)
    return RoomResponse.model_validate(room)


@router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(
    room_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    room = await _get_room_or_404(room_id, current_user.household_id, db)

    if await _has_any_assignment(room_id, db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Room has active occupants; reassign them first",
        )

    before = model_to_dict(room)
    room_id_for_log = room.id  # capture before the ORM object is expunged
    await db.delete(room)

    await log_audit(
        db,
        action="delete",
        entity="Room",
        entity_id=room_id_for_log,
        user_id=current_user.id,
        before=before,
    )
    logger.info("Room id=%s deleted by user_id=%s", room_id_for_log, current_user.id)
