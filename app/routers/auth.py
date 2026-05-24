"""Authentication router.

Security rules enforced here:
- Passwords and tokens are NEVER written to any logger call.
- Login error messages are generic — never reveal whether the email or
  password was wrong (prevents user enumeration).
- bcrypt is ALWAYS called on login, even when the email doesn't exist,
  so response timing doesn't leak whether the address is registered.
- forgot-password always returns 200, even for unknown emails.
- Reset tokens are single-use secrets stored as SHA-256 hashes in the
  password_reset_tokens table; they expire after RESET_TOKEN_EXPIRY_HOURS.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.core import User
from app.models.password_reset_token import PasswordResetToken
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    ResetPasswordRequest,
    TokenResponse,
)
from app.schemas.core import UserResponse
from app.services.email import send_password_reset_email
from app.utils.audit import log_audit
from app.utils.auth import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.utils.permissions import get_current_user

settings = get_settings()
logger = logging.getLogger(__name__)
router = APIRouter()


def _user_resp(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        household_id=user.household_id,
        household_name=user.household.name,
        name=user.name,
        email=user.email,
        joined_at=user.joined_at,
        left_at=user.left_at,
        is_admin=user.is_admin,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )

# Computed once at import time. bcrypt is always called in login — even when
# no user with that email exists — so the response time is constant and
# doesn't leak whether the address is registered.
_DUMMY_HASH: str = hash_password("constant-time-timing-protection")


# ---------------------------------------------------------------------------
# Background-task helper
# ---------------------------------------------------------------------------

async def _send_reset_email_bg(to: str, reset_link: str) -> None:
    """Fire-and-forget wrapper for the password reset email.

    Errors are logged but not surfaced — the 200 response has already
    been sent by the time this runs.
    """
    try:
        await send_password_reset_email(to=to, reset_link=reset_link)
    except Exception:
        logger.error("Password reset email failed to deliver to %s", to)


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
    return _user_resp(current_user)


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

    return _user_resp(current_user)


@router.post("/forgot-password", status_code=status.HTTP_200_OK, response_model=MessageResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await db.execute(
        select(User).where(func.lower(User.email) == body.email.strip().lower())
    )
    user = result.scalar_one_or_none()

    if user is not None and user.left_at is None:
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=token_hash,
                expires_at=datetime.now(timezone.utc)
                + timedelta(hours=settings.reset_token_expiry_hours),
            )
        )
        reset_link = f"{settings.frontend_url}/reset-password?token={raw_token}"
        background_tasks.add_task(_send_reset_email_bg, to=user.email, reset_link=reset_link)
        logger.info("Password reset token created for user_id=%s", user.id)

    # Always return 200 — never reveal whether the email is registered.
    return MessageResponse(message="If an account with that email exists, a reset link has been sent.")


@router.post("/reset-password", status_code=status.HTTP_200_OK, response_model=MessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    prt_result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    prt = prt_result.scalar_one_or_none()

    if prt is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    user_result = await db.execute(select(User).where(User.id == prt.user_id))
    user = user_result.scalar_one_or_none()

    if user is None or user.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    prt.used_at = now

    await log_audit(
        db,
        action="update",
        entity="User",
        entity_id=user.id,
        user_id=user.id,
        note="password reset via email",
    )
    logger.info("Password reset completed for user_id=%s", user.id)

    return MessageResponse(message="Password reset successfully. You can now log in.")
