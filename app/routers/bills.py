"""Bills (utility bills) router."""
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.expenses import UtilityBill
from app.models.settlement import Month
from app.models.core import User
from app.schemas.expenses import UtilityBillResponse, UtilityBillUpdate
from app.services.storage import save_receipt_photo
from app.utils.audit import log_audit, model_to_dict
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()
logger = logging.getLogger(__name__)

_VALID_TYPES: frozenset[str] = frozenset(
    {"electricity", "internet", "gas", "water", "other"}
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_bill_or_404(bill_id: int, household_id: int, db: AsyncSession) -> UtilityBill:
    result = await db.execute(
        select(UtilityBill).where(
            UtilityBill.id == bill_id,
            UtilityBill.household_id == household_id,
        )
    )
    bill = result.scalar_one_or_none()
    if bill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found")
    return bill


async def _require_open_month(month: str, household_id: int, db: AsyncSession) -> None:
    """Raise 409 if the month row exists and is closed. Missing row → open."""
    result = await db.execute(
        select(Month).where(Month.id == month, Month.household_id == household_id)
    )
    row = result.scalar_one_or_none()
    if row is not None and row.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Month is closed",
        )


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[UtilityBillResponse])
async def list_bills(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UtilityBillResponse]:
    target_month = month or _current_month()
    result = await db.execute(
        select(UtilityBill)
        .where(
            UtilityBill.household_id == current_user.household_id,
            UtilityBill.month == target_month,
        )
        .order_by(UtilityBill.paid_at, UtilityBill.id)
    )
    return [UtilityBillResponse.model_validate(b) for b in result.scalars().all()]


@router.get("/{bill_id}", response_model=UtilityBillResponse)
async def get_bill(
    bill_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UtilityBillResponse:
    bill = await _get_bill_or_404(bill_id, current_user.household_id, db)
    return UtilityBillResponse.model_validate(bill)


@router.post("", response_model=UtilityBillResponse, status_code=status.HTTP_201_CREATED)
async def create_bill(
    month: str = Form(..., pattern=r"^\d{4}-\d{2}$"),
    type: str = Form(...),
    amount: Decimal = Form(...),
    paid_at: date = Form(...),
    note: str | None = Form(None),
    photo: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UtilityBillResponse:
    if type not in _VALID_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"type must be one of: {', '.join(sorted(_VALID_TYPES))}",
        )

    await _require_open_month(month, current_user.household_id, db)

    photo_url = await save_receipt_photo(photo)

    bill = UtilityBill(
        household_id=current_user.household_id,
        month=month,
        type=type,
        amount=amount,
        paid_by=current_user.id,
        paid_at=paid_at,
        photo_url=photo_url,
        note=note,
    )
    db.add(bill)
    await db.flush()
    await db.refresh(bill)

    await log_audit(
        db,
        action="create",
        entity="UtilityBill",
        entity_id=bill.id,
        user_id=current_user.id,
        after=model_to_dict(bill),
    )
    logger.info("UtilityBill id=%s created by user_id=%s", bill.id, current_user.id)
    return UtilityBillResponse.model_validate(bill)


@router.patch("/{bill_id}", response_model=UtilityBillResponse)
async def update_bill(
    bill_id: int,
    body: UtilityBillUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UtilityBillResponse:
    bill = await _get_bill_or_404(bill_id, current_user.household_id, db)
    await _require_open_month(bill.month, current_user.household_id, db)

    before = model_to_dict(bill)
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}

    if not updates:
        return UtilityBillResponse.model_validate(bill)

    # If the month is being changed, the new month must also be open
    if "month" in updates and updates["month"] != bill.month:
        await _require_open_month(updates["month"], current_user.household_id, db)

    for field, value in updates.items():
        setattr(bill, field, value)

    await db.flush()
    await db.refresh(bill)

    await log_audit(
        db,
        action="update",
        entity="UtilityBill",
        entity_id=bill.id,
        user_id=current_user.id,
        before=before,
        after=model_to_dict(bill),
    )
    logger.info("UtilityBill id=%s updated by user_id=%s", bill.id, current_user.id)
    return UtilityBillResponse.model_validate(bill)


@router.delete("/{bill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bill(
    bill_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    bill = await _get_bill_or_404(bill_id, current_user.household_id, db)
    await _require_open_month(bill.month, current_user.household_id, db)

    before = model_to_dict(bill)
    bill_id_for_log = bill.id
    await db.delete(bill)

    await log_audit(
        db,
        action="delete",
        entity="UtilityBill",
        entity_id=bill_id_for_log,
        user_id=current_user.id,
        before=before,
    )
    logger.info("UtilityBill id=%s deleted by user_id=%s", bill_id_for_log, current_user.id)
