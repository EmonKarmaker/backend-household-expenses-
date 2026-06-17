"""Tests for grant-gated meal editing (app/routers/meals.py).

An admin editing ANOTHER member's meals needs an approved MealEditPermission
for that member + the log's month. Editing one's own meals is unrestricted.

Dates here are kept in the past (April/May 2026) so the bulk endpoint's
"more than 1 day in the future" guard never fires before the grant check.
"""
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db, utcnow
from app.main import app
from app.models.expenses import MealLog
from app.models.meal_governance import MealEditPermission
from app.utils.permissions import get_current_user

MAY = "2026-05"
APR = "2026-04"
MAY_DAY = date(2026, 5, 15)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api(db):
    async def _db_override():
        yield db

    def _factory(user) -> AsyncClient:
        app.dependency_overrides[get_db] = _db_override
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _factory

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


async def _make_admin(household, make_room, make_user, db, room=None):
    room = room or await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    return admin, room


async def _grant(db, household, admin, member, month) -> MealEditPermission:
    """Insert an already-approved grant for member + month."""
    perm = MealEditPermission(
        household_id=household.id,
        admin_user_id=admin.id,
        member_user_id=member.id,
        month=month,
        status="approved",
        resolved_at=utcnow(),
    )
    db.add(perm)
    await db.flush()
    return perm


def _entry(user_id, log_date=MAY_DAY, meal_count="2"):
    return {"user_id": user_id, "log_date": log_date.isoformat(), "meal_count": meal_count}


async def _meal_log_count(db, household_id) -> int:
    from sqlalchemy import func, select
    from app.models.core import User

    result = await db.execute(
        select(func.count(MealLog.id))
        .join(User, MealLog.user_id == User.id)
        .where(User.household_id == household_id)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# bulk_upsert gating
# ---------------------------------------------------------------------------

async def test_bulk_admin_without_grant_foreign_403(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        resp = await client.post("/api/v1/meals/bulk", json={"entries": [_entry(member.id)]})

    assert resp.status_code == 403
    assert f"user_id={member.id} in {MAY}" in resp.json()["detail"]
    assert await _meal_log_count(db, household.id) == 0


async def test_bulk_admin_with_grant_succeeds(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    await _grant(db, household, admin, member, MAY)

    async with api(admin) as client:
        resp = await client.post("/api/v1/meals/bulk", json={"entries": [_entry(member.id)]})

    assert resp.status_code == 200
    assert resp.json()[0]["user"]["id"] == member.id
    assert await _meal_log_count(db, household.id) == 1


async def test_bulk_admin_own_meals_no_grant_succeeds(db, household, make_room, make_user, api):
    admin, _ = await _make_admin(household, make_room, make_user, db)

    async with api(admin) as client:
        resp = await client.post("/api/v1/meals/bulk", json={"entries": [_entry(admin.id)]})

    assert resp.status_code == 200
    assert resp.json()[0]["user"]["id"] == admin.id


async def test_bulk_nonadmin_own_meals_succeeds(db, household, make_room, make_user, api):
    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(member) as client:
        resp = await client.post("/api/v1/meals/bulk", json={"entries": [_entry(member.id)]})

    assert resp.status_code == 200
    assert resp.json()[0]["user"]["id"] == member.id


async def test_bulk_nonadmin_foreign_403(db, household, make_room, make_user, api):
    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    other = await make_user(household, room, name="Other")

    async with api(member) as client:
        resp = await client.post("/api/v1/meals/bulk", json={"entries": [_entry(other.id)]})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Non-admin users can only upsert their own meal logs"


async def test_bulk_grant_is_month_specific_403(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    # Grant covers April, but the entry is in May.
    await _grant(db, household, admin, member, APR)

    async with api(admin) as client:
        resp = await client.post("/api/v1/meals/bulk", json={"entries": [_entry(member.id)]})

    assert resp.status_code == 403
    assert f"user_id={member.id} in {MAY}" in resp.json()["detail"]


async def test_bulk_mixed_own_and_foreign_all_or_nothing(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    # Own entry is fine, but the foreign entry has no grant → whole request 403,
    # and the admin's own entry must NOT be applied.
    async with api(admin) as client:
        resp = await client.post(
            "/api/v1/meals/bulk",
            json={"entries": [_entry(admin.id), _entry(member.id)]},
        )

    assert resp.status_code == 403
    assert f"user_id={member.id} in {MAY}" in resp.json()["detail"]
    assert await _meal_log_count(db, household.id) == 0  # nothing applied


# ---------------------------------------------------------------------------
# update_log / delete_log gating
# ---------------------------------------------------------------------------

async def test_update_log_admin_grant_gating(db, household, make_room, make_user, make_meal_log, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, MAY_DAY, 2)

    # Without a grant → 403
    async with api(admin) as client:
        resp = await client.patch(f"/api/v1/meals/{log.id}", json={"meal_count": "3"})
    assert resp.status_code == 403

    # With a grant for member + May → ok
    await _grant(db, household, admin, member, MAY)
    async with api(admin) as client:
        resp = await client.patch(f"/api/v1/meals/{log.id}", json={"meal_count": "3"})
    assert resp.status_code == 200
    assert resp.json()["meal_count"] == "3.00"


async def test_delete_log_admin_grant_gating(db, household, make_room, make_user, make_meal_log, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, MAY_DAY, 2)

    # Without a grant → 403
    async with api(admin) as client:
        resp = await client.delete(f"/api/v1/meals/{log.id}")
    assert resp.status_code == 403

    # With a grant → 204
    await _grant(db, household, admin, member, MAY)
    async with api(admin) as client:
        resp = await client.delete(f"/api/v1/meals/{log.id}")
    assert resp.status_code == 204


async def test_update_own_log_no_grant_succeeds(db, household, make_room, make_user, make_meal_log, api):
    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, MAY_DAY, 2)

    # Member edits their OWN log — unchanged behaviour, no grant needed.
    async with api(member) as client:
        resp = await client.patch(f"/api/v1/meals/{log.id}", json={"meal_count": "4"})
    assert resp.status_code == 200
    assert resp.json()["meal_count"] == "4.00"
