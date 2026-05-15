"""Monthly expense calculation engine.

Pure read — no DB writes, no side effects.

calculate_month() aggregates all expense data for a given household/month
and returns a MonthSummary with per-user breakdowns using the
largest-remainder method to guarantee penny-perfect totals.
"""
import calendar
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import Room, RoomAssignment, User
from app.models.expenses import MealLog, ShoppingEntry, ShoppingItem, UtilityBill

CENT     = Decimal("0.01")
ZERO     = Decimal("0")
FOUR_DP  = Decimal("0.0001")

MoneyDecimal = Annotated[Decimal, PlainSerializer(str, return_type=str)]


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class UserMonthBreakdown(BaseModel):
    model_config = ConfigDict(json_encoders={Decimal: str})

    id: int
    name: str

    # Rent zone — paid directly to landlord, NOT part of inter-flatmate settlement.
    rent_owed: MoneyDecimal

    # Settlement zone — passes through the household pool; settles between flatmates.
    utility_owed: MoneyDecimal
    household_owed: MoneyDecimal
    personal_owed: MoneyDecimal
    meal_owed: MoneyDecimal
    settlement_owed: MoneyDecimal     # = util + household + personal + meal
    total_paid: MoneyDecimal          # what they paid through the pool
    settlement_balance: MoneyDecimal  # = total_paid - settlement_owed (sums to zero)

    # Legacy aggregates kept for the UI's "total owed this month" display.
    total_owed: MoneyDecimal          # rent_owed + settlement_owed
    balance: MoneyDecimal             # total_paid - total_owed (includes rent — does NOT sum to zero)


class MonthSummary(BaseModel):
    model_config = ConfigDict(json_encoders={Decimal: str})

    month_id: str
    household_id: int
    meal_pool: MoneyDecimal
    total_meals: MoneyDecimal
    cost_per_meal: MoneyDecimal
    users: list[UserMonthBreakdown]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _month_range(month_id: str) -> tuple[date, date]:
    year, month = int(month_id[:4]), int(month_id[5:])
    _, last_day_num = calendar.monthrange(year, month)
    return date(year, month, 1), date(year, month, last_day_num)


