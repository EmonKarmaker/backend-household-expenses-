"""Tests for the standalone Grocery Fund ledger (app/routers/funds.py).

HTTP-layer tests: the balance/contribution logic lives in the router, so we
exercise it through the ASGI app with get_db / get_current_user overridden
(the same pattern as test_deposits_api.py and test_assets_api.py).
"""
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.funds import Fund
from app.utils.permissions import get_current_user


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api(db):
    """Return a factory that yields an AsyncClient authenticated as `user`.

    Calling the factory (re)points get_current_user at the given user, so a
    single test can act as different members in turn. Overrides are cleared
    at teardown.
    """
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


async def _make_fund(db, household, creator, *, name="Groceries", fund_status="active") -> Fund:
    fund = Fund(
        household_id=household.id,
        name=name,
        description=None,
        status=fund_status,
        created_by=creator.id,
    )
    db.add(fund)
    await db.flush()
    return fund


# ---------------------------------------------------------------------------
# Test 1 — admin can create a fund (201)
# ---------------------------------------------------------------------------

async def test_admin_creates_fund(db, household, make_room, make_user, api):
    admin, _ = await _make_admin(household, make_room, make_user, db)

    async with api(admin) as client:
        resp = await client.post("/api/v1/funds", json={"name": "Groceries", "description": "Shared food"})

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Groceries"
    assert data["status"] == "active"
    assert data["household_id"] == household.id
    assert data["created_by_user"]["id"] == admin.id
    assert data["total_deposits"] == "0.00"
    assert data["total_expenses"] == "0.00"
    assert data["balance"] == "0.00"
    assert data["contributions"] == []


# ---------------------------------------------------------------------------
# Test 2 — non-admin member cannot create a fund (403)
# ---------------------------------------------------------------------------

