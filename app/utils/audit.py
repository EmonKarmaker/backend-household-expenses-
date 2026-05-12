"""Audit-log helpers.

Usage in a router:
    from app.utils.audit import log_audit, model_to_dict

    # before a delete:
    snap = model_to_dict(bill)
    await db.delete(bill)
    await log_audit(db, action="delete", entity="UtilityBill",
                    entity_id=bill.id, user_id=current_user.id, before=snap)

    # after a create:
    db.add(new_bill)
    await db.flush()   # populates new_bill.id
    await log_audit(db, action="create", entity="UtilityBill",
                    entity_id=new_bill.id, user_id=current_user.id,
                    after=model_to_dict(new_bill))
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import utcnow
from app.models.settlement import AuditLog


def _serialize(value: Any) -> Any:
    """Coerce types that aren't JSON-native to safe primitives."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def model_to_dict(
    obj: Any,
    exclude: frozenset[str] = frozenset({"password_hash"}),
) -> dict[str, Any]:
    """Snapshot a SQLAlchemy model instance into a plain, JSON-serializable dict.

    Only reads mapped column attributes — skips relationships and internal state.
    Call this *before* mutating or deleting the object so the snapshot is accurate.
    """
    mapper = sa_inspect(type(obj))
    return {
        col.key: _serialize(getattr(obj, col.key))
        for col in mapper.columns
        if col.key not in exclude
    }


async def log_audit(
    db: AsyncSession,
    *,
    action: Literal["create", "update", "delete"],
    entity: str,
    entity_id: int,
    user_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    note: str | None = None,
) -> None:
    """Append one row to audit_log inside the current transaction.

    Do NOT call db.commit() here — get_db owns the transaction boundary.
    If the surrounding request rolls back, this audit row rolls back with it,
    which is the correct behaviour (no phantom audit entries for failed writes).
    """
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before_data=before,
            after_data=after,
            note=note,
            at=utcnow(),
        )
    )
