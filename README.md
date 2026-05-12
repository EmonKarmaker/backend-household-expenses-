# Household Expense Tracker — Backend

FastAPI + PostgreSQL + async SQLAlchemy. See `HOUSEHOLD_APP_PLAN.md` (one directory up) for the full plan.

## Setup

```bash
# 1. Python environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Configure environment
cp .env.example .env
# Edit .env with your DB credentials and a strong JWT_SECRET:
#   python -c "import secrets; print(secrets.token_hex(32))"

# 3. Create database (Postgres)
createdb household_dev

# 4. Generate the initial migration
alembic revision --autogenerate -m "initial schema"
# Inspect the generated file in alembic/versions/ before applying!

# 5. Apply migration
alembic upgrade head

# 6. Run the app
uvicorn app.main:app --reload
```

Visit `http://localhost:8000/docs` for the Swagger UI.

## Project layout

```
backend/
├── alembic/                # Migrations
├── app/
│   ├── main.py             # FastAPI app
│   ├── config.py           # Settings (pydantic-settings)
│   ├── database.py         # Async engine + Base + get_db
│   ├── models/             # SQLAlchemy models (17 tables)
│   ├── schemas/            # Pydantic request/response schemas
│   ├── routers/            # FastAPI routers, one per resource
│   ├── services/           # Business logic (calculation, settlement, leaving)
│   ├── utils/              # auth, audit, permissions
│   └── tests/              # pytest
├── scripts/                # seed_dev_data.py, backup_db.sh
└── pyproject.toml
```

## Weekend 1 — Remaining tasks

The scaffolding done by Claude (this skeleton) covers config, database, all 17 models, Alembic setup, auth utility, and main.py. Hand the rest to Claude Code:

### Generate the initial migration
```bash
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```
Review the generated migration file before applying. Check that all 17 tables and all enums are created.

### Build these routers (in order)

1. **`app/routers/setup.py`** — `GET /setup/required`, `POST /setup/initialize` (creates first household + admin)
2. **`app/routers/auth.py`** — login, change-password, forgot-password, reset-password
3. **`app/utils/permissions.py`** — `get_current_user`, `require_admin` dependencies
4. **`app/services/email.py`** — Resend client for invites and password resets
5. **`app/services/storage.py`** — photo upload with Pillow compression to 800px JPEG @ 85%
6. **`app/utils/audit.py`** — middleware/helper to write to `audit_log` on every mutation
7. **`app/routers/rooms.py`** — CRUD (admin-only writes)
8. **`app/routers/users.py`** — list, invite (with email), assign-room, transfer-admin
9. **`app/routers/bills.py`** — CRUD with photo upload
10. **`app/routers/shopping.py`** — CRUD with items[] in the same request
11. **`app/routers/meals.py`** — bulk upsert for the month calendar
12. **`app/routers/item_catalog.py`** — autocomplete

For each router, also create matching Pydantic schemas in `app/schemas/`.

## Lessons-learned reminders (from Dixon & AI Reservation)

- **Daily DB backups** — set up `pg_dump` to a free B2/R2 bucket on day 1
- **Decimal not float** — every money column is `Numeric(10, 2)`, every Python value is `Decimal`
- **UTC everywhere in DB**, convert to Asia/Dhaka only on display
- **Always `await db.rollback()`** in async session exception handlers (already in `get_db`)
- **Never echo raw env vars in logs** — mask DB URLs with scheme+host+port+"***"
- **Supabase pooler ports** — Session pooler (5432) for Alembic, Transaction pooler (6543) for the async runtime; username = `postgres.<PROJECT_REF>`

## Database backup

Set up daily backups before deploying:

```bash
# scripts/backup_db.sh
pg_dump $ALEMBIC_DATABASE_URL | gzip > backup_$(date +%Y%m%d).sql.gz
# Upload to Backblaze B2 or Cloudflare R2 (both have free tiers)
```

Set this as a daily cron on your machine or a Render cron job.
