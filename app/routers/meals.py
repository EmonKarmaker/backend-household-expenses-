"""Meals router — meal log bulk upsert, single update/delete, and list."""
import calendar
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.core import User
from app.models.expenses import MealLog
from app.models.settlement import Month
from app.schemas.core import UserMini
from app.schemas.expenses import MealLogBulkUpsertRequest, MealLogResponse, MealLogUpdate
from app.utils.audit import log_audit, model_to_dict
from app.utils.permissions import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _month_date_range(month_str: str) -> tuple[date, date]:
    """Return (first_day, last_day) for a YYYY-MM string. Index-friendly alternative to to_char."""
    year, month = int(month_str[:4]), int(month_str[5:])
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, 1), date(year, month, last_day)


async def _get_log_or_404(log_id: int, household_id: int, db: AsyncSession) -> MealLog:
    result = await db.execute(
        select(MealLog)
        .join(User, MealLog.user_id == User.id)
        .where(MealLog.id == log_id, User.household_id == household_id)
    )
    log = result.scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meal log not found")
    return log


def _require_self_or_admin(log: MealLog, current_user: User) -> None:
    if not current_user.is_admin and log.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify another user's meal log",
        )


async def _require_open_month(month: str, household_id: int, db: AsyncSession) -> None:
    result = await db.execute(
        select(Month).where(Month.id == month, Month.household_id == household_id)
    )
    row = result.scalar_one_or_none()
    if row is not None and row.status == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Month is closed")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[MealLogResponse])
async def list_logs(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    user_id: int | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MealLogResponse]:
    target_month = month or _current_month()
    first_day, last_day = _month_date_range(target_month)

    stmt = (
        select(MealLog)
        .join(User, MealLog.user_id == User.id)
        .where(
            User.household_id == current_user.household_id,
            MealLog.log_date >= first_day,
            MealLog.log_date <= last_day,
        )
        .options(selectinload(MealLog.user))
        .order_by(MealLog.log_date, MealLog.user_id)
    )
    if user_id is not None:
        stmt = stmt.where(MealLog.user_id == user_id)

    result = await db.execute(stmt)
    return [
        MealLogResponse(
            id=r.id,
            user=UserMini(id=r.user.id, name=r.user.name),
            log_date=r.log_date,
            meal_count=r.meal_count,
            guest_meals=r.guest_meals,
            total_meals=r.total_meals,
            note=r.note,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in result.scalars().all()
    ]


@router.get("/me", response_model=list[MealLogResponse])
async def list_my_logs(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MealLogResponse]:
    target_month = month or _current_month()
    first_day, last_day = _month_date_range(target_month)

    result = await db.execute(
        select(MealLog)
        .where(
            MealLog.user_id == current_user.id,
            MealLog.log_date >= first_day,
            MealLog.log_date <= last_day,
        )
        .order_by(MealLog.log_date)
    )
    owner = UserMini(id=current_user.id, name=current_user.name)
    return [
        MealLogResponse(
            id=r.id,
            user=owner,
            log_date=r.log_date,
            meal_count=r.meal_count,
            guest_meals=r.guest_meals,
            total_meals=r.total_meals,
            note=r.note,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in result.scalars().all()
    ]


@router.post("/bulk", response_model=list[MealLogResponse])
async def bulk_upsert(
    body: MealLogBulkUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MealLogResponse]:
    user_ids_requested = {e.user_id for e in body.entries}

    # Non-admin: short-circuit before any household lookup to avoid info leakage
    if not current_user.is_admin:
        foreign_ids = user_ids_requested - {current_user.id}
        if foreign_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Non-admin users can only upsert their own meal logs",
            )

    # All referenced users must be in this household
    result = await db.execute(
        select(User).where(
            User.id.in_(user_ids_requested),
            User.household_id == current_user.household_id,
        )
    )
    users_by_id = {u.id: u for u in result.scalars().all()}

    bad_ids = user_ids_requested - users_by_id.keys()
    if bad_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"user_id(s) not in household: {sorted(bad_ids)}",
        )

    today = date.today()
    max_date = today + timedelta(days=1)

    for i, entry in enumerate(body.entries, 1):
        user = users_by_id[entry.user_id]
        if entry.log_date < user.joined_at:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Entry {i}: log_date {entry.log_date} is before user joined ({user.joined_at})",
            )
        if entry.log_date > max_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Entry {i}: log_date {entry.log_date} is more than 1 day in the future",
            )

    # Each distinct month in the request must be open
    for month_str in {e.log_date.strftime("%Y-%m") for e in body.entries}:
        await _require_open_month(month_str, current_user.household_id, db)

    # Batch upsert — one SQL round-trip regardless of row count
    values = [
        {
            "user_id": e.user_id,
            "log_date": e.log_date,
            "meal_count": e.meal_count,
            "guest_meals": e.guest_meals,
            "note": e.note,
        }
        for e in body.entries
    ]
    insert_stmt = pg_insert(MealLog).values(values)
    upsert_stmt = (
        insert_stmt
        .on_conflict_do_update(
            index_elements=["user_id", "log_date"],
            set_={
                "meal_count": insert_stmt.excluded.meal_count,
                "guest_meals": insert_stmt.excluded.guest_meals,
                "note": insert_stmt.excluded.note,
                "updated_at": func.now(),
            },
        )
        .returning(MealLog.id)
    )
    cursor = await db.execute(upsert_stmt)
    affected_ids = [row[0] for row in cursor.fetchall()]

    # Re-fetch ORM objects — pg_insert bypasses the identity map
    result = await db.execute(
        select(MealLog)
        .where(MealLog.id.in_(affected_ids))
        .order_by(MealLog.log_date, MealLog.user_id)
    )
    logs = result.scalars().all()

    for log in logs:
        await log_audit(
            db,
            action="create",
            entity="MealLog",
            entity_id=log.id,
            user_id=current_user.id,
            after=model_to_dict(log),
            note="bulk upsert",
        )
    logger.info("MealLog bulk upsert: %d rows by user_id=%s", len(logs), current_user.id)
    return [
        MealLogResponse(
            id=log.id,
            user=UserMini(id=users_by_id[log.user_id].id, name=users_by_id[log.user_id].name),
            log_date=log.log_date,
            meal_count=log.meal_count,
            guest_meals=log.guest_meals,
            total_meals=log.total_meals,
            note=log.note,
            created_at=log.created_at,
            updated_at=log.updated_at,
        )
        for log in logs
    ]


