"""Export the FastAPI OpenAPI schema to docs/openapi.json.

Run from the repo root:  python scripts/export_openapi.py

This produces a committed API contract the frontend team consumes via
codegen (e.g. `npx openapi-typescript docs/openapi.json -o src/api/types.ts`).
Re-run and commit whenever the API surface changes.
"""
import json
import sys
from pathlib import Path

# Anchor to this project's backend root before any app import so the correct
# `app` package is found even when other projects are on sys.path.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Import the app WITHOUT starting the server or touching the database.
from app.main import app  # noqa: E402


def main() -> None:
    schema = app.openapi()
    out_dir = Path(__file__).resolve().parent.parent / "docs"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "openapi.json"
    out_path.write_text(json.dumps(schema, indent=2, sort_keys=False), encoding="utf-8")
    route_count = len(schema.get("paths", {}))
    print(f"Wrote {out_path} ({route_count} paths, {out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
