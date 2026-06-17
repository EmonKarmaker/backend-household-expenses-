"""Tests for the meal dispute flow (app/routers/meal_governance.py dispute_router).

HTTP-layer tests through the ASGI app with get_db / get_current_user
overridden — the same pattern as test_meal_permissions_api.py.
"""
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.utils.permissions import get_current_user

LOG_DAY = date(2026, 5, 15)


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


async def _raise(client, meal_log_id, reason="Did not eat this meal"):
    return await client.post(
        "/api/v1/meal-disputes",
        json={"meal_log_id": meal_log_id, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Test 1 — member raises a dispute → open, with log snapshot
# ---------------------------------------------------------------------------

async def test_member_raises_dispute(db, household, make_room, make_user, make_meal_log, api):
    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    target = await make_user(household, room, name="Target")
    log = await make_meal_log(target, LOG_DAY, 3)

    async with api(member) as client:
        resp = await _raise(client, log.id)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "open"
    assert data["household_id"] == household.id
    assert data["raised_by_user"]["id"] == member.id
    assert data["resolved_by_user"] is None
    assert data["resolved_at"] is None
    # Disputed-log snapshot for admin context.
    assert data["meal_log"]["id"] == log.id
    assert data["meal_log"]["user"]["id"] == target.id
    assert data["meal_log"]["meal_count"] == "3.00"


# ---------------------------------------------------------------------------
# Test 2 — dispute on a non-existent / cross-household log → 404
# ---------------------------------------------------------------------------

async def test_dispute_foreign_or_missing_log_404(db, household, make_room, make_user, make_meal_log, api):
    from app.models.core import Household

    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")

    other_hh = Household(name="Other")
    db.add(other_hh)
    await db.flush()
    other_room = await make_room(other_hh)
    outsider = await make_user(other_hh, other_room, name="Outsider")
    foreign_log = await make_meal_log(outsider, LOG_DAY, 2)

    async with api(member) as client:
        missing = await _raise(client, 999999)
        foreign = await _raise(client, foreign_log.id)

    assert missing.status_code == 404
    assert foreign.status_code == 404


# ---------------------------------------------------------------------------
# Test 3 — blank reason → 422
# ---------------------------------------------------------------------------

async def test_blank_reason_422(db, household, make_room, make_user, make_meal_log, api):
    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, LOG_DAY, 2)

    async with api(member) as client:
        empty = await _raise(client, log.id, reason="")
        whitespace = await _raise(client, log.id, reason="   ")

    assert empty.status_code == 422
    assert whitespace.status_code == 422


# ---------------------------------------------------------------------------
# Test 4 — duplicate open dispute by same user on same log → 409
# ---------------------------------------------------------------------------

async def test_duplicate_open_dispute_409(db, household, make_room, make_user, make_meal_log, api):
    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, LOG_DAY, 2)

    async with api(member) as client:
        first = await _raise(client, log.id)
        second = await _raise(client, log.id)

    assert first.status_code == 200
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Test 5 — admin resolves → resolved, resolved_by set
# ---------------------------------------------------------------------------

async def test_admin_resolves(db, household, make_room, make_user, make_meal_log, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, LOG_DAY, 2)

    async with api(member) as client:
        dispute_id = (await _raise(client, log.id)).json()["id"]
    async with api(admin) as client:
        resp = await client.post(f"/api/v1/meal-disputes/{dispute_id}/resolve")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["resolved_by_user"]["id"] == admin.id
    assert data["resolved_at"] is not None


# ---------------------------------------------------------------------------
# Test 6 — resolve an already-resolved dispute → 409
# ---------------------------------------------------------------------------

async def test_resolve_already_resolved_409(db, household, make_room, make_user, make_meal_log, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, LOG_DAY, 2)

    async with api(member) as client:
        dispute_id = (await _raise(client, log.id)).json()["id"]
    async with api(admin) as client:
        await client.post(f"/api/v1/meal-disputes/{dispute_id}/resolve")
        again = await client.post(f"/api/v1/meal-disputes/{dispute_id}/resolve")

    assert again.status_code == 409


# ---------------------------------------------------------------------------
# Test 7 — non-admin cannot resolve → 403
# ---------------------------------------------------------------------------

async def test_member_cannot_resolve_403(db, household, make_room, make_user, make_meal_log, api):
    _admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, LOG_DAY, 2)

    async with api(member) as client:
        dispute_id = (await _raise(client, log.id)).json()["id"]
        resp = await client.post(f"/api/v1/meal-disputes/{dispute_id}/resolve")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test 8 — list scoping: admin sees all; member sees own-raised + on-own-logs;
#          a member does NOT see an unrelated dispute between two others.
# ---------------------------------------------------------------------------

