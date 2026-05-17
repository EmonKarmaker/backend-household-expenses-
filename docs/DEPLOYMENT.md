# Deployment Runbook — Household App Backend

## Prerequisites

- GitHub repository connected to your Render account
- Neon PostgreSQL database (both a pooled `DATABASE_URL` and a direct `ALEMBIC_DATABASE_URL`)
- Resend account with a verified sending domain
- Cloudinary account (free tier) with a cloud name, API key, and API secret
- A strong JWT secret: `python -c "import secrets; print(secrets.token_hex(32))"`

## Environment Variables

All 11 variables must be set in the Render dashboard (none are committed to git).

| Variable | What it is |
|---|---|
| `APP_ENV` | Set to `production` (already set in render.yaml) |
| `DATABASE_URL` | Neon **transaction pooler** URL (`postgres://user:pass@host:6543/db?sslmode=require`) — used by the async runtime |
| `ALEMBIC_DATABASE_URL` | Neon **session pooler** URL (`postgres://user:pass@host:5432/db?sslmode=require`) — used by Alembic migrations; must support DDL |
| `JWT_SECRET` | Random 64-hex-char string; signs all auth tokens |
| `RESEND_API_KEY` | Resend API key (`re_...`) for sending invite and password-reset emails |
| `EMAIL_FROM` | Verified sender address, e.g. `noreply@yourdomain.com` |
| `FRONTEND_URL` | Full URL of the frontend app, e.g. `https://household-app.vercel.app` |
| `CORS_ORIGINS` | Comma-separated allowed origins, e.g. `https://household-app.vercel.app` |
| `CLOUDINARY_CLOUD_NAME` | Your Cloudinary cloud name (visible on the dashboard homepage) |
| `CLOUDINARY_API_KEY` | Cloudinary API key |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret — treat as a password |

## Creating the Render Service

1. Log in to [render.com](https://render.com) and click **New → Blueprint**.
2. Connect your GitHub account if not already done.
3. Select the repository (`backend-household-expenses-`) and click **Connect**.
4. Render detects `render.yaml` and pre-fills the service name (`household-app-backend`) and region (Singapore).
5. Click **Apply** — Render creates the service but does **not** deploy yet.

Alternatively, create manually via **New → Web Service**:
- Source: GitHub repo
- Runtime: **Docker**
- Region: **Singapore**
- Dockerfile path: `./Dockerfile`
- Health check path: `/health`
- Plan: **Free**

## Setting Secrets in the Render Dashboard

1. Open the service → **Environment** tab.
2. For every variable marked `sync: false` in `render.yaml`, click **Add Environment Variable** and paste the value.
3. `APP_ENV` is pre-filled as `production` from `render.yaml` — verify it is present.
4. Click **Save Changes**. Render will queue a new deploy automatically.

## Triggering the First Deploy

After secrets are saved, either:
- Wait for Render's automatic deploy (triggered by the env var save), **or**
- Click **Manual Deploy → Deploy latest commit** from the service dashboard.

The deploy log will show:
```
Running database migrations...
INFO  [alembic.runtime.migration] Running upgrade ...
Starting server...
INFO:     Application startup complete.
```
A failed migration aborts startup (`set -e` in entrypoint.sh) and Render keeps the previous deploy running.

## Verifying the Deployment

Once the status badge shows **Live**, hit the health endpoint:
```bash
curl https://<your-render-url>/health
# Expected: {"status":"ok","env":"production"}
```

## Smoke-Test Sequence

Replace `BASE` with your Render service URL (e.g. `https://household-app-backend.onrender.com`).

```bash
# 1. Check setup state
curl $BASE/api/v1/setup/required

# 2. Initialise (first run only — creates household + admin)
curl -X POST $BASE/api/v1/setup/initialize \
  -H "Content-Type: application/json" \
  -d '{"household_name":"Test House","admin_name":"Admin","admin_email":"admin@example.com","admin_password":"changeme123","deposit_amount":"5000"}'

# 3. Login
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"changeme123"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 4. Create a utility bill
curl -X POST $BASE/api/v1/bills \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"month":"2026-05","utility_type":"electricity","amount":"1200","paid_by":<admin_user_id>}'

# 5. Check calculation
curl $BASE/api/v1/months/2026-05/summary \
  -H "Authorization: Bearer $TOKEN"
```

All responses must be 2xx with valid JSON.

## Rollback

Render retains all previous deploys. To roll back:

1. Open the service → **Events** tab.
2. Find the last known-good deploy.
3. Click **Rollback to this deploy**.

The previous container is live within ~30 seconds. Database migrations are forward-only (Alembic does not auto-downgrade), so roll back application code only — do not attempt `alembic downgrade` in production without a tested migration script.
