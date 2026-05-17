#!/bin/sh
set -e

# Preflight: abort early with a legible message rather than a pydantic traceback
for var in DATABASE_URL ALEMBIC_DATABASE_URL JWT_SECRET RESEND_API_KEY EMAIL_FROM; do
    eval val=\$$var
    if [ -z "$val" ]; then
        echo "FATAL: required environment variable $var is not set. Aborting startup."
        exit 1
    fi
done

echo "Running database migrations..."
alembic upgrade head
echo "Starting server..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
