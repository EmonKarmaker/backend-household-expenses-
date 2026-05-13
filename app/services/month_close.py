"""Month close / reopen service.

Pure-DB operations with no HTTP concerns — testable directly.
"""
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settlement import Month, Settlement
from app.services.calculation import calculate_month
from app.services.settlement import minimize_transfers
from app.utils.audit import log_audit


# ---------------------------------------------------------------------------
# Domain errors — routers convert these to HTTP responses
# ---------------------------------------------------------------------------

class MonthAlreadyClosed(Exception):
    pass


class MonthAlreadyOpen(Exception):
    pass


class MonthNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def close_month(
    month_id: str,
    household_id: int,
    closed_by_user_id: int,
    db: AsyncSession,
) -> tuple[Month, list[Settlement]]:
    """Freeze a month and persist minimised settlement transfers.

    Steps:
      1. Load or create the Month row.
      2. Reject if already closed.
      3. Run the calculation engine.
      4. Minimise settlements via greedy algorithm.
      5. Delete any stale Settlement rows (idempotent re-close support).
      6. Insert fresh Settlement rows.
      7. Mark month closed.

    Returns the updated Month and the freshly loaded Settlement objects
    (re-fetched so server-side timestamps are populated).
    """
    result = await db.execute(
        select(Month).where(Month.id == month_id, Month.household_id == household_id)
    )
    month = result.scalar_one_or_none()

    if month is None:
        month = Month(id=month_id, household_id=household_id, status="open")
        db.add(month)
        await db.flush()
    elif month.status == "closed":
        raise MonthAlreadyClosed(f"Month {month_id} is already closed")

    # Calculate and minimise
    summary = await calculate_month(month_id, household_id, db)
    balances = {u.id: u.settlement_balance for u in summary.users}
    transfers = minimize_transfers(balances)

    # Delete stale rows (safe: FK CASCADE would also remove them on Month delete,
    # but here we want to replace them while keeping the Month row itself)
    await db.execute(delete(Settlement).where(Settlement.month_id == month_id))

    # Insert new rows
    new_rows = [
        Settlement(
            month_id=month_id,
            from_user=t.from_user_id,
            to_user=t.to_user_id,
            amount=t.amount,
            paid=False,
        )
        for t in transfers
    ]
    db.add_all(new_rows)
    await db.flush()

    # Re-fetch to get server-side created_at populated
    new_ids = [s.id for s in new_rows]
    result = await db.execute(
        select(Settlement).where(Settlement.id.in_(new_ids)).order_by(Settlement.id)
    )
    settlements = list(result.scalars().all())

    # Mark month closed
    month.status = "closed"
    month.closed_at = _utcnow()
    month.closed_by = closed_by_user_id
    await db.flush()

    return month, settlements


async def reopen_month(
    month_id: str,
    household_id: int,
    reopened_by_user_id: int,
    db: AsyncSession,
) -> Month:
    """Restore a closed month to open status.

    Existing Settlement rows are left in place as a historical record.
    When the month is closed again, close_month() will delete and regenerate them.
    """
    result = await db.execute(
        select(Month).where(Month.id == month_id, Month.household_id == household_id)
    )
    month = result.scalar_one_or_none()

    if month is None:
        raise MonthNotFound(f"Month {month_id} not found")
    if month.status == "open":
        raise MonthAlreadyOpen(f"Month {month_id} is already open")

    month.status = "open"
    month.closed_at = None
    month.closed_by = None
    await db.flush()

    await log_audit(
        db,
        action="update",
        entity="Month",
        entity_id=0,
        user_id=reopened_by_user_id,
        note=f"Month {month_id} reopened",
    )

    return month
