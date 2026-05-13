"""Settlement mark-paid / unmark service.

Extracted from the router so tests can call it directly without HTTP.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settlement import Month, Settlement
from app.utils.audit import log_audit, model_to_dict


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------

class SettlementNotFound(Exception):
    pass


class MonthIsOpen(Exception):
    pass


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _load_settlement_with_month(
    settlement_id: int,
    household_id: int,
    db: AsyncSession,
) -> tuple[Settlement, Month]:
    """Load Settlement + its Month in one query, scoped to household.

    Raises SettlementNotFound on a missing settlement or cross-household access.
    """
    result = await db.execute(
        select(Settlement, Month)
        .join(Month, Settlement.month_id == Month.id)
        .where(
            Settlement.id == settlement_id,
            Month.household_id == household_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        raise SettlementNotFound(f"Settlement {settlement_id} not found")
    return row[0], row[1]


# ---------------------------------------------------------------------------
# Service function
# ---------------------------------------------------------------------------

async def mark_settlement_paid(
    settlement_id: int,
    household_id: int,
    paid: bool,
    marked_by_user_id: int,
    db: AsyncSession,
) -> Settlement:
    """Set or clear the paid flag on a settlement.

    paid=True  → sets paid, paid_at, paid_marked_by
    paid=False → clears all three (admin un-mark)

    Raises:
        SettlementNotFound — settlement doesn't exist or belongs to another household
        MonthIsOpen        — month must be closed before marking transfers as paid
    """
    settlement, month = await _load_settlement_with_month(settlement_id, household_id, db)

    if month.status == "open":
        raise MonthIsOpen(
            f"Cannot mark settlement paid in an open month ({month.id})"
        )

    before = model_to_dict(settlement)

    if paid:
        settlement.paid = True
        settlement.paid_at = _utcnow()
        settlement.paid_marked_by = marked_by_user_id
    else:
        settlement.paid = False
        settlement.paid_at = None
        settlement.paid_marked_by = None

    await db.flush()

    await log_audit(
        db,
        action="update",
        entity="Settlement",
        entity_id=settlement_id,
        user_id=marked_by_user_id,
        before=before,
        after=model_to_dict(settlement),
    )

    return settlement
