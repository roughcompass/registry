"""Regression test for the dev-script fallback DATABASE_URL.

`scripts/bootstrap_dev_tenant.py` and `scripts/seed_dev_capabilities.py`
each hard-code a `_DOCKER_COMPOSE_DATABASE_URL` constant used as a
fallback when `DATABASE_URL` is unset — the "just-works against
`docker compose up`" convenience for new contributors.

The literal can drift from what docker-compose.yml actually provisions:
in 2026-05 the compose stack was renamed from `catalog` to `registry`
but both script constants kept pointing at `/catalog`, so `make
dev-token` failed with `database "catalog" does not exist` on a fresh
clone. This test pins both literals to the real compose values so a
future rename can't silently break the dev loop again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.engine.url import make_url

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed_dev_capabilities.py"


def _load_constant(script_path: Path) -> str:
    """Read `_DOCKER_COMPOSE_DATABASE_URL` from a script without running it.

    The scripts have top-level `from registry...` imports that require the
    full app dependency graph; parsing the constant out of the source
    keeps this test fast and avoids the side effects of import-time
    `sys.path` manipulation.
    """
    source = script_path.read_text(encoding="utf-8")
    match = re.search(
        r"^_DOCKER_COMPOSE_DATABASE_URL\s*=\s*\"(?P<url>[^\"]+)\"\s*$",
        source,
        re.MULTILINE,
    )
    assert match is not None, (
        f"{script_path.name} no longer defines _DOCKER_COMPOSE_DATABASE_URL — "
        "if the fallback shape changed, update this test."
    )
    return match.group("url")


def _parse_compose_postgres() -> tuple[str, int]:
    """Extract (POSTGRES_DB, host_port) from docker-compose.yml.

    Uses regex rather than PyYAML to avoid adding a test-only dependency.
    The compose file is small and stable; if its shape ever changes
    materially, this test will fail loudly and prompt an update.
    """
    source = _COMPOSE_FILE.read_text(encoding="utf-8")

    db_match = re.search(r"^\s*POSTGRES_DB:\s*(\S+)\s*$", source, re.MULTILINE)
    assert db_match is not None, "docker-compose.yml is missing POSTGRES_DB"
    db_name = db_match.group(1)

    port_match = re.search(r'"(\d+):5432"', source)
    assert port_match is not None, "docker-compose.yml is missing a 'HOST:5432' port mapping"
    host_port = int(port_match.group(1))

    return db_name, host_port


@pytest.fixture(scope="module")
def compose_postgres() -> tuple[str, int]:
    return _parse_compose_postgres()


@pytest.mark.parametrize(
    "script_path",
    [_BOOTSTRAP_SCRIPT, _SEED_SCRIPT],
    ids=["bootstrap_dev_tenant", "seed_dev_capabilities"],
)
def test_default_url_matches_compose(
    script_path: Path,
    compose_postgres: tuple[str, int],
) -> None:
    """Fallback literal must point at the database docker-compose creates.

    Drift on either side (compose renames the DB, or someone updates one
    script's literal but not the other's) trips this assertion.
    """
    expected_db, expected_port = compose_postgres
    url = make_url(_load_constant(script_path))

    assert url.database == expected_db, (
        f"{script_path.name} fallback points at database {url.database!r}, but "
        f"docker-compose.yml provisions {expected_db!r}. `make dev-token` will "
        f"fail with `database {url.database!r} does not exist` on a fresh clone."
    )
    assert url.port == expected_port, (
        f"{script_path.name} fallback uses port {url.port}, but docker-compose.yml "
        f"maps Postgres to host port {expected_port}."
    )
    assert url.host == "localhost", (
        f"{script_path.name} fallback host is {url.host!r}, expected 'localhost' "
        f"(the script runs on the host, not inside the compose network)."
    )
    assert url.drivername == "postgresql+asyncpg", (
        f"{script_path.name} fallback driver is {url.drivername!r}; the scripts "
        f"use SQLAlchemy's async engine and require the asyncpg driver."
    )


def test_both_scripts_use_the_same_default() -> None:
    """The two scripts must agree — they target the same compose stack."""
    bootstrap_url = _load_constant(_BOOTSTRAP_SCRIPT)
    seed_url = _load_constant(_SEED_SCRIPT)
    assert bootstrap_url == seed_url, (
        "bootstrap_dev_tenant.py and seed_dev_capabilities.py have diverged "
        "on their _DOCKER_COMPOSE_DATABASE_URL fallbacks. They must match — "
        "`make dev-token` then `make dev-seed` is a single workflow against "
        "one compose stack."
    )


def test_load_constant_helper_handles_missing_constant(tmp_path: Path) -> None:
    """The helper must fail loudly if a script drops the constant entirely."""
    fake_script = tmp_path / "no_constant.py"
    fake_script.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="no longer defines"):
        _load_constant(fake_script)
