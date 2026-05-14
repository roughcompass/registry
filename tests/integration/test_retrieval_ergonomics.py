"""End-to-end tests for retrieval-ergonomics: name lookup + ?include= + external-id lookup.

Runs against the testcontainer Postgres. Each test bootstraps a fresh
tenant, seeds it, exercises the routes via ASGITransport, and asserts
on the response shapes.
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
from httpx import ASGITransport, AsyncClient

from registry.config import Settings
from registry.main import create_app

_REPO_ROOT = Path(__file__).parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed.py"

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
async def seeded_client(pg_container: str) -> AsyncGenerator[tuple[AsyncClient, str], None]:
    """Bootstrap tenant + seed + yield an authenticated ASGI client."""
    slug = "dx-retrieval"
    bootstrap = _run(pg_container, _BOOTSTRAP_SCRIPT, "--tenant-slug", slug)
    assert bootstrap.returncode == 0, bootstrap.stderr
    token = _parse_token(bootstrap.stdout)

    seed = _run(pg_container, _SEED_SCRIPT, "--tenant-slug", slug)
    assert seed.returncode == 0, seed.stderr

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
        yield client, token


@pytest.mark.asyncio
async def test_name_lookup_matches_uuid_lookup(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    """GET /v1/capabilities/salt-design-system and the same call with the UUID
    return identical bodies (modulo any timestamp jitter, which we don't expect
    on a single read)."""
    client, token = seeded_client
    h = {"Authorization": f"Bearer {token}"}

    by_name = await client.get("/v1/capabilities/salt-design-system", headers=h)
    assert by_name.status_code == 200, by_name.text
    body_by_name = by_name.json()

    uuid_str = body_by_name["entity_id"]
    by_uuid = await client.get(f"/v1/capabilities/{uuid_str}", headers=h)
    assert by_uuid.status_code == 200, by_uuid.text

    # The two responses must agree on every persisted field in the default view.
    # (`tenant_id` only appears under ?view=audit.)
    for field in ("entity_id", "name", "lifecycle", "attributes"):
        assert body_by_name[field] == by_uuid.json()[field], f"mismatch on {field}"


@pytest.mark.asyncio
async def test_slug_lookup_404_for_nonexistent(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.get(
        "/v1/capabilities/nonexistent-thing",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_slug_lookup_422_for_invalid_format(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    # Spaces, uppercase — not a valid slug.
    r = await client.get(
        "/v1/capabilities/Not%20A%20Valid%20Slug",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_include_components_expands_composes_edges(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.get(
        "/v1/capabilities/salt-design-system?include=components",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    comps = body.get("components") or {}
    items = comps.get("items", [])

    assert len(items) >= 16, f"expected ≥16 components, got {len(items)}"
    assert comps.get("truncated") is False
    # Each component carries its own attributes — the whole point of the
    # expansion is avoiding a per-component round-trip.
    component_names = {i["name"] for i in items}
    assert "salt-button" in component_names
    button = next(i for i in items if i["name"] == "salt-button")
    assert button["entity_type"] == "concept"
    assert "display_name" in button["attributes"]


@pytest.mark.asyncio
async def test_include_external_ids_returns_npm_and_github(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.get(
        "/v1/capabilities/salt-design-system?include=external_ids",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    items = r.json()["external_ids"]["items"]
    by_system = {i["external_system_slug"]: i["external_id"] for i in items}
    assert by_system.get("npm") == "@salt-ds/core"
    assert by_system.get("github") == "jpmorganchase/salt-ds"


@pytest.mark.asyncio
async def test_include_unknown_value_returns_422(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.get(
        "/v1/capabilities/salt-design-system?include=bogus",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    items = r.json()["errors"]
    assert len(items) == 1
    assert "bogus" in items[0]["message"]
    assert "components" in items[0]["message"]  # error message lists known values


@pytest.mark.asyncio
async def test_external_id_lookup_resolves_to_salt(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    """The npm package name resolves to Salt — the copilot use case end-to-end."""
    client, token = seeded_client
    r = await client.get(
        "/v1/entities?external_system=npm&external_id=@salt-ds/core",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "salt-design-system"


@pytest.mark.asyncio
async def test_one_call_mega_retrieval(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    """The N+1 collapse: one call yields the full picture an LLM/agent needs."""
    client, token = seeded_client
    r = await client.get(
        "/v1/capabilities/salt-design-system?include=components,external_ids,interface",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "salt-design-system"
    assert len(body["attributes"]) >= 10  # 11 attributes seeded
    assert len((body.get("components") or {}).get("items", [])) >= 16
    assert len((body.get("external_ids") or {}).get("items", [])) == 2
    # interface field is present even when no surface registered (empty).
    assert body.get("interface") is not None


@pytest.mark.asyncio
async def test_slug_validation_rejects_invalid_create(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    """POST with a non-slug name returns 422."""
    client, token = seeded_client
    r = await client.post(
        "/v1/capabilities",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"name": "Salt Design System", "entity_type": "capability"},
    )
    assert r.status_code == 422, r.text
    assert "name" in r.text.lower() or "slug" in r.text.lower()
