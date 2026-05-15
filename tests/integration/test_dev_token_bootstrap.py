"""End-to-end test for the local-dev bootstrap script.

Runs `scripts/bootstrap_dev_tenant.py` against the testcontainer
Postgres and verifies the rows it produces. Mock-OIDC + mock-entitlement
seeding is skipped so the test doesn't depend on those services being
reachable.

The script is responsible for two things in the DB layer:

  * exactly one tenant row per slug,
  * exactly one actor row per (tenant, oidc_subject).

Re-running the bootstrap is supposed to be a no-op against existing
rows; the idempotency test pins that contract.

The env-file test checks that the keys the developer workflow depends
on (`DEV_TENANT_SLUG`, `CLIENT_ID`, `CLIENT_SECRET`, etc.) are written
to the path passed via `--env-file`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine

_REPO_ROOT = Path(__file__).parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"


def _run_bootstrap(
    database_url: str, env_file: Path, *extra_args: str
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(_BOOTSTRAP_SCRIPT),
        "--skip-mock-seed",
        "--env-file",
        str(env_file),
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
        cwd=str(_REPO_ROOT),
        check=False,
    )


@pytest.mark.asyncio
async def test_bootstrap_seeds_tenant_and_actor(
    pg_container: str, tmp_path: Path
) -> None:
    """First run inserts one tenant + one actor for the requested slug."""
    slug = "dx-bootstrap-seed"
    env_file = tmp_path / ".env.dev"

    result = _run_bootstrap(pg_container, env_file, "--tenant-slug", slug)
    assert result.returncode == 0, (
        f"bootstrap_dev_tenant.py exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    async with engine.connect() as conn:
        tenant_row = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT tenant_id, display_name, is_active "
                    "FROM tenants WHERE slug = :slug"
                ),
                {"slug": slug},
            )
        ).one()
        actor_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM actors a "
                    "JOIN tenants t ON a.tenant_id = t.tenant_id "
                    "WHERE t.slug = :slug AND a.oidc_subject = 'dev-admin'"
                ),
                {"slug": slug},
            )
        ).scalar_one()
    await engine.dispose()

    assert tenant_row.is_active is True
    assert tenant_row.display_name, "tenant display_name must be set"
    assert actor_count == 1


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(pg_container: str, tmp_path: Path) -> None:
    """Two runs share the same tenant_id + actor_id; no new rows on re-run."""
    slug = "dx-bootstrap-idempotent"
    env_file = tmp_path / ".env.dev"

    result1 = _run_bootstrap(pg_container, env_file, "--tenant-slug", slug)
    assert result1.returncode == 0, result1.stderr

    result2 = _run_bootstrap(pg_container, env_file, "--tenant-slug", slug)
    assert result2.returncode == 0, result2.stderr

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    async with engine.connect() as conn:
        tenant_count = (
            await conn.execute(
                sqlalchemy.text("SELECT COUNT(*) FROM tenants WHERE slug = :slug"),
                {"slug": slug},
            )
        ).scalar_one()
        actor_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM actors a "
                    "JOIN tenants t ON a.tenant_id = t.tenant_id "
                    "WHERE t.slug = :slug AND a.oidc_subject = 'dev-admin'"
                ),
                {"slug": slug},
            )
        ).scalar_one()
    await engine.dispose()

    assert tenant_count == 1, f"expected 1 tenant for slug {slug!r}, got {tenant_count}"
    assert actor_count == 1, (
        f"expected 1 dev-admin actor for slug {slug!r}, got {actor_count}"
    )

    # Env file from the second run must parse to the same tenant + actor IDs
    # as the row in Postgres — pins that "idempotent" really means stable.
    env_values = _parse_env_file(env_file)
    tenant_id = uuid.UUID(env_values["DEV_TENANT_ID"])
    actor_id = uuid.UUID(env_values["DEV_ACTOR_ID"])

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    async with engine.connect() as conn:
        db_tenant_id = (
            await conn.execute(
                sqlalchemy.text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                {"slug": slug},
            )
        ).scalar_one()
        db_actor_id = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT actor_id FROM actors "
                    "WHERE tenant_id = :tid AND oidc_subject = 'dev-admin'"
                ),
                {"tid": db_tenant_id},
            )
        ).scalar_one()
    await engine.dispose()

    assert uuid.UUID(str(db_tenant_id)) == tenant_id
    assert uuid.UUID(str(db_actor_id)) == actor_id


@pytest.mark.asyncio
async def test_bootstrap_writes_env_file(pg_container: str, tmp_path: Path) -> None:
    """--env-file persists tenant + mock-IDP credentials for the dev workflow."""
    env_file = tmp_path / ".env.dev"
    slug = "dx-bootstrap-envfile"

    result = _run_bootstrap(pg_container, env_file, "--tenant-slug", slug)
    assert result.returncode == 0, result.stderr

    values = _parse_env_file(env_file)
    assert values["DEV_TENANT_SLUG"] == slug
    assert values["DEV_USER_ID"] == "dev-admin"
    # UUIDs must parse — the workflow consumes them as opaque strings, but
    # malformed values would silently break callers like seed.py.
    uuid.UUID(values["DEV_TENANT_ID"])
    uuid.UUID(values["DEV_ACTOR_ID"])
    # Mock-IDP credentials are how the developer fetches a JWT.
    assert values["CLIENT_ID"]
    assert values["CLIENT_SECRET"]


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key] = value
    return out