async def test_list_scoping(db, household, make_room, make_user, make_meal_log, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    alice = await make_user(household, room, name="Alice")
    bob = await make_user(household, room, name="Bob")
    carol = await make_user(household, room, name="Carol")

    alice_log = await make_meal_log(alice, LOG_DAY, 2)
    bob_log = await make_meal_log(bob, LOG_DAY, 2)
    carol_log = await make_meal_log(carol, LOG_DAY, 2)

    # (a) Alice raises a dispute on Bob's log → Alice raised it.
    async with api(alice) as client:
        await _raise(client, bob_log.id, reason="Bob didn't eat")
    # (b) Bob raises a dispute on Alice's log → on Alice's own log.
    async with api(bob) as client:
        await _raise(client, alice_log.id, reason="Alice over-claimed")
    # (c) Carol raises a dispute on Carol's own log → unrelated to Alice.
    async with api(carol) as client:
        await _raise(client, carol_log.id, reason="My mistake")

    # Admin sees all three.
    async with api(admin) as client:
        admin_list = (await client.get("/api/v1/meal-disputes")).json()
    assert len(admin_list) == 3

    # Alice sees (a) she raised + (b) on her log, but NOT (c).
    async with api(alice) as client:
        alice_list = (await client.get("/api/v1/meal-disputes")).json()
    seen_logs = {d["meal_log_id"] for d in alice_list}
    assert seen_logs == {bob_log.id, alice_log.id}
    assert carol_log.id not in seen_logs
    assert len(alice_list) == 2


# ---------------------------------------------------------------------------
# Test 9 — list filters (status / meal_log_id)
# ---------------------------------------------------------------------------

async def test_list_filters(db, household, make_room, make_user, make_meal_log, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log1 = await make_meal_log(member, LOG_DAY, 2)
    log2 = await make_meal_log(member, date(2026, 5, 16), 2)

    async with api(member) as client:
        d1 = (await _raise(client, log1.id)).json()["id"]
        await _raise(client, log2.id)
    async with api(admin) as client:
        await client.post(f"/api/v1/meal-disputes/{d1}/resolve")

        open_only = (await client.get("/api/v1/meal-disputes?status=open")).json()
        resolved_only = (await client.get("/api/v1/meal-disputes?status=resolved")).json()
        by_log = (await client.get(f"/api/v1/meal-disputes?meal_log_id={log2.id}")).json()

    assert len(open_only) == 1 and open_only[0]["meal_log_id"] == log2.id
    assert len(resolved_only) == 1 and resolved_only[0]["meal_log_id"] == log1.id
    assert len(by_log) == 1 and by_log[0]["meal_log_id"] == log2.id


# ---------------------------------------------------------------------------
# Test 10 — withdraw: own open → 204; others' (non-admin) → 403;
#           admin withdraws any open; withdraw resolved → 409.
# ---------------------------------------------------------------------------

async def test_withdraw_permissions(db, household, make_room, make_user, make_meal_log, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    other = await make_user(household, room, name="Other")
    log = await make_meal_log(member, LOG_DAY, 2)

    # Other member cannot withdraw a dispute they didn't raise → 403
    async with api(member) as client:
        dispute_id = (await _raise(client, log.id)).json()["id"]
    async with api(other) as client:
        forbidden = await client.delete(f"/api/v1/meal-disputes/{dispute_id}")
    assert forbidden.status_code == 403

    # The raiser withdraws their own open dispute → 204
    async with api(member) as client:
        own = await client.delete(f"/api/v1/meal-disputes/{dispute_id}")
    assert own.status_code == 204

    # Admin can withdraw any open dispute → 204
    async with api(member) as client:
        d2 = (await _raise(client, log.id)).json()["id"]
    async with api(admin) as client:
        admin_del = await client.delete(f"/api/v1/meal-disputes/{d2}")
    assert admin_del.status_code == 204

    # A resolved dispute cannot be withdrawn → 409
    async with api(member) as client:
        d3 = (await _raise(client, log.id)).json()["id"]
    async with api(admin) as client:
        await client.post(f"/api/v1/meal-disputes/{d3}/resolve")
        resolved_del = await client.delete(f"/api/v1/meal-disputes/{d3}")
    assert resolved_del.status_code == 409


# ---------------------------------------------------------------------------
# Test 11 — cross-household isolation
# ---------------------------------------------------------------------------

async def test_cross_household_isolation(db, household, make_room, make_user, make_meal_log, api):
    from app.models.core import Household

    admin, room = await _make_admin(household, make_room, make_user, db)
    member = await make_user(household, room, name="Member")
    log = await make_meal_log(member, LOG_DAY, 2)

    async with api(member) as client:
        dispute_id = (await _raise(client, log.id)).json()["id"]

    other_hh = Household(name="Other House")
    db.add(other_hh)
    await db.flush()
    other_room = await make_room(other_hh)
    outsider_admin = await make_user(other_hh, other_room, name="OutAdmin")
    outsider_admin.is_admin = True
    await db.flush()

    async with api(outsider_admin) as client:
        listing = (await client.get("/api/v1/meal-disputes")).json()
        resolve = await client.post(f"/api/v1/meal-disputes/{dispute_id}/resolve")
        delete = await client.delete(f"/api/v1/meal-disputes/{dispute_id}")

    assert listing == []
    assert resolve.status_code == 404
    assert delete.status_code == 404
