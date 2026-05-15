"""Tests for security deposit endpoints (app/services/deposit_svc.py + router)."""
from datetime import date
from decimal import Decimal

import pytest

from app.services.deposit_svc import (
    DepositAlreadyExists,
    DepositUserInactive,
    DepositUserNotFound,
    create_deposit_svc,
    list_deposits_svc,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _make_deposit(household, held_by, tenant, db, *, amount="5000.00"):
    return await create_deposit_svc(
        household_id=household.id,
        user_id=tenant.id,
        amount=Decimal(amount),
        deposited_at=date(2026, 1, 1),
        held_by_user_id=held_by.id,
        db=db,
    )


# ---------------------------------------------------------------------------
# Test 1 — Admin POST creates deposit → 201, all fields populated
# ---------------------------------------------------------------------------

async def test_admin_post_creates_deposit(db, household, make_room, make_user):
    """create_deposit_svc returns a fully populated SecurityDeposit."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    tenant = await make_user(household, room, name="Tenant")

    deposit = await _make_deposit(household, admin, tenant, db, amount="5000.00")

    assert deposit.id is not None
    assert deposit.user_id == tenant.id
    assert deposit.amount == Decimal("5000.00")
    assert deposit.deposited_at == date(2026, 1, 1)
    assert deposit.held_by_user_id == admin.id
    assert deposit.status == "held"
    assert deposit.deduction_amount == Decimal("0")
    assert deposit.applied_to_dues == Decimal("0")
    assert deposit.final_refund == Decimal("0")
    assert deposit.created_at is not None


# ---------------------------------------------------------------------------
# Test 2 — Duplicate held deposit → DepositAlreadyExists (409)
# ---------------------------------------------------------------------------

async def test_post_duplicate_held_deposit_raises(db, household, make_room, make_user):
    """Second create for the same user (status=held) raises DepositAlreadyExists."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    tenant = await make_user(household, room, name="Tenant")

    await _make_deposit(household, admin, tenant, db)

    with pytest.raises(DepositAlreadyExists):
        await _make_deposit(household, admin, tenant, db, amount="3000.00")


# ---------------------------------------------------------------------------
# Test 3 — User not in household → DepositUserNotFound (404)
# ---------------------------------------------------------------------------

async def test_post_user_not_in_household_raises(db, household, make_room, make_user):
    """Unknown user_id raises DepositUserNotFound."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")

    with pytest.raises(DepositUserNotFound):
        await create_deposit_svc(
            household_id=household.id,
            user_id=99_999,
            amount=Decimal("5000.00"),
            deposited_at=date(2026, 1, 1),
            held_by_user_id=admin.id,
            db=db,
        )


# ---------------------------------------------------------------------------
# Test 4 — Left user → DepositUserInactive (422)
# ---------------------------------------------------------------------------

async def test_post_left_user_raises(db, household, make_room, make_user):
    """User with left_at set raises DepositUserInactive."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    tenant = await make_user(household, room, name="ExTenant")
    tenant.left_at = date(2026, 3, 1)
    await db.flush()

    with pytest.raises(DepositUserInactive):
        await _make_deposit(household, admin, tenant, db)


# ---------------------------------------------------------------------------
# Test 5 — GET as admin returns SecurityDepositResponseFull
# ---------------------------------------------------------------------------

async def test_get_deposits_as_admin_returns_full(db, household, make_room, make_user):
    """Admin GET /deposits sees held_by_user_id and deduction fields."""
    from httpx import ASGITransport, AsyncClient

    from app.database import get_db
    from app.main import app
    from app.utils.permissions import get_current_user

    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    admin.is_admin = True
    await db.flush()
    tenant = await make_user(household, room, name="Tenant")
    await _make_deposit(household, admin, tenant, db, amount="4000.00")

    async def _db_override():
        yield db

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = lambda: admin

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/deposits")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert "held_by_user_id" in row
        assert "deduction_amount" in row
        assert "applied_to_dues" in row
        assert "final_refund" in row
        assert "created_at" in row
        assert row["amount"] == "4000.00"
        assert row["status"] == "held"
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Test 6 — GET as non-admin returns SecurityDepositResponsePublic
# ---------------------------------------------------------------------------

async def test_get_deposits_as_non_admin_returns_public(db, household, make_room, make_user):
    """Non-admin GET /deposits does NOT expose deduction fields."""
    from httpx import ASGITransport, AsyncClient

    from app.database import get_db
    from app.main import app
    from app.utils.permissions import get_current_user

    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    tenant = await make_user(household, room, name="Tenant")
    await _make_deposit(household, admin, tenant, db, amount="4000.00")

    async def _db_override():
        yield db

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = lambda: tenant

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/deposits")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert set(row.keys()) == {"id", "user_id", "amount", "deposited_at", "status"}
        assert "held_by_user_id" not in row
        assert "deduction_amount" not in row
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Test 7 — Cross-household GET returns empty list
# ---------------------------------------------------------------------------

async def test_cross_household_get_returns_empty(db, household, make_room, make_user):
    """list_deposits_svc with a foreign household_id returns []."""
    room = await make_room(household)
    admin = await make_user(household, room, name="Admin")
    tenant = await make_user(household, room, name="Tenant")
    await _make_deposit(household, admin, tenant, db)

    result = await list_deposits_svc(household_id=99_999, db=db)
    assert result == []
