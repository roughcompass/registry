"""Re-export the committed ``openapi.json``.

Runs offline — no DB I/O — so it works in CI / dev without a real
``DATABASE_URL``. The committed ``openapi.json`` is the contract surface
for downstream consumers; ``tests/conformance/test_openapi_drift.py``
fails the build on any PR that changes a router model without re-running
this script.

Usage::

    python scripts/export_openapi.py

To regenerate a typed Python client from the export::

    pip install openapi-python-client
    openapi-python-client generate --path openapi.json --output-path <dir> --overwrite
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from registry.config import Settings
from registry.main import create_app

_OUT = Path(__file__).parent.parent / "openapi.json"


def main() -> int:
    # OpenAPI rendering runs offline (no DB I/O); fallback URL is unused.
    db_url = os.environ.get(  # config: intentional
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:password@localhost:5432/cap_test",
    )
    settings = Settings(
        database_url=db_url,
        pgbouncer_url=db_url,
        scheduler_jobstore_url=db_url,
    )
    app = create_app(settings)
    spec = app.openapi()
    _OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {_OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
