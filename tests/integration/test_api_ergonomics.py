"""End-to-end tests for the api-ergonomics phase.

Covers the user-visible features shipped in ERG-T02 through ERG-T11:
- /whoami session context
- Structured error envelope on 4xx
- UI-flavoured default response (no bitemporal cols) + ?view=audit
- Artifact list pagination + filters + sparse fields
- HATEOAS _links on detail responses + whoami
- ETag emission + If-Match precondition
- X-Idempotency-Key replay + conflict
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
    slug = "dx-ergonomics"
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
async def test_whoami_returns_session_context(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.get("/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    for field in (
        "actor_id",
        "actor_display_name",
        "tenant_id",
        "tenant_slug",
        "tenant_display_name",
        "roles",
    ):
        assert field in body, f"whoami missing {field}"
    assert body["tenant_slug"] == "dx-ergonomics"
    assert body["actor_display_name"] == "dev-admin"
    assert "admin" in body["roles"]
    # _links pointers present
    assert body["_links"]["self"] == "/v1/whoami"


@pytest.mark.asyncio
async def test_error_envelope_on_404(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.get(
        "/v1/capabilities/nonexistent-thing",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
    body = r.json()
    assert "errors" in body
    assert body["errors"][0]["code"] == "not_found"
    assert "message" in body["errors"][0]


@pytest.mark.asyncio
async def test_default_response_excludes_bitemporal_cols(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    body = (
        await client.get(
            "/v1/capabilities/salt-design-system",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    # Default view strips these.
    for forbidden in ("tenant_id", "is_active", "superseded_facts_count"):
        assert forbidden not in body, f"default view leaks {forbidden}"
    for fact in body.get("facts", []):
        for forbidden in ("tenant_id", "entity_id", "is_authoritative", "valid_from", "ingested_at"):
            assert forbidden not in fact, f"default fact leaks {forbidden}"


@pytest.mark.asyncio
async def test_audit_view_includes_bitemporal_cols(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    body = (
        await client.get(
            "/v1/capabilities/salt-design-system?view=audit",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    for required in ("tenant_id", "is_active"):
        assert required in body, f"audit view missing {required}"


@pytest.mark.asyncio
async def test_capability_response_has_links(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    body = (
        await client.get(
            "/v1/capabilities/salt-design-system",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    links = body["_links"]
    assert links["self"] == "/v1/capabilities/salt-design-system"
    assert links["artifacts"] == "/v1/capabilities/salt-design-system/artifacts"
    assert links["dependencies"] == "/v1/capabilities/salt-design-system/dependencies"


@pytest.mark.asyncio
async def test_get_capability_emits_etag(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    etag = r.headers.get("etag")
    assert etag is not None
    assert etag.startswith('W/"') and etag.endswith('"')


@pytest.mark.asyncio
async def test_patch_visibility_stale_if_match_returns_412(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    r = await client.patch(
        "/v1/capabilities/salt-design-system/visibility",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "If-Match": 'W/"definitely-stale"',
        },
        json={"visibility": "private"},
    )
    assert r.status_code == 412
    assert r.json()["errors"][0]["code"] == "precondition_failed"


@pytest.mark.asyncio
async def test_idempotency_key_replays_first_response(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": "erg-test-key-1",
    }
    body = {"name": "erg-idempotency-cap", "entity_type": "capability"}

    r1 = await client.post("/v1/capabilities", headers=h, json=body)
    assert r1.status_code == 201
    first_id = r1.json()["entity_id"]

    r2 = await client.post("/v1/capabilities", headers=h, json=body)
    assert r2.status_code == 201
    assert r2.json()["entity_id"] == first_id, "same key + body must replay"

    r3 = await client.post(
        "/v1/capabilities",
        headers=h,
        json={"name": "erg-different-cap", "entity_type": "capability"},
    )
    assert r3.status_code == 409
    assert r3.json()["errors"][0]["code"] == "idempotency_key_conflict"


@pytest.mark.asyncio
async def test_artifact_list_default_excludes_body(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    """Default list response includes title/category/created_at, NOT body."""
    client, token = seeded_client
    # Resolve Salt's entity_id via the slug GET (artifact router needs UUID).
    salt_body = (
        await client.get(
            "/v1/capabilities/salt-design-system",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    eid = salt_body["entity_id"]
    body = (
        await client.get(
            f"/v1/capabilities/{eid}/artifacts",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    assert body["items"], "expected artifacts"
    first = body["items"][0]
    assert "title" in first
    assert "body" not in first  # default excludes body
    assert first.get("created_by_display_name") == "dev-admin"


@pytest.mark.asyncio
async def test_artifact_list_with_category_filter(
    seeded_client: tuple[AsyncClient, str],
) -> None:
    client, token = seeded_client
    salt_body = (
        await client.get(
            "/v1/capabilities/salt-design-system",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    eid = salt_body["entity_id"]
    body = (
        await client.get(
            f"/v1/capabilities/{eid}/artifacts?category=overview",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["category"] == "overview"