async def test_member_cannot_create_fund(db, household, make_room, make_user, api):
    room = await make_room(household)
    member = await make_user(household, room, name="Member")  # is_admin defaults to False

    async with api(member) as client:
        resp = await client.post("/api/v1/funds", json={"name": "Groceries"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin access required"


# ---------------------------------------------------------------------------
# Test 3 — deposits from two members → balance + contributions
# ---------------------------------------------------------------------------

async def test_two_member_deposits_balance_and_contributions(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    m2 = await make_user(household, room, name="Bob")
    fund = await _make_fund(db, household, admin)

    async with api(admin) as client:
        r1 = await client.post(
            f"/api/v1/funds/{fund.id}/deposits",
            json={"user_id": admin.id, "amount": "500.00", "deposited_at": "2026-05-01"},
        )
        r2 = await client.post(
            f"/api/v1/funds/{fund.id}/deposits",
            json={"user_id": m2.id, "amount": "300.00", "deposited_at": "2026-05-02"},
        )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["user"]["id"] == admin.id
    assert r1.json()["amount"] == "500.00"

    async with api(admin) as client:
        detail = await client.get(f"/api/v1/funds/{fund.id}")
    data = detail.json()
    assert data["total_deposits"] == "800.00"
    assert data["total_expenses"] == "0.00"
    assert data["balance"] == "800.00"

    contribs = {c["user"]["id"]: c["total"] for c in data["contributions"]}
    assert contribs == {admin.id: "500.00", m2.id: "300.00"}
    assert len(data["deposits"]) == 2


# ---------------------------------------------------------------------------
# Test 4 — logging an expense decreases the balance
# ---------------------------------------------------------------------------

async def test_expense_decreases_balance(db, household, make_room, make_user, api):
    admin, _ = await _make_admin(household, make_room, make_user, db)
    fund = await _make_fund(db, household, admin)

    async with api(admin) as client:
        await client.post(
            f"/api/v1/funds/{fund.id}/deposits",
            json={"user_id": admin.id, "amount": "500.00", "deposited_at": "2026-05-01"},
        )
        exp = await client.post(
            f"/api/v1/funds/{fund.id}/expenses",
            json={"amount": "120.00", "description": "Rice & oil", "spent_at": "2026-05-03"},
        )
        detail = await client.get(f"/api/v1/funds/{fund.id}")

    assert exp.status_code == 201
    assert exp.json()["created_by_user"]["id"] == admin.id
    assert exp.json()["shopping_entry_id"] is None
    data = detail.json()
    assert data["total_deposits"] == "500.00"
    assert data["total_expenses"] == "120.00"
    assert data["balance"] == "380.00"


# ---------------------------------------------------------------------------
# Test 5 — expense linked to a valid shopping entry
# ---------------------------------------------------------------------------

async def test_expense_with_shopping_entry_link(db, household, make_room, make_user, make_shopping_entry, api):
    admin, _ = await _make_admin(household, make_room, make_user, db)
    fund = await _make_fund(db, household, admin)
    entry = await make_shopping_entry(
        household, admin, "2026-05",
        [{"name": "Rice", "price": "100", "category": "meal"}],
    )

    async with api(admin) as client:
        resp = await client.post(
            f"/api/v1/funds/{fund.id}/expenses",
            json={
                "amount": "100.00",
                "description": "Linked groceries",
                "spent_at": "2026-05-04",
                "shopping_entry_id": entry.id,
            },
        )

    assert resp.status_code == 201
    assert resp.json()["shopping_entry_id"] == entry.id


# ---------------------------------------------------------------------------
# Test 6 — expense with a shopping_entry_id from another household → 422
# ---------------------------------------------------------------------------

async def test_expense_foreign_shopping_entry_rejected(db, household, make_room, make_user, make_shopping_entry, api):
    from app.models.core import Household

    admin, _ = await _make_admin(household, make_room, make_user, db)
    fund = await _make_fund(db, household, admin)

    # A shopping entry belonging to a *different* household.
    other_hh = Household(name="Other")
    db.add(other_hh)
    await db.flush()
    other_room = await make_room(other_hh)
    other_user = await make_user(other_hh, other_room, name="Stranger")
    foreign_entry = await make_shopping_entry(
        other_hh, other_user, "2026-05",
        [{"name": "Eggs", "price": "50", "category": "meal"}],
    )

    async with api(admin) as client:
        resp = await client.post(
            f"/api/v1/funds/{fund.id}/expenses",
            json={
                "amount": "50.00",
                "description": "Sneaky link",
                "spent_at": "2026-05-04",
                "shopping_entry_id": foreign_entry.id,
            },
        )

    assert resp.status_code == 422
    assert "household" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test 7 — delete deposit: by depositor, by admin, by unrelated member (403)
# ---------------------------------------------------------------------------

async def test_delete_deposit_permissions(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    depositor = await make_user(household, room, name="Depositor")
    other = await make_user(household, room, name="Other")
    fund = await _make_fund(db, household, admin)

    async def _add_deposit(actor):
        async with api(actor) as client:
            r = await client.post(
                f"/api/v1/funds/{fund.id}/deposits",
                json={"user_id": depositor.id, "amount": "100.00", "deposited_at": "2026-05-01"},
            )
        assert r.status_code == 201
        return r.json()["id"]

    # Unrelated member cannot delete someone else's deposit → 403
    dep_id = await _add_deposit(depositor)
    async with api(other) as client:
        resp = await client.delete(f"/api/v1/funds/{fund.id}/deposits/{dep_id}")
    assert resp.status_code == 403

    # The depositor can delete their own → 204
    async with api(depositor) as client:
        resp = await client.delete(f"/api/v1/funds/{fund.id}/deposits/{dep_id}")
    assert resp.status_code == 204

    # An admin can delete any deposit → 204
    dep_id2 = await _add_deposit(depositor)
    async with api(admin) as client:
        resp = await client.delete(f"/api/v1/funds/{fund.id}/deposits/{dep_id2}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Test 8 — delete expense: by creator and by admin
# ---------------------------------------------------------------------------

async def test_delete_expense_permissions(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    creator = await make_user(household, room, name="Creator")
    fund = await _make_fund(db, household, admin)

    async def _add_expense(actor):
        async with api(actor) as client:
            r = await client.post(
                f"/api/v1/funds/{fund.id}/expenses",
                json={"amount": "75.00", "description": "Snacks", "spent_at": "2026-05-05"},
            )
        assert r.status_code == 201
        return r.json()["id"]

    # Creator deletes own expense → 204
    exp_id = await _add_expense(creator)
    async with api(creator) as client:
        resp = await client.delete(f"/api/v1/funds/{fund.id}/expenses/{exp_id}")
    assert resp.status_code == 204

    # Admin deletes a member's expense → 204
    exp_id2 = await _add_expense(creator)
    async with api(admin) as client:
        resp = await client.delete(f"/api/v1/funds/{fund.id}/expenses/{exp_id2}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Test 9 — balance math with decimal amounts (500.00, 333.33)
# ---------------------------------------------------------------------------

async def test_balance_math_decimal(db, household, make_room, make_user, api):
    admin, room = await _make_admin(household, make_room, make_user, db)
    m2 = await make_user(household, room, name="Bob")
    fund = await _make_fund(db, household, admin)

    async with api(admin) as client:
        await client.post(
            f"/api/v1/funds/{fund.id}/deposits",
            json={"user_id": admin.id, "amount": "500.00", "deposited_at": "2026-05-01"},
        )
        await client.post(
            f"/api/v1/funds/{fund.id}/deposits",
            json={"user_id": m2.id, "amount": "333.33", "deposited_at": "2026-05-02"},
        )
        await client.post(
            f"/api/v1/funds/{fund.id}/expenses",
            json={"amount": "100.00", "description": "Veg", "spent_at": "2026-05-03"},
        )
        detail = await client.get(f"/api/v1/funds/{fund.id}")

    data = detail.json()
    # 500.00 + 333.33 = 833.33 deposited; 100.00 spent; balance = 733.33
    assert data["total_deposits"] == "833.33"
    assert data["total_expenses"] == "100.00"
    assert data["balance"] == "733.33"
    contribs = {c["user"]["id"]: c["total"] for c in data["contributions"]}
    assert contribs == {admin.id: "500.00", m2.id: "333.33"}


# ---------------------------------------------------------------------------
# Test 10 — cannot deposit to an archived fund (422)
# ---------------------------------------------------------------------------

async def test_cannot_deposit_to_archived_fund(db, household, make_room, make_user, api):
    admin, _ = await _make_admin(household, make_room, make_user, db)
    fund = await _make_fund(db, household, admin, fund_status="archived")

    async with api(admin) as client:
        resp = await client.post(
            f"/api/v1/funds/{fund.id}/deposits",
            json={"user_id": admin.id, "amount": "100.00", "deposited_at": "2026-05-01"},
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Cannot deposit to an archived fund"


# ---------------------------------------------------------------------------
# Test 11 — cross-household isolation: cannot see another household's fund (404)
# ---------------------------------------------------------------------------

async def test_cross_household_isolation(db, household, make_room, make_user, api):
    from app.models.core import Household

    admin, _ = await _make_admin(household, make_room, make_user, db)
    fund = await _make_fund(db, household, admin)

    other_hh = Household(name="Other House")
    db.add(other_hh)
    await db.flush()
    other_room = await make_room(other_hh)
    outsider = await make_user(other_hh, other_room, name="Outsider")

    # Outsider (different household) cannot GET or PATCH the fund.
    async with api(outsider) as client:
        get_resp = await client.get(f"/api/v1/funds/{fund.id}")
        # outsider is not admin, so PATCH would 403 before the 404; make them admin
        # of their own household to prove the isolation is by household, not role.
    assert get_resp.status_code == 404

    outsider.is_admin = True
    await db.flush()
    async with api(outsider) as client:
        patch_resp = await client.patch(f"/api/v1/funds/{fund.id}", json={"name": "Hijacked"})
        list_resp = await client.get("/api/v1/funds")
    assert patch_resp.status_code == 404
    assert list_resp.json() == []  # outsider's household has no funds
