"""Assets router — shared durable household purchases."""
import json
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.assets import SharedAsset
from app.models.core import User
from app.schemas.assets import AssetContributionInput, SharedAssetResponse, SharedAssetUpdate
from app.services.asset_svc import (
    create_asset_svc,
    dispose_asset_svc,
    get_asset_or_404,
    update_asset_svc,
)
from app.services.storage import save_asset_photo
from app.utils.permissions import get_current_user, require_admin

router = APIRouter()

_contributions_adapter: TypeAdapter[list[AssetContributionInput]] = TypeAdapter(
    list[AssetContributionInput]
)


@router.get("", response_model=list[SharedAssetResponse])
async def list_assets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SharedAssetResponse]:
    result = await db.execute(
        select(SharedAsset)
        .where(SharedAsset.household_id == current_user.household_id)
        .order_by(SharedAsset.purchase_date.desc(), SharedAsset.id.desc())
        .options(
            selectinload(SharedAsset.contributions),
            selectinload(SharedAsset.refunds),
        )
    )
    return [SharedAssetResponse.model_validate(a) for a in result.scalars().all()]


@router.get("/{asset_id}", response_model=SharedAssetResponse)
async def get_asset(
    asset_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SharedAssetResponse:
    return SharedAssetResponse.model_validate(
        await get_asset_or_404(asset_id, current_user.household_id, db)
    )


@router.post("", response_model=SharedAssetResponse, status_code=status.HTTP_201_CREATED)
async def create_asset(
    name: str = Form(...),
    description: str | None = Form(None),
    purchase_date: date = Form(...),
    total_cost: Decimal = Form(...),
    requires_buyin_from_new_members: bool = Form(True),
    contributions_json: str = Form(...),
    photo: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SharedAssetResponse:
    try:
        contributions = _contributions_adapter.validate_python(json.loads(contributions_json))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    photo_url = await save_asset_photo(photo)

    asset = await create_asset_svc(
        household_id=current_user.household_id,
        bought_by_user_id=current_user.id,
        name=name,
        description=description,
        purchase_date=purchase_date,
        total_cost=total_cost,
        requires_buyin_from_new_members=requires_buyin_from_new_members,
        contributions=contributions,
        photo_url=photo_url,
        db=db,
    )
    return SharedAssetResponse.model_validate(asset)


@router.patch("/{asset_id}", response_model=SharedAssetResponse)
async def update_asset(
    asset_id: int,
    body: SharedAssetUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SharedAssetResponse:
    return SharedAssetResponse.model_validate(
        await update_asset_svc(asset_id, current_user.household_id, body, current_user.id, db)
    )


@router.post("/{asset_id}/dispose", response_model=SharedAssetResponse)
async def dispose_asset(
    asset_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SharedAssetResponse:
    return SharedAssetResponse.model_validate(
        await dispose_asset_svc(asset_id, current_user.household_id, current_user.id, db)
    )
