"""Setup router — first-run household initialization.

These two endpoints require NO authentication: the frontend hits
GET /setup/required before it knows whether any users exist at all.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.core import Household, User
from app.schemas.core import UserResponse
from app.schemas.setup import SetupInitRequest, SetupRequiredResponse
from app.utils.auth import hash_password

router = APIRouter()


async def _user_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(User))  # type: ignore[return-value]


@router.get("/required", response_model=SetupRequiredResponse)
async def setup_required(db: AsyncSession = Depends(get_db)) -> SetupRequiredResponse:
    """Return whether first-run setup is still needed."""
    return SetupRequiredResponse(required=await _user_count(db) == 0)


@router.post(
    "/initialize",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def setup_initialize(
    body: SetupInitRequest,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Create the first household and admin user atomically.

    Returns 409 if setup has already been completed — callers should redirect
    to /auth/login instead.  No audit log entry is written here because there
    is no user_id to attribute the action to before this endpoint succeeds.
    """
    if await _user_count(db) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup already complete. Use /api/v1/auth/login instead.",
        )

    household = Household(name=body.household_name)
    db.add(household)
    await db.flush()  # assigns household.id before we reference it below

    today_utc = datetime.now(timezone.utc).date()
    user = User(
        household_id=household.id,
        name=body.admin_name,
        email=body.admin_email,
        password_hash=hash_password(body.admin_password),
        joined_at=today_utc,
        is_admin=True,
        must_change_password=False,
    )
    db.add(user)
    await db.flush()  # assigns user.id before we build the response

    return UserResponse.model_validate(user)
