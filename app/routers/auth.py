"""Authentication router.

Security rules enforced here:
- Passwords and tokens are NEVER written to any logger call.
- Login error messages are generic — never reveal whether the email or
  password was wrong (prevents user enumeration).
- bcrypt is ALWAYS called on login, even when the email doesn't exist,
  so response timing doesn't leak whether the address is registered.
- forgot-password always returns 200, even for unknown emails.
- Reset tokens are short-lived JWTs with purpose=password_reset; a normal
  Bearer token cannot be used to reset a password.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.core import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    TokenResponse,
)
from app.schemas.core import UserResponse
from app.services.email import send_password_reset_email
from app.utils.audit import log_audit
from app.utils.auth import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.utils.permissions import get_current_user

settings = get_settings()
logger = logging.getLogger(__name__)
router = APIRouter()

# Computed once at import time. bcrypt is always called in login — even when
# no user with that email exists — so the response time is constant and
# doesn't leak whether the address is registered.
_DUMMY_HASH: str = hash_password("constant-time-timing-protection")


# ---------------------------------------------------------------------------
# Reset-token helpers (separate from the login JWT — purpose claim enforces it)
# ---------------------------------------------------------------------------

def _make_reset_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "purpose": "password_reset",
            "iat": now,
            "exp": now + timedelta(minutes=settings.password_reset_expire_minutes),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def _decode_reset_token(token: str) -> int | None:
    """Return user_id if the token is a valid, unexpired reset token; else None."""
    payload = decode_token(token)          # handles expiry + signature
    if payload is None:
        return None
    if payload.get("purpose") != "password_reset":
        return None                        # normal Bearer tokens rejected here
    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await db.execute(
        select(User).where(func.lower(User.email) == body.email.strip().lower())
    )
    user = result.scalar_one_or_none()

    # Always run bcrypt — constant response time regardless of whether the
    # email is registered. Use the real hash when found, dummy hash when not.
    candidate_hash = user.password_hash if user is not None else _DUMMY_HASH
    password_ok = verify_password(body.password, candidate_hash)

    if not password_ok or user is None or user.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user.id, user.is_admin)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(current_user)


@router.post("/change-password", response_model=UserResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    # FastAPI deduplicates get_db — current_user is already tracked by db.
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    current_user.password_hash = hash_password(body.new_password)
    current_user.must_change_password = False

    await log_audit(
        db,
        action="update",
        entity="User",
        entity_id=current_user.id,
        user_id=current_user.id,
        note="password changed",
    )
    logger.info("Password changed for user_id=%s", current_user.id)

    return UserResponse.model_validate(current_user)


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    result = await db.execute(
        select(User).where(func.lower(User.email) == body.email.strip().lower())
    )
    user = result.scalar_one_or_none()

    if user is not None and user.left_at is None:
        token = _make_reset_token(user.id)
        reset_url = f"{settings.frontend_url}/reset-password?token={token}"
        try:
            await send_password_reset_email(
                to_email=user.email,
                to_name=user.name,
                reset_token=token,    # never logged inside that function
                reset_url=reset_url,
            )
            logger.info("Password reset email dispatched for user_id=%s", user.id)
        except RuntimeError:
            # Log the failure but don't surface it — the response must be
            # identical whether the email exists or not.
            logger.error("Password reset email failed for user_id=%s", user.id)

    # Always return 200 — never reveal whether the email is registered.
    return {"message": "If that email is registered, you will receive a reset link shortly."}


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user_id = _decode_reset_token(body.token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or user.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False

    await log_audit(
        db,
        action="update",
        entity="User",
        entity_id=user.id,
        user_id=user.id,
        note="password reset via email",
    )
    logger.info("Password reset completed for user_id=%s", user.id)

    return {"message": "Password updated successfully."}
