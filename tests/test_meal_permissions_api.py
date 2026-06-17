"""Tests for the meal-edit permission flow (app/routers/meal_governance.py).

HTTP-layer tests through the ASGI app with get_db / get_current_user
overridden — the same pattern as test_funds_api.py. The `has_meal_edit_permission`
helper is exercised directly against the session.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.meal_governance import MealEditPermission
from app.routers.meal_governance import has_meal_edit_permission
from app.utils.permissions import get_current_user


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api(db):
    """Factory yielding an AsyncClient authenticated as the given user."""
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


async def _request(client, member_id, month="2026-05"):
    return await client.post(
        "/api/v1/meal-permissions",
        json={"member_user_id": member_id, "month": month},
    )


# ---------------------------------------------------------------------------
# Test 1 — admin requests permission for a member → pending
# ---------------------------------------------------------------------------

async def test_admin_requests_permission(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        resp = await _request(client, member.id)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["household_id"] == household.id
    assert data["admin_user"]["id"] == admin.id
    assert data["member_user"]["id"] == member.id
    assert data["month"] == "2026-05"
    assert data["reject_reason"] is None
    assert data["resolved_at"] is None


# ---------------------------------------------------------------------------
# Test 2 — admin cannot request permission for themselves → 400
# ---------------------------------------------------------------------------

async def test_admin_cannot_request_for_self(db, household, make_room, make_user, api):
    admin, _ = await _make_admin(household, make_room, make_user, db)

    async with api(admin) as client:
        resp = await _request(client, admin.id)

    assert resp.status_code == 400
    assert "own meals" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test 3 — admin cannot request for a non-household member → 404
# ---------------------------------------------------------------------------

async def test_request_for_outsider_404(db, household, make_room, make_user, api):
    from app.models.core import Household

    admin, _ = await _make_admin(household, make_room, make_user, db)
    other_hh = Household(name="Other")
    db.add(other_hh)
    await db.flush()
    other_room = await make_room(other_hh)
    outsider = await make_user(other_hh, other_room, name="Outsider")

    async with api(admin) as client:
        resp = await _request(client, outsider.id)

    assert resp.status_code == 404
    assert "household" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test 4 — non-admin member cannot request permission → 403
# ---------------------------------------------------------------------------

async def test_member_cannot_request_permission(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    target = await make_user(household, room, name="Target")

    async with api(member) as client:
        resp = await _request(client, target.id)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin access required"


# ---------------------------------------------------------------------------
# Test 5 — duplicate pending request → 409
# ---------------------------------------------------------------------------

async def test_duplicate_pending_request_409(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        first = await _request(client, member.id)
        second = await _request(client, member.id)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "pending" in second.json()["detail"]


# ---------------------------------------------------------------------------
# Test 6 — member approves → approved; helper returns True
# ---------------------------------------------------------------------------

async def test_member_approves(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]

    async with api(member) as client:
        resp = await client.post(f"/api/v1/meal-permissions/{perm_id}/approve")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["resolved_at"] is not None

    assert await has_meal_edit_permission(db, household.id, member.id, "2026-05") is True


# ---------------------------------------------------------------------------
# Test 7 — already-approved re-request → 409
# ---------------------------------------------------------------------------

async def test_request_when_already_approved_409(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]
    async with api(member) as client:
        await client.post(f"/api/v1/meal-permissions/{perm_id}/approve")
    async with api(admin) as client:
        resp = await _request(client, member.id)

    assert resp.status_code == 409
    assert "already granted" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test 8 — member rejects with reason → rejected, reason stored
# ---------------------------------------------------------------------------

async def test_member_rejects_with_reason(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]

    async with api(member) as client:
        resp = await client.post(
            f"/api/v1/meal-permissions/{perm_id}/reject",
            json={"reason": "I logged these correctly"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert data["reject_reason"] == "I logged these correctly"
    assert data["resolved_at"] is not None
    assert await has_meal_edit_permission(db, household.id, member.id, "2026-05") is False


# ---------------------------------------------------------------------------
# Test 9 — re-request after rejection → allowed, back to pending
# ---------------------------------------------------------------------------

async def test_rerequest_after_rejection(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]
    async with api(member) as client:
        await client.post(
            f"/api/v1/meal-permissions/{perm_id}/reject", json={"reason": "no"}
        )

    async with api(admin) as client:
        resp = await _request(client, member.id)

    assert resp.status_code == 200
    data = resp.json()
    # Same record reset, not a duplicate.
    assert data["id"] == perm_id
    assert data["status"] == "pending"
    assert data["reject_reason"] is None
    assert data["resolved_at"] is None


# ---------------------------------------------------------------------------
# Test 10 — non-target member cannot approve/reject → 403
# ---------------------------------------------------------------------------

async def test_non_target_cannot_resolve(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    other = await make_user(household, room, name="Other")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]

    async with api(other) as client:
        approve = await client.post(f"/api/v1/meal-permissions/{perm_id}/approve")
        reject = await client.post(
            f"/api/v1/meal-permissions/{perm_id}/reject", json={}
        )

    assert approve.status_code == 403
    assert reject.status_code == 403


# ---------------------------------------------------------------------------
# Test 11 — admin cannot approve their own request → 403 (only the member can)
# ---------------------------------------------------------------------------

async def test_admin_cannot_approve_own_request(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]
        resp = await client.post(f"/api/v1/meal-permissions/{perm_id}/approve")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test 12 — approve/reject a non-pending request → 409
# ---------------------------------------------------------------------------

async def test_resolve_non_pending_409(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]
    async with api(member) as client:
        await client.post(f"/api/v1/meal-permissions/{perm_id}/approve")
        # Already approved → rejecting now is a conflict.
        resp = await client.post(
            f"/api/v1/meal-permissions/{perm_id}/reject", json={}
        )

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Test 13 — GET scoping: member sees only their own, admin sees all
# ---------------------------------------------------------------------------

async def test_list_scoping_by_role(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    m1 = await make_user(household, room, name="M1")
    m2 = await make_user(household, room, name="M2")

    async with api(admin) as client:
        await _request(client, m1.id)
        await _request(client, m2.id)
        admin_list = (await client.get("/api/v1/meal-permissions")).json()

    # Admin sees both records.
    assert len(admin_list) == 2

    # m1 sees only the request directed at m1.
    async with api(m1) as client:
        m1_list = (await client.get("/api/v1/meal-permissions")).json()
    assert len(m1_list) == 1
    assert m1_list[0]["member_user"]["id"] == m1.id


# ---------------------------------------------------------------------------
# Test 14 — GET filters (month / status / member_user_id)
# ---------------------------------------------------------------------------

async def test_list_filters(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    m1 = await make_user(household, room, name="M1")
    m2 = await make_user(household, room, name="M2")

    async with api(admin) as client:
        await _request(client, m1.id, month="2026-05")
        await _request(client, m2.id, month="2026-06")

        by_month = (await client.get("/api/v1/meal-permissions?month=2026-05")).json()
        by_member = (
            await client.get(f"/api/v1/meal-permissions?member_user_id={m2.id}")
        ).json()
        by_status = (await client.get("/api/v1/meal-permissions?status=pending")).json()

    assert len(by_month) == 1 and by_month[0]["month"] == "2026-05"
    assert len(by_member) == 1 and by_member[0]["member_user"]["id"] == m2.id
    assert len(by_status) == 2


# ---------------------------------------------------------------------------
# Test 15 — helper returns False when no approved grant exists
# ---------------------------------------------------------------------------

async def test_helper_false_without_grant(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    # No request at all.
    assert await has_meal_edit_permission(db, household.id, member.id, "2026-05") is False

    # A pending request is still not an approval.
    async with api(admin) as client:
        await _request(client, member.id)
    assert await has_meal_edit_permission(db, household.id, member.id, "2026-05") is False

    # Different month is not covered by an approval for 2026-05.
    perm = (
        await db.execute(
            MealEditPermission.__table__.select().where(
                MealEditPermission.member_user_id == member.id
            )
        )
    ).first()
    assert perm is not None
    assert await has_meal_edit_permission(db, household.id, member.id, "2026-06") is False


# ---------------------------------------------------------------------------
# Test 16 — cross-household isolation
# ---------------------------------------------------------------------------

async def test_cross_household_isolation(db, household, make_room, make_user, api):
    from app.models.core import Household

    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    async with api(admin) as client:
        perm_id = (await _request(client, member.id)).json()["id"]

    other_hh = Household(name="Other House")
    db.add(other_hh)
    await db.flush()
    other_room = await make_room(other_hh)
    outsider_admin = await make_user(other_hh, other_room, name="OutAdmin")
    outsider_admin.is_admin = True
    await db.flush()

    async with api(outsider_admin) as client:
        # Cannot see the foreign request, nor approve/reject it.
        listing = (await client.get("/api/v1/meal-permissions")).json()
        approve = await client.post(f"/api/v1/meal-permissions/{perm_id}/approve")

    assert listing == []
    assert approve.status_code == 404
