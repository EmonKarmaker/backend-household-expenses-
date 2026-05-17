# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what pip needs; .dockerignore keeps everything else out
COPY pyproject.toml .
COPY app/ ./app/

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Copy the fully-populated venv (includes the installed `app` package)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user for security
RUN useradd --no-create-home --shell /bin/sh appuser

WORKDIR /app

# Alembic migration files — needed at startup, not part of the Python package
COPY alembic/ ./alembic/
COPY alembic.ini .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER appuser

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
