"""Shopping router — entries, line items, and item-catalog upsert.

Multipart POST note
-------------------
POST /shopping and PATCH /shopping/{id} accept multipart/form-data so that
an optional receipt photo can be included in the same request as the text
fields.  For the create endpoint the item list is too rich to express as
individual form fields, so it is sent as a single JSON-encoded string in the
``items_json`` form field.

    items_json='[{"name":"Rice","price":"120","quantity":"1","category":"meal"}]'

FastAPI validates the pattern/type fields on the outer form but the items
array is parsed manually with Pydantic inside the handler, so validation
errors in individual items surface as 422 with field-level detail.
"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.database import get_db
from app.models.core import User
from app.models.expenses import ItemCatalog, ShoppingEntry, ShoppingItem
from app.models.settlement import Month
from app.schemas.expenses import (
    ShoppingEntryResponse,
    ShoppingItemCreate,
    ShoppingItemResponse,
    ShoppingItemUpdate,
)
from app.services.storage import save_receipt_photo
from app.utils.audit import log_audit, model_to_dict
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_entry_or_404(entry_id: int, household_id: int, db: AsyncSession) -> ShoppingEntry:
    result = await db.execute(
        select(ShoppingEntry).where(
            ShoppingEntry.id == entry_id,
            ShoppingEntry.household_id == household_id,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shopping entry not found")
    return entry


async def _get_item_or_404(
    item_id: int,
    household_id: int,
    db: AsyncSession,
) -> tuple[ShoppingItem, ShoppingEntry]:
    """Return (item, parent_entry) in one query. 404 if item is missing or cross-household."""
    result = await db.execute(
        select(ShoppingItem)
        .where(ShoppingItem.id == item_id)
        .options(joinedload(ShoppingItem.entry))
    )
    item = result.unique().scalar_one_or_none()
    if item is None or item.entry.household_id != household_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shopping item not found")
    return item, item.entry


async def _load_entry_response(entry_id: int, db: AsyncSession) -> ShoppingEntryResponse:
    """Re-query a single entry with items eagerly loaded for response building."""
    result = await db.execute(
        select(ShoppingEntry)
        .where(ShoppingEntry.id == entry_id)
        .options(selectinload(ShoppingEntry.items))
    )
    return ShoppingEntryResponse.model_validate(result.scalar_one())


async def _require_open_month(month: str, household_id: int, db: AsyncSession) -> None:
    result = await db.execute(
        select(Month).where(Month.id == month, Month.household_id == household_id)
    )
    row = result.scalar_one_or_none()
    if row is not None and row.status == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Month is closed")


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def _validate_items(
    items: list[ShoppingItemCreate],
    household_id: int,
    db: AsyncSession,
) -> None:
    """Enforce personal/target_user_id rules and household membership."""
    for i, item in enumerate(items, 1):
        if item.category == "personal" and item.target_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Item {i}: target_user_id is required when category is 'personal'",
            )
        if item.category != "personal" and item.target_user_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Item {i}: target_user_id is only allowed when category is 'personal'",
            )

    target_ids = {item.target_user_id for item in items if item.target_user_id is not None}
    if target_ids:
        result = await db.execute(
            select(User.id).where(
                User.id.in_(target_ids),
                User.household_id == household_id,
            )
        )
        found_ids = {row[0] for row in result}
        bad_ids = target_ids - found_ids
        if bad_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"target_user_id(s) not found in household: {sorted(bad_ids)}",
            )


async def _upsert_catalog(
    db: AsyncSession,
    household_id: int,
    items: list[ShoppingItemCreate],
    now: datetime,
) -> None:
    """Best-effort catalog upsert in a savepoint — failure is logged, never raised."""
    try:
        async with db.begin_nested():
            for item in items:
                name_lower = item.name.strip().lower()
                result = await db.execute(
                    select(ItemCatalog).where(
                        ItemCatalog.household_id == household_id,
                        func.lower(ItemCatalog.name) == name_lower,
                    )
                )
                catalog = result.scalar_one_or_none()
                if catalog is None:
                    db.add(ItemCatalog(
                        household_id=household_id,
                        name=item.name.strip(),
                        default_category=item.category,
                        last_used_at=now,
                        use_count=1,
                    ))
                else:
                    catalog.use_count += 1
                    catalog.last_used_at = now
                    catalog.default_category = item.category  # most-recent category wins
    except Exception as exc:
        logger.warning("item_catalog upsert failed (non-fatal): %s", exc)


def _parse_items_json(raw: str) -> list[ShoppingItemCreate]:
    """Parse the items_json form field. Raises 422 for bad JSON or schema violations."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"items_json is not valid JSON: {exc}",
        )
    if not isinstance(data, list) or len(data) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="items_json must be a non-empty JSON array",
        )
    try:
        return [ShoppingItemCreate.model_validate(it) for it in data]
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"items_json validation error: {exc}",
        )