@router.patch("/{log_id}", response_model=MealLogResponse)
async def update_log(
    log_id: int,
    body: MealLogUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MealLogResponse:
    log = await _get_log_or_404(log_id, current_user.household_id, db)
    _require_self_or_admin(log, current_user)
    await _require_open_month(log.log_date.strftime("%Y-%m"), current_user.household_id, db)

    updates = body.model_dump(exclude_unset=True)
    # meal_count/guest_meals cannot be null in DB; only note can be cleared to null
    filtered = {k: v for k, v in updates.items() if v is not None or k == "note"}
    if not filtered:
        log_user = await db.get(User, log.user_id)
        return MealLogResponse(
            id=log.id,
            user=UserMini(id=log_user.id, name=log_user.name),
            log_date=log.log_date,
            meal_count=log.meal_count,
            guest_meals=log.guest_meals,
            total_meals=log.total_meals,
            note=log.note,
            created_at=log.created_at,
            updated_at=log.updated_at,
        )

    before = model_to_dict(log)
    for field, value in filtered.items():
        setattr(log, field, value)

    await db.flush()
    await db.refresh(log)
    log_user = await db.get(User, log.user_id)

    await log_audit(
        db,
        action="update",
        entity="MealLog",
        entity_id=log.id,
        user_id=current_user.id,
        before=before,
        after=model_to_dict(log),
    )
    logger.info("MealLog id=%s updated by user_id=%s", log.id, current_user.id)
    return MealLogResponse(
        id=log.id,
        user=UserMini(id=log_user.id, name=log_user.name),
        log_date=log.log_date,
        meal_count=log.meal_count,
        guest_meals=log.guest_meals,
        total_meals=log.total_meals,
        note=log.note,
        created_at=log.created_at,
        updated_at=log.updated_at,
    )


@router.delete("/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_log(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    log = await _get_log_or_404(log_id, current_user.household_id, db)
    _require_self_or_admin(log, current_user)
    await _require_open_month(log.log_date.strftime("%Y-%m"), current_user.household_id, db)

    before = model_to_dict(log)
    log_id_for_log = log.id
    await db.delete(log)

    await log_audit(
        db,
        action="delete",
        entity="MealLog",
        entity_id=log_id_for_log,
        user_id=current_user.id,
        before=before,
    )
    logger.info("MealLog id=%s deleted by user_id=%s", log_id_for_log, current_user.id)
