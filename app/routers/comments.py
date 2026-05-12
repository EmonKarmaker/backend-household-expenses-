"""Comments router — threaded comments on shopping entries, bills, and assets."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.assets import SharedAsset
from app.models.comments import Comment
from app.models.core import User
from app.models.expenses import ShoppingEntry, UtilityBill
from app.schemas.comments import CommentCreate, CommentResponse
from app.utils.audit import log_audit, model_to_dict
from app.utils.permissions import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

_VALID_ENTRY_TYPES = frozenset({"shopping", "bill", "asset"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _validate_entry_exists(
    entry_type: str,
    entry_id: int,
    household_id: int,
    db: AsyncSession,
) -> None:
    """Raise 404 if entry_id doesn't exist in this household for the given type."""
    if entry_type == "shopping":
        model = ShoppingEntry
    elif entry_type == "bill":
        model = UtilityBill
    elif entry_type == "asset":
        model = SharedAsset
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid entry_type",
        )
    found = await db.scalar(
        select(model.id).where(
            model.id == entry_id,
            model.household_id == household_id,
        )
    )
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referenced entry not found",
        )


async def _get_comment_or_404(
    comment_id: int,
    household_id: int,
    db: AsyncSession,
) -> Comment:
    """Return comment scoped to household (via the comment owner's household)."""
    result = await db.execute(
        select(Comment)
        .join(User, Comment.user_id == User.id)
        .where(Comment.id == comment_id, User.household_id == household_id)
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found",
        )
    return comment


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[CommentResponse])
async def list_comments(
    entry_type: str = Query(..., pattern=r"^(shopping|bill|asset)$"),
    entry_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CommentResponse]:
    await _validate_entry_exists(entry_type, entry_id, current_user.household_id, db)

    result = await db.execute(
        select(Comment)
        .where(Comment.entry_type == entry_type, Comment.entry_id == entry_id)
        .order_by(Comment.created_at, Comment.id)
    )
    return [CommentResponse.model_validate(c) for c in result.scalars().all()]


@router.post("", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    body: CommentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommentResponse:
    await _validate_entry_exists(body.entry_type, body.entry_id, current_user.household_id, db)

    comment = Comment(
        entry_type=body.entry_type,
        entry_id=body.entry_id,
        user_id=current_user.id,
        body=body.body,
    )
    db.add(comment)
    await db.flush()
    await db.refresh(comment)

    await log_audit(
        db,
        action="create",
        entity="Comment",
        entity_id=comment.id,
        user_id=current_user.id,
        after=model_to_dict(comment),
    )
    logger.info("Comment id=%s created by user_id=%s", comment.id, current_user.id)
    return CommentResponse.model_validate(comment)


@router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    comment = await _get_comment_or_404(comment_id, current_user.household_id, db)

    if not current_user.is_admin and comment.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete another user's comment",
        )

    before = model_to_dict(comment)
    comment_id_log = comment.id
    await db.delete(comment)

    await log_audit(
        db,
        action="delete",
        entity="Comment",
        entity_id=comment_id_log,
        user_id=current_user.id,
        before=before,
    )
    logger.info("Comment id=%s deleted by user_id=%s", comment_id_log, current_user.id)
