"""End-to-end test for the local-dev bootstrap script.

Runs `scripts/bootstrap_dev_tenant.py` against the testcontainer
Postgres, then uses the printed token to authenticate against a live
FastAPI app instance via ASGITransport. A second invocation verifies
idempotency — exactly one tenant + one actor exist regardless of how
many times the bootstrap runs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from registry.config import Settings
from registry.main import create_app

_REPO_ROOT = Path(__file__).parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"

_TOKEN_LINE_RE = re.compile(r"Token\s*:\s*(\S+)")


def _run_bootstrap(database_url: str, *extra_args: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(_BOOTSTRAP_SCRIPT), *extra_args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
        cwd=str(_REPO_ROOT),
        check=False,
    )


def _parse_token(stdout: str) -> str:
    match = _TOKEN_LINE_RE.search(stdout)
    if match is None:
        raise AssertionError(f"Could not find token line in output:\n{stdout}")
    return match.group(1)


@pytest_asyncio.fixture
async def app_client(pg_container: str) -> AsyncGenerator[AsyncClient, None]:
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_bootstrap_mints_working_token(pg_container: str, app_client: AsyncClient) -> None:
    """First run: token authenticates against /v1/capabilities."""
    result = _run_bootstrap(pg_container, "--tenant-slug", "dx-bootstrap-test")
    assert result.returncode == 0, (
        f"bootstrap_dev_tenant.py exited {result.returncode}\n" f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    token = _parse_token(result.stdout)
    assert token, "no token parsed from stdout"

    resp = await app_client.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, f"token did not authenticate: {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(pg_container: str) -> None:
    """Two runs share the same tenant + actor; each run mints a fresh token."""
    slug = "dx-bootstrap-idempotent"

    result1 = _run_bootstrap(pg_container, "--tenant-slug", slug)
    assert result1.returncode == 0, result1.stderr
    token1 = _parse_token(result1.stdout)

    result2 = _run_bootstrap(pg_container, "--tenant-slug", slug)
    assert result2.returncode == 0, result2.stderr
    token2 = _parse_token(result2.stdout)

    assert token1 != token2, "second run should mint a new token"

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
                    "SELECT COUNT(*) FROM actors a JOIN tenants t ON a.tenant_id = t.tenant_id "
                    "WHERE t.slug = :slug AND a.display_name = 'dev-admin'"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        token_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM api_tokens at JOIN tenants t ON at.tenant_id = t.tenant_id "
                    "WHERE t.slug = :slug"
                ),
                {"slug": slug},
            )
        ).scalar_one()
    await engine.dispose()

    assert tenant_count == 1, f"expected 1 tenant for slug {slug!r}, got {tenant_count}"
    assert actor_count == 1, f"expected 1 dev-admin actor for slug {slug!r}, got {actor_count}"
    assert token_count == 2, f"expected 2 tokens (one per run), got {token_count}"


@pytest.mark.asyncio
async def test_bootstrap_writes_env_file(pg_container: str, tmp_path: Path) -> None:
    """--write-env-file persists REGISTRY_DEV_TOKEN=<token> to the path."""
    env_file = tmp_path / ".env.dev"
    result = _run_bootstrap(
        pg_container,
        "--tenant-slug",
        "dx-bootstrap-envfile",
        "--write-env-file",
        str(env_file),
    )
    assert result.returncode == 0, result.stderr

    token = _parse_token(result.stdout)
    contents = env_file.read_text(encoding="utf-8")
    assert f"REGISTRY_DEV_TOKEN={token}" in contents, f"env file missing token line:\n{contents}"
