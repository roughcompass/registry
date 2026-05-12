"""End-to-end test for the dev-seed script.

Runs bootstrap_dev_tenant.py + seed_dev_capabilities.py against the
testcontainer Postgres, then verifies:
- GET /v1/capabilities returns the 2 demo capabilities.
- POST /v1/capabilities with a standard payload succeeds (vocab seed
  removed the `unknown vocabulary value` rejection).
- Re-running seed_dev_capabilities.py is idempotent — capability count
  stays at 2 and entity_ids are identical.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
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
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed_dev_capabilities.py"

_TOKEN_LINE_RE = re.compile(r"Token\s*:\s*(\S+)")


def _run(database_url: str, script: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *extra],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
        cwd=str(_REPO_ROOT),
        check=False,
    )


def _parse_token(stdout: str) -> str:
    m = _TOKEN_LINE_RE.search(stdout)
    if m is None:
        raise AssertionError(f"no Token line in output:\n{stdout}")
    return m.group(1)


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
async def test_dev_seed_populates_list_and_unblocks_post(pg_container: str, app_client: AsyncClient) -> None:
    """bootstrap → seed → list returns 2 capabilities → POST works → re-seed idempotent."""
    slug = "dx-seed-test"

    bootstrap = _run(pg_container, _BOOTSTRAP_SCRIPT, "--tenant-slug", slug)
    assert bootstrap.returncode == 0, bootstrap.stderr
    token = _parse_token(bootstrap.stdout)

    seed = _run(pg_container, _SEED_SCRIPT, "--tenant-slug", slug)
    assert seed.returncode == 0, seed.stderr
    assert "salt-design-system" in seed.stdout
    assert "user-preferences" in seed.stdout

    # GET /v1/capabilities returns both top-level seeded rows (plus Salt's
    # component children, which are concept-kind entities surfaced by the
    # same list endpoint).
    headers = {"Authorization": f"Bearer {token}"}
    list_resp = await app_client.get("/v1/capabilities?page_size=50", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    names = {item["name"] for item in list_resp.json()["items"]}
    assert {"salt-design-system", "user-preferences"}.issubset(names), names

    # POST /v1/capabilities with the standard payload succeeds — vocab seed
    # removed the unknown-value rejection.
    create_resp = await app_client.post(
        "/v1/capabilities",
        headers={**headers, "Content-Type": "application/json"},
        json={"name": "integration-test-cap", "entity_type": "capability"},
    )
    assert create_resp.status_code == 201, create_resp.text
    assert "entity_id" in create_resp.json()

    # Re-running the seeder is a no-op for the demo capabilities — same UUIDs.
    list_resp2 = await app_client.get("/v1/capabilities?page_size=50", headers=headers)
    ids_before = {item["entity_id"] for item in list_resp2.json()["items"]}

    reseed = _run(pg_container, _SEED_SCRIPT, "--tenant-slug", slug)
    assert reseed.returncode == 0, reseed.stderr
    assert "already present" in reseed.stdout

    list_resp3 = await app_client.get("/v1/capabilities?page_size=50", headers=headers)
    ids_after = {item["entity_id"] for item in list_resp3.json()["items"]}
    assert ids_after == ids_before, "re-seed should leave entity_ids unchanged"


@pytest.mark.asyncio
async def test_dev_seed_enriches_salt_with_components_edges_facts(pg_container: str, app_client: AsyncClient) -> None:
    """Salt gets all four axes; user-preferences stays thin (contrast preserved)."""
    slug = "dx-seed-salt-enriched"

    bootstrap = _run(pg_container, _BOOTSTRAP_SCRIPT, "--tenant-slug", slug)
    assert bootstrap.returncode == 0, bootstrap.stderr
    token = _parse_token(bootstrap.stdout)

    seed = _run(pg_container, _SEED_SCRIPT, "--tenant-slug", slug)
    assert seed.returncode == 0, seed.stderr

    headers = {"Authorization": f"Bearer {token}"}

    # Properties axis: Salt has the version/package/framework attributes.
    list_resp = await app_client.get("/v1/capabilities?page_size=50", headers=headers)
    items = list_resp.json()["items"]
    salt = next((i for i in items if i["name"] == "salt-design-system"), None)
    user_prefs = next((i for i in items if i["name"] == "user-preferences"), None)
    assert salt is not None and user_prefs is not None

    salt_detail = (await app_client.get(f"/v1/capabilities/{salt['entity_id']}", headers=headers)).json()
    for key in ("current_version", "package_name", "framework", "license", "accessibility_compliance"):
        assert key in salt_detail["attributes"], f"Salt missing attribute {key!r}"

    # Contrast: user-preferences stays thin.
    user_prefs_detail = (await app_client.get(f"/v1/capabilities/{user_prefs['entity_id']}", headers=headers)).json()
    for key in ("current_version", "package_name"):
        assert key not in user_prefs_detail["attributes"], f"user-preferences should be thin; saw {key!r}"

    # Composition axis: ≥ 16 `composes` edges from Salt, and the list contains
    # the Salt components as concept-kind entities.
    component_names = {i["name"] for i in items if i["name"].startswith("salt-") and i["name"] != "salt-design-system"}
    assert len(component_names) >= 16, f"expected ≥16 Salt components, got {len(component_names)}"
    assert "salt-button" in component_names
    assert "salt-data-grid" in component_names

    salt_uuid = uuid.UUID(salt["entity_id"])
    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    async with engine.connect() as conn:
        edge_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM edges WHERE src_entity_id = :sid "
                    "AND rel = 'composes' AND t_invalidated_at IS NULL"
                ),
                {"sid": salt_uuid},
            )
        ).scalar_one()
        assert edge_count >= 16, f"expected ≥16 composes edges, got {edge_count}"

        # Narrative axis: Salt has an overview + a release_note fact.
        fact_rows = (
            await conn.execute(
                sqlalchemy.text("SELECT category FROM facts WHERE entity_id = :sid " "AND t_invalidated_at IS NULL"),
                {"sid": salt_uuid},
            )
        ).all()
        fact_categories = {row[0] for row in fact_rows}
        assert "overview" in fact_categories, f"missing overview fact; got {fact_categories}"
        assert "release_note" in fact_categories, f"missing release_note fact; got {fact_categories}"

        # Contrast: user-preferences has zero facts and zero outgoing edges.
        up_uuid = uuid.UUID(user_prefs["entity_id"])
        up_facts = (
            await conn.execute(
                sqlalchemy.text("SELECT COUNT(*) FROM facts WHERE entity_id = :uid AND t_invalidated_at IS NULL"),
                {"uid": up_uuid},
            )
        ).scalar_one()
        up_edges = (
            await conn.execute(
                sqlalchemy.text("SELECT COUNT(*) FROM edges WHERE src_entity_id = :uid AND t_invalidated_at IS NULL"),
                {"uid": up_uuid},
            )
        ).scalar_one()
        assert up_facts == 0, f"user-preferences should have 0 facts; got {up_facts}"
        assert up_edges == 0, f"user-preferences should have 0 outgoing edges; got {up_edges}"
    await engine.dispose()

    # Idempotency on the enriched axes: re-run should not duplicate edges or facts.
    reseed = _run(pg_container, _SEED_SCRIPT, "--tenant-slug", slug)
    assert reseed.returncode == 0, reseed.stderr

    engine2 = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    async with engine2.connect() as conn:
        edge_count2 = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM edges WHERE src_entity_id = :sid "
                    "AND rel = 'composes' AND t_invalidated_at IS NULL"
                ),
                {"sid": salt_uuid},
            )
        ).scalar_one()
    await engine2.dispose()
    assert edge_count2 == edge_count, "re-seed must not duplicate composes edges"
