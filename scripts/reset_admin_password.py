"""Interactively reset a user's password in the production database.

Can be invoked from any working directory — the script anchors to the
backend repo root so .env and app.* imports always resolve correctly.

    python scripts/reset_admin_password.py

Nothing sensitive is echoed, logged, or passed on the command line.
The password is deleted from memory as soon as it has been hashed.
"""
import getpass
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Anchor to the backend root before any app import.
# os.chdir ensures get_settings() finds .env at the expected relative path.
# sys.path insert ensures `app.*` is importable.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_ROOT)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Read ALEMBIC_DATABASE_URL from .env directly (no extra dependency needed).
# We use ALEMBIC_DATABASE_URL (postgresql://) not DATABASE_URL
# (postgresql+asyncpg://) because this script uses a synchronous engine.
# ---------------------------------------------------------------------------

_ENV_FILE = _ROOT / ".env"


def _read_env_var(key: str) -> str | None:
    """Return the value of `key` from .env, or None if absent/missing file."""
    if not _ENV_FILE.exists():
        return None
    for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            # Strip optional surrounding quotes (single or double)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            return v
    return None


# ---------------------------------------------------------------------------
# App imports — hash_password reuses the project's bcrypt setup exactly.
# Importing it also runs get_settings(); os.chdir(_ROOT) above makes that safe.
# ---------------------------------------------------------------------------

from sqlalchemy import create_engine, text       # noqa: E402  (after sys.path insert)
from app.utils.auth import hash_password          # noqa: E402

# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 8


def main() -> None:
    db_url = _read_env_var("ALEMBIC_DATABASE_URL")
    if not db_url:
        print(
            "Error: ALEMBIC_DATABASE_URL not found in .env — "
            f"expected file at {_ENV_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Collect email
    # ------------------------------------------------------------------
    email = input("User email: ").strip()
    if not email:
        print("Error: email is required.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(db_url)

    # ------------------------------------------------------------------
    # Phase 1: verify user exists (read-only — no transaction needed)
    # ------------------------------------------------------------------
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": email},
        ).fetchone()

    if row is None:
        print("User not found.")
        sys.exit(1)

    user_id: int = row[0]

    # ------------------------------------------------------------------
    # Phase 2: collect new password — hidden input, confirmed, min length
    # ------------------------------------------------------------------
    while True:
        new_password = getpass.getpass("New password: ")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            print(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters. "
                "Try again."
            )
            continue
        confirm = getpass.getpass("Confirm new password: ")
        if new_password != confirm:
            print("Passwords do not match. Try again.")
            continue
        break

    # ------------------------------------------------------------------
    # Phase 3: hash, update, then immediately delete the plaintext
    # ------------------------------------------------------------------
    hashed = hash_password(new_password)
    del new_password, confirm  # scrub plaintext from memory once hashed

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users"
                " SET password_hash = :hash, must_change_password = false"
                " WHERE id = :id"
            ),
            {"hash": hashed, "id": user_id},
        )

    print(f"Password reset successfully for {email}.")


if __name__ == "__main__":
    main()