def _distribute(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Divide *total* among N items proportional to *weights*.

    Uses the largest-remainder method so that sum(result) == total exactly,
    regardless of how many decimal places the inputs have.
    """
    weight_sum = sum(weights)
    if weight_sum == ZERO:
        return [ZERO] * len(weights)

    raw     = [total * w / weight_sum for w in weights]
    floored = [r.quantize(CENT, rounding=ROUND_FLOOR) for r in raw]
    residual = total - sum(floored)

    n_extra = int((residual / CENT).quantize(Decimal("1")))
    n_extra = max(0, min(n_extra, len(weights)))
    if n_extra > 0:
        # Rank indices by fractional part descending; stable sort keeps original
        # order for ties (deterministic tiebreaker = lower index wins).
        ranked = sorted(
            range(len(raw)),
            key=lambda i: raw[i] - floored[i],
            reverse=True,
        )
        for j in range(n_extra):
            floored[ranked[j]] += CENT

    return floored


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def calculate_month(
    month_id: str,
    household_id: int,
    db: AsyncSession,
) -> MonthSummary:
    first_day, last_day = _month_range(month_id)

    # ------------------------------------------------------------------
    # 1. Active users
    #    joined_at <= last_day  AND  (left_at IS NULL OR left_at >= first_day)
    # ------------------------------------------------------------------
    result = await db.execute(
        select(User)
        .where(
            User.household_id == household_id,
            User.joined_at <= last_day,
            or_(User.left_at.is_(None), User.left_at >= first_day),
        )
        .order_by(User.id)
    )
    active_users = result.scalars().all()

    if not active_users:
        return MonthSummary(
            month_id=month_id,
            household_id=household_id,
            meal_pool=ZERO,
            total_meals=ZERO,
            cost_per_meal=ZERO,
            users=[],
        )

    user_ids  = [u.id for u in active_users]
    user_map  = {u.id: u for u in active_users}
    n_users   = len(user_ids)

    # ------------------------------------------------------------------
    # 2. Room assignments — latest effective_month <= month_id per user
    # ------------------------------------------------------------------
    from sqlalchemy import func  # local import avoids circular at module load

    subq = (
        select(
            RoomAssignment.user_id,
            func.max(RoomAssignment.effective_month).label("max_month"),
        )
        .where(
            RoomAssignment.user_id.in_(user_ids),
            RoomAssignment.effective_month <= month_id,
        )
        .group_by(RoomAssignment.user_id)
        .subquery()
    )

    result = await db.execute(
        select(RoomAssignment, Room)
        .join(
            subq,
            and_(
                RoomAssignment.user_id == subq.c.user_id,
                RoomAssignment.effective_month == subq.c.max_month,
            ),
        )
        .join(Room, RoomAssignment.room_id == Room.id)
    )
    rows = result.all()

    room_by_id:      dict[int, Room]      = {}
    room_occupants:  dict[int, list[int]] = {}  # room_id → [user_id, ...]

    for ra, room in rows:
        room_by_id[room.id] = room
        room_occupants.setdefault(room.id, [])
        if ra.user_id not in room_occupants[room.id]:
            room_occupants[room.id].append(ra.user_id)

    # ------------------------------------------------------------------
    # 3. Rent per user — largest-remainder per room
    # ------------------------------------------------------------------
    rent_owed: dict[int, Decimal] = {uid: ZERO for uid in user_ids}

    for room_id, occupant_ids in room_occupants.items():
        room = room_by_id[room_id]
        room_total = room.monthly_rent + room.monthly_service_charge
        if room_total == ZERO or not occupant_ids:
            continue
        shares = _distribute(room_total, [Decimal("1")] * len(occupant_ids))
        for uid, share in zip(occupant_ids, shares):
            rent_owed[uid] = share

    # ------------------------------------------------------------------
    # 4. Utility bills — sum, then split equally
    # ------------------------------------------------------------------
    result = await db.execute(
        select(UtilityBill.amount, UtilityBill.paid_by)
        .where(
            UtilityBill.household_id == household_id,
            UtilityBill.month == month_id,
        )
    )
    bill_rows = result.all()

    utility_pool                         = sum((r.amount for r in bill_rows), ZERO)
    utility_paid_by: dict[int, Decimal]  = {uid: ZERO for uid in user_ids}
    for r in bill_rows:
        if r.paid_by in utility_paid_by:
            utility_paid_by[r.paid_by] += r.amount

    utility_owed: dict[int, Decimal] = {uid: ZERO for uid in user_ids}
    if utility_pool > ZERO:
        shares = _distribute(utility_pool, [Decimal("1")] * n_users)
        for uid, share in zip(user_ids, shares):
            utility_owed[uid] = share

    # ------------------------------------------------------------------
    # 5. Shopping items — split by category
    #    meal      → pooled, split by meal proportion
    #    household → pooled, split equally
    #    personal  → charged directly to target_user_id
    # ------------------------------------------------------------------
    result = await db.execute(
        select(
            ShoppingItem.category,
            ShoppingItem.price,
            ShoppingItem.quantity,
            ShoppingItem.target_user_id,
            ShoppingEntry.paid_by,
        )
        .join(ShoppingEntry, ShoppingItem.entry_id == ShoppingEntry.id)
        .where(
            ShoppingEntry.household_id == household_id,
            ShoppingEntry.month == month_id,
        )
    )
    item_rows = result.all()

    meal_pool      = ZERO
    household_pool = ZERO
    personal_owed: dict[int, Decimal]    = {uid: ZERO for uid in user_ids}
    shopping_paid_by: dict[int, Decimal] = {uid: ZERO for uid in user_ids}

    for r in item_rows:
        line = r.price * r.quantity
        if r.paid_by in shopping_paid_by:
            shopping_paid_by[r.paid_by] += line

        if r.category == "meal":
            meal_pool += line
        elif r.category == "household":
            household_pool += line
        elif r.category == "personal" and r.target_user_id in personal_owed:
            personal_owed[r.target_user_id] += line

    household_owed: dict[int, Decimal] = {uid: ZERO for uid in user_ids}
    if household_pool > ZERO:
        shares = _distribute(household_pool, [Decimal("1")] * n_users)
        for uid, share in zip(user_ids, shares):
            household_owed[uid] = share

    # ------------------------------------------------------------------
    # 6. Meal logs — per-user totals for this month
    # ------------------------------------------------------------------
    result = await db.execute(
        select(MealLog.user_id, MealLog.meal_count, MealLog.guest_meals)
        .where(
            MealLog.user_id.in_(user_ids),
            MealLog.log_date >= first_day,
            MealLog.log_date <= last_day,
        )
    )
    log_rows = result.all()

    user_meals: dict[int, Decimal] = {uid: ZERO for uid in user_ids}
    for r in log_rows:
        user_meals[r.user_id] += r.meal_count + r.guest_meals

    total_meals  = sum(user_meals.values())
    cost_per_meal = (
        (meal_pool / total_meals).quantize(FOUR_DP)
        if total_meals > ZERO else ZERO
    )

    meal_owed: dict[int, Decimal] = {uid: ZERO for uid in user_ids}
    if meal_pool > ZERO and total_meals > ZERO:
        meal_weights = [user_meals[uid] for uid in user_ids]
        shares = _distribute(meal_pool, meal_weights)
        for uid, share in zip(user_ids, shares):
            meal_owed[uid] = share

    # ------------------------------------------------------------------
    # 7. Total paid (utilities + shopping)
    # ------------------------------------------------------------------
    total_paid: dict[int, Decimal] = {
        uid: utility_paid_by[uid] + shopping_paid_by[uid]
        for uid in user_ids
    }

    # ------------------------------------------------------------------
    # 8. Assemble breakdowns
    # ------------------------------------------------------------------
    breakdowns = []
    for uid in user_ids:
        r_owed = rent_owed[uid]
        u_owed = utility_owed[uid]
        h_owed = household_owed[uid]
        p_owed = personal_owed[uid]
        m_owed = meal_owed[uid]
        s_owed = u_owed + h_owed + p_owed + m_owed  # settlement zone only
        t_owed = r_owed + s_owed                    # everything
        t_paid = total_paid[uid]
        breakdowns.append(UserMonthBreakdown(
            id=uid,
            name=user_map[uid].name,
            rent_owed=r_owed.quantize(CENT),
            utility_owed=u_owed.quantize(CENT),
            household_owed=h_owed.quantize(CENT),
            personal_owed=p_owed.quantize(CENT),
            meal_owed=m_owed.quantize(CENT),
            settlement_owed=s_owed.quantize(CENT),
            total_paid=t_paid.quantize(CENT),
            settlement_balance=(t_paid - s_owed).quantize(CENT),
            total_owed=t_owed.quantize(CENT),
            balance=(t_paid - t_owed).quantize(CENT),
        ))

    return MonthSummary(
        month_id=month_id,
        household_id=household_id,
        meal_pool=meal_pool.quantize(CENT),
        total_meals=total_meals,
        cost_per_meal=cost_per_meal,
        users=breakdowns,
    )
