"""Committed openapi.json must match the spec generated from the live app.

If a router model changes shape (new field, type change), this test fails with
a JSON diff. The fix is to regenerate the committed spec via:

    python -c "import json; from registry.main import create_app; from registry.config import Settings; \
        s = Settings(database_url='...', pgbouncer_url='...', scheduler_jobstore_url='...'); \
        json.dump(create_app(s).openapi(), open('openapi.json','w'), indent=2, sort_keys=True)"

(or run the helper in `scripts/export_openapi.py` once it lands).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from registry.config import Settings
from registry.main import create_app

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_COMMITTED_SPEC = _PROJECT_ROOT / "openapi.json"


def test_committed_openapi_matches_generated() -> None:
    # Use any live DATABASE_URL — the spec doesn't actually hit the DB.
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:password@localhost:5432/cap_test",
    )
    settings = Settings(
        database_url=db_url,
        pgbouncer_url=db_url,
        scheduler_jobstore_url=db_url,
    )
    app = create_app(settings)
    generated = app.openapi()

    assert (
        _COMMITTED_SPEC.exists()
    ), f"{_COMMITTED_SPEC} is missing — regenerate via the helper documented in this file's docstring"
    committed = json.loads(_COMMITTED_SPEC.read_text())

    if generated != committed:
        msg_lines = [
            "openapi.json drifted from the live app's generated spec.",
            "Regenerate with:",
            "  python -c 'import json; from registry.main import create_app; from registry.config import Settings; "
            's = Settings(database_url="...", pgbouncer_url="...", scheduler_jobstore_url="..."); '
            'json.dump(create_app(s).openapi(), open("openapi.json","w"), indent=2, sort_keys=True)\'',
        ]
        raise AssertionError("\n".join(msg_lines))
