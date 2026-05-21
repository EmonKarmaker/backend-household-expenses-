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
from app.models.assets import AssetContribution, AssetRefund, SharedAsset
from app.models.core import User
from app.schemas.assets import (
    AssetContributionInput,
    AssetContributionResponse,
    AssetRefundResponse,
    SharedAssetResponse,
    SharedAssetUpdate,
)
from app.schemas.core import UserMini
from app.services.asset_svc import (
    AssetNotFound,
    InvalidContributionSum,
    InvalidContributors,
    InvalidTotalCost,
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


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

def _build_contribution_response(c) -> AssetContributionResponse:
    return AssetContributionResponse(
        id=c.id,
        asset_id=c.asset_id,
        user=UserMini(id=c.user.id, name=c.user.name),
        amount=c.amount,
        contributed_at=c.contributed_at,
        contribution_type=c.contribution_type,
        created_at=c.created_at,
    )


def _build_refund_response(r) -> AssetRefundResponse:
    return AssetRefundResponse(
        id=r.id,
        asset_id=r.asset_id,
        user=UserMini(id=r.user_obj.id, name=r.user_obj.name),
        amount=r.amount,
        refunded_at=r.refunded_at,
        paid_by_user=UserMini(id=r.paid_by_user_obj.id, name=r.paid_by_user_obj.name) if r.paid_by_user_obj else None,
        replaced_by_user=UserMini(id=r.replaced_by_user_obj.id, name=r.replaced_by_user_obj.name) if r.replaced_by_user_obj else None,
        created_at=r.created_at,
    )


def _build_asset_response(a: SharedAsset) -> SharedAssetResponse:
    return SharedAssetResponse(
        id=a.id,
        household_id=a.household_id,
        name=a.name,
        description=a.description,
        photo_url=a.photo_url,
        purchase_date=a.purchase_date,
        total_cost=a.total_cost,
        requires_buyin_from_new_members=a.requires_buyin_from_new_members,
        status=a.status,
        bought_by_user=UserMini(id=a.bought_by_user.id, name=a.bought_by_user.name),
        created_at=a.created_at,
        updated_at=a.updated_at,
        contributions=[_build_contribution_response(c) for c in a.contributions],
        refunds=[_build_refund_response(r) for r in a.refunds],
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
            selectinload(SharedAsset.bought_by_user),
            selectinload(SharedAsset.contributions).selectinload(AssetContribution.user),
            selectinload(SharedAsset.refunds).selectinload(AssetRefund.user_obj),
            selectinload(SharedAsset.refunds).selectinload(AssetRefund.paid_by_user_obj),
            selectinload(SharedAsset.refunds).selectinload(AssetRefund.replaced_by_user_obj),
        )
    )
    return [_build_asset_response(a) for a in result.scalars().all()]


@router.get("/{asset_id}", response_model=SharedAssetResponse)
async def get_asset(
    asset_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SharedAssetResponse:
    try:
        asset = await get_asset_or_404(asset_id, current_user.household_id, db)
    except AssetNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return _build_asset_response(asset)


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
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    photo_url = await save_asset_photo(photo)

    try:
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
    except InvalidTotalCost as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except InvalidContributionSum as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except InvalidContributors as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _build_asset_response(asset)


@router.patch("/{asset_id}", response_model=SharedAssetResponse)
async def update_asset(
    asset_id: int,
    body: SharedAssetUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SharedAssetResponse:
    try:
        asset = await update_asset_svc(asset_id, current_user.household_id, body, current_user.id, db)
    except AssetNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return _build_asset_response(asset)


@router.post("/{asset_id}/dispose", response_model=SharedAssetResponse)
async def dispose_asset(
    asset_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SharedAssetResponse:
    try:
        asset = await dispose_asset_svc(asset_id, current_user.household_id, current_user.id, db)
    except AssetNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return _build_asset_response(asset)