# ---------------------------------------------------------------------------
# Endpoints — entries
# ---------------------------------------------------------------------------

@router.get("", response_model=list[ShoppingEntryResponse])
async def list_entries(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    category: str | None = Query(None, pattern=r"^(meal|household|personal)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ShoppingEntryResponse]:
    target_month = month or _current_month()

    stmt = (
        select(ShoppingEntry)
        .where(
            ShoppingEntry.household_id == current_user.household_id,
            ShoppingEntry.month == target_month,
        )
        .options(selectinload(ShoppingEntry.items))
        .order_by(ShoppingEntry.id)
    )

    if category:
        stmt = stmt.where(
            ShoppingEntry.id.in_(
                select(ShoppingItem.entry_id).where(ShoppingItem.category == category)
            )
        )

    result = await db.execute(stmt)
    return [ShoppingEntryResponse.model_validate(e) for e in result.scalars().all()]


@router.get("/{entry_id}", response_model=ShoppingEntryResponse)
async def get_entry(
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShoppingEntryResponse:
    await _get_entry_or_404(entry_id, current_user.household_id, db)
    return await _load_entry_response(entry_id, db)


@router.post(
    "",
    response_model=ShoppingEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create shopping entry (multipart: items_json + optional photo)",
)
async def create_entry(
    items_json: str = Form(
        ...,
        description=(
            'JSON array of items. Each item: {"name": str, "price": "decimal",'
            ' "quantity": "decimal", "category": "meal|household|personal",'
            ' "target_user_id": int|null (required iff category=personal)}'
        ),
    ),
    month: str = Form(..., pattern=r"^\d{4}-\d{2}$"),
    note: str | None = Form(None),
    photo: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShoppingEntryResponse:
    items = _parse_items_json(items_json)
    await _validate_items(items, current_user.household_id, db)
    await _require_open_month(month, current_user.household_id, db)

    photo_url = await save_receipt_photo(photo)
    now = datetime.now(timezone.utc)

    entry = ShoppingEntry(
        household_id=current_user.household_id,
        month=month,
        paid_by=current_user.id,
        photo_url=photo_url,
        note=note if note else None,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)

    added_items = []
    for item_data in items:
        si = ShoppingItem(
            entry_id=entry.id,
            name=item_data.name,
            price=item_data.price,
            quantity=item_data.quantity,
            category=item_data.category,
            target_user_id=item_data.target_user_id,
        )
        db.add(si)
        added_items.append(si)
    await db.flush()

    await _upsert_catalog(db, current_user.household_id, items, now)

    await log_audit(
        db,
        action="create",
        entity="ShoppingEntry",
        entity_id=entry.id,
        user_id=current_user.id,
        after=model_to_dict(entry),
    )
    for si in added_items:
        await log_audit(
            db,
            action="create",
            entity="ShoppingItem",
            entity_id=si.id,
            user_id=current_user.id,
            after=model_to_dict(si),
        )
    logger.info("ShoppingEntry id=%s created by user_id=%s", entry.id, current_user.id)
    return await _load_entry_response(entry.id, db)


@router.patch(
    "/{entry_id}",
    response_model=ShoppingEntryResponse,
    summary="Update entry note/photo (multipart form)",
)
async def update_entry(
    entry_id: int,
    note: str | None = Form(None),
    photo: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShoppingEntryResponse:
    entry = await _get_entry_or_404(entry_id, current_user.household_id, db)
    await _require_open_month(entry.month, current_user.household_id, db)

    before = model_to_dict(entry)
    changed = False

    # note: None = don't touch; "" = clear; "text" = update
    if note is not None:
        entry.note = note if note else None
        changed = True

    if photo is not None and photo.filename:
        entry.photo_url = await save_receipt_photo(photo)
        changed = True

    if not changed:
        return await _load_entry_response(entry_id, db)

    await db.flush()
    await db.refresh(entry)

    await log_audit(
        db,
        action="update",
        entity="ShoppingEntry",
        entity_id=entry.id,
        user_id=current_user.id,
        before=before,
        after=model_to_dict(entry),
    )
    logger.info("ShoppingEntry id=%s updated by user_id=%s", entry.id, current_user.id)
    return await _load_entry_response(entry.id, db)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    entry = await _get_entry_or_404(entry_id, current_user.household_id, db)
    await _require_open_month(entry.month, current_user.household_id, db)

    before = model_to_dict(entry)
    entry_id_log = entry.id
    await db.delete(entry)  # cascade="all, delete-orphan" removes items

    await log_audit(
        db,
        action="delete",
        entity="ShoppingEntry",
        entity_id=entry_id_log,
        user_id=current_user.id,
        before=before,
    )
    logger.info("ShoppingEntry id=%s deleted by user_id=%s", entry_id_log, current_user.id)


# ---------------------------------------------------------------------------
# Endpoints — items sub-resource
# ---------------------------------------------------------------------------

@router.post(
    "/{entry_id}/items",
    response_model=ShoppingEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_items(
    entry_id: int,
    body: list[ShoppingItemCreate],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShoppingEntryResponse:
    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Must provide at least one item",
        )

    entry = await _get_entry_or_404(entry_id, current_user.household_id, db)
    await _require_open_month(entry.month, current_user.household_id, db)
    await _validate_items(body, current_user.household_id, db)

    now = datetime.now(timezone.utc)
    added_items = []
    for item_data in body:
        si = ShoppingItem(
            entry_id=entry.id,
            name=item_data.name,
            price=item_data.price,
            quantity=item_data.quantity,
            category=item_data.category,
            target_user_id=item_data.target_user_id,
        )
        db.add(si)
        added_items.append(si)
    await db.flush()

    await _upsert_catalog(db, current_user.household_id, body, now)

    for si in added_items:
        await log_audit(
            db,
            action="create",
            entity="ShoppingItem",
            entity_id=si.id,
            user_id=current_user.id,
            after=model_to_dict(si),
        )
    logger.info(
        "Added %d item(s) to ShoppingEntry id=%s by user_id=%s",
        len(body), entry.id, current_user.id,
    )
    return await _load_entry_response(entry.id, db)


@router.patch("/items/{item_id}", response_model=ShoppingItemResponse)
async def update_item(
    item_id: int,
    body: ShoppingItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShoppingItemResponse:
    item, entry = await _get_item_or_404(item_id, current_user.household_id, db)
    await _require_open_month(entry.month, current_user.household_id, db)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return ShoppingItemResponse.model_validate(item)

    # Compute the resulting category + target_user_id to validate the combination
    resulting_category = updates.get("category", item.category)
    resulting_target = updates["target_user_id"] if "target_user_id" in updates else item.target_user_id

    if resulting_category == "personal" and resulting_target is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_user_id is required when category is 'personal'",
        )
    if resulting_category != "personal" and resulting_target is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_user_id is only allowed when category is 'personal'",
        )

    # Validate new target_user_id belongs to this household
    if "target_user_id" in updates and updates["target_user_id"] is not None:
        new_tid = updates["target_user_id"]
        if new_tid != item.target_user_id:
            found = await db.scalar(
                select(User.id).where(
                    User.id == new_tid,
                    User.household_id == current_user.household_id,
                )
            )
            if found is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="target_user_id not found in household",
                )

    before = model_to_dict(item)
    for field, value in updates.items():
        setattr(item, field, value)  # target_user_id may be None — allowed

    await db.flush()
    await db.refresh(item)

    await log_audit(
        db,
        action="update",
        entity="ShoppingItem",
        entity_id=item.id,
        user_id=current_user.id,
        before=before,
        after=model_to_dict(item),
    )
    logger.info("ShoppingItem id=%s updated by user_id=%s", item.id, current_user.id)
    return ShoppingItemResponse.model_validate(item)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    item, entry = await _get_item_or_404(item_id, current_user.household_id, db)
    await _require_open_month(entry.month, current_user.household_id, db)

    item_count = await db.scalar(
        select(func.count()).select_from(ShoppingItem).where(
            ShoppingItem.entry_id == item.entry_id
        )
    )
    if item_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the last item in a shopping entry",
        )

    before = model_to_dict(item)
    item_id_log = item.id
    await db.delete(item)

    await log_audit(
        db,
        action="delete",
        entity="ShoppingItem",
        entity_id=item_id_log,
        user_id=current_user.id,
        before=before,
    )
    logger.info("ShoppingItem id=%s deleted by user_id=%s", item_id_log, current_user.id)
