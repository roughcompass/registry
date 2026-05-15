"""Integration tests for the code-consolidation phase exit gate.

Covers the six cross-cutting features rolled out across all routers
during this phase:

1. Slug acceptance — routes updated to accept slug-or-UUID in the path
   segment work with both forms.
2. ``?view=audit`` — bitemporal-data routes populate audit fields when
   the parameter is present and omit them by default.
3. ``_links.self`` — detail responses carry the canonical URL.
4. ``X-Idempotency-Key`` — POST endpoints replay on same key+body (201)
   and reject same key + different body (409 ``idempotency_key_conflict``).
5. ``If-Match`` precondition — PATCH endpoints return 412 on a stale ETag
   and 200 on a current one.
6. Whoami ``_links`` — ``_links.tenant`` and ``_links.actor`` both resolve
   to real endpoints (200 each).

Run against the shared testcontainer Postgres using the bootstrap +
seed scripts to minimise fixture duplication.  Each numbered section
corresponds to one of the CON-T03..T07 + T11 task contracts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    patch_validator_for_actor,
)

_REPO_ROOT = Path(__file__).parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(database_url: str, script: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *extra],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
        cwd=str(_REPO_ROOT),
        check=False,
    )


async def _lookup_actor_id(pg_url: str, slug: str, oidc_subject: str) -> uuid.UUID:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT a.actor_id FROM actors a "
                        "JOIN tenants t ON t.tenant_id = a.tenant_id "
                        "WHERE t.slug = :slug AND a.oidc_subject = :sub"
                    ),
                    {"slug": slug, "sub": oidc_subject},
                )
            ).first()
        assert row is not None
        return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Shared fixture — one tenant bootstrapped + seeded per test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def con_client(
    pg_container: str,
) -> AsyncGenerator[tuple[AsyncClient, str, str], None]:
    """Bootstrap + seed a dedicated tenant; yield (client, token, slug).

    The yielded ``token`` is a placeholder string — the OIDC validator
    is patched for the duration of the fixture so the bytes don't matter.
    Existing tests that send ``Authorization: Bearer {token}`` keep
    working without per-call mocking.
    """
    slug = "dx-consolidation"
    bootstrap = _run(
        pg_container,
        _BOOTSTRAP_SCRIPT,
        "--tenant-slug",
        slug,
        "--actor-display-name",
        "dev-admin",
        "--skip-mock-seed",
    )
    assert bootstrap.returncode == 0, bootstrap.stderr

    seed = _run(pg_container, _SEED_SCRIPT, "--tenant-slug", slug)
    assert seed.returncode == 0, seed.stderr

    actor_id = await _lookup_actor_id(pg_container, slug, "dev-admin")

    class _FixedSubjectPersona(TenantPersona):
        @property
        def oidc_subject(self) -> str:  # type: ignore[override]
            return "dev-admin"

    persona = _FixedSubjectPersona(
        slug=slug, actor_id=actor_id, roles=["admin", "producer", "consumer"]
    )

    async with EntitlementAuthHarness(pg_container) as harness:
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                # Tenant headers default to the fixture's tenant slug so
                # tests that send only ``Authorization: Bearer …`` keep
                # working — the ASGI client appends X-Tenant-ID for them.
                client.headers.setdefault("X-Tenant-ID", persona.slug)
                yield client, "dummy.jwt", slug


# ---------------------------------------------------------------------------
# 1. Slug acceptance (CON-T03)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_capability_by_slug(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/capabilities/<slug> returns 200 — slug routed correctly."""
    client, token, _ = con_client
    r = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "salt-design-system"


@pytest.mark.asyncio
async def test_get_capability_by_uuid(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/capabilities/<uuid> returns 200 — UUID path still works."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert slug_resp.status_code == 200
    entity_id = slug_resp.json()["entity_id"]

    uuid_resp = await client.get(
        f"/v1/capabilities/{entity_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert uuid_resp.status_code == 200
    assert uuid_resp.json()["entity_id"] == entity_id


@pytest.mark.asyncio
async def test_put_interface_accepts_slug(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """PUT /v1/capabilities/<slug>/interface accepts a slug in the path segment."""
    client, token, _ = con_client
    r = await client.put(
        "/v1/capabilities/salt-design-system/interface",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "interface_source": "type Palette = { primary: string; }",
            "interface_format": "typescript",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    field_names = {f["name"] for f in body["fields"]}
    assert "primary" in field_names


@pytest.mark.asyncio
async def test_preview_version_accepts_slug(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """POST /v1/capabilities/<slug>/preview-version accepts a slug."""
    client, token, _ = con_client
    r = await client.post(
        "/v1/capabilities/salt-design-system/preview-version",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "proposed_version": "2.0.0",
            "proposed_interface": "type NewPalette = { secondary: string; }",
            "interface_format": "typescript",
        },
    )
    # 200 with diff data OR 422 if no existing interface — either way the
    # slug was resolved (not a 404 "not found" from failing to resolve the path).
    assert r.status_code in (200, 422), r.text
    assert r.status_code != 404


@pytest.mark.asyncio
async def test_concept_get_by_slug(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/concepts/<slug> — concept GET accepts slug form."""
    client, token, _ = con_client
    # Create a concept so we have something to fetch.
    create = await client.post(
        "/v1/concepts",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "con-test-concept", "entity_type": "concept"},
    )
    assert create.status_code == 201, create.text
    concept_id = create.json()["entity_id"]

    # Fetch by slug.
    r = await client.get(
        "/v1/concepts/con-test-concept",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["entity_id"] == concept_id


# ---------------------------------------------------------------------------
# 2. ?view=audit (CON-T04)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscriptions_default_view_omits_audit_fields(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/capabilities/<id>/subscriptions default view omits bitemporal cols."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    # Create a subscription so the list is non-empty.
    idem_key = f"con-sub-default-{uuid.uuid4().hex[:8]}"
    await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idem_key},
        json={"event_kinds": ["version_published"]},
    )

    r = await client.get(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    if items:
        item = items[0]
        for forbidden in (
            "valid_from",
            "valid_to",
            "ingested_at",
            "invalidated_at",
            "t_valid_from",
            "t_valid_to",
            "t_ingested_at",
            "t_invalidated_at",
        ):
            assert forbidden not in item, f"default view leaked {forbidden}"


@pytest.mark.asyncio
async def test_subscriptions_audit_view_populates_audit_fields(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/capabilities/<id>/subscriptions?view=audit populates valid_from etc."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    # Create a subscription so we have at least one row.
    idem_key = f"con-sub-audit-{uuid.uuid4().hex[:8]}"
    await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idem_key},
        json={"event_kinds": ["version_published"]},
    )

    r = await client.get(
        f"/v1/capabilities/{entity_id}/subscriptions?view=audit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    if items:
        item = items[0]
        # Audit view must expose the clean names (no t_ prefix).
        assert "valid_from" in item, f"audit view missing valid_from; got {list(item)}"
        assert "ingested_at" in item, f"audit view missing ingested_at; got {list(item)}"
        # Storage-layer names with t_ prefix must not leak through.
        for forbidden in ("t_valid_from", "t_ingested_at", "t_valid_to", "t_invalidated_at"):
            assert forbidden not in item, f"audit view leaked storage name {forbidden}"


@pytest.mark.asyncio
async def test_adoptions_audit_view(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/capabilities/<id>/adoptions?view=audit returns clean audit field names."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    r = await client.get(
        f"/v1/capabilities/{entity_id}/adoptions?view=audit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    # If there are rows, verify the audit-field shapes.
    for item in items:
        assert "valid_from" in item
        for forbidden in ("t_valid_from", "t_ingested_at"):
            assert forbidden not in item


# ---------------------------------------------------------------------------
# 3. _links.self on detail responses (CON-T05)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concept_detail_has_links_self(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/concepts/<id> response carries _links.self."""
    client, token, _ = con_client
    create = await client.post(
        "/v1/concepts",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "con-links-concept", "entity_type": "concept"},
    )
    assert create.status_code == 201
    cid = create.json()["entity_id"]

    r = await client.get(
        f"/v1/concepts/{cid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "_links" in body, f"response missing _links; keys: {list(body)}"
    assert body["_links"]["self"] == f"/v1/concepts/{cid}"


@pytest.mark.asyncio
async def test_interface_detail_has_links_self(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/capabilities/<id>/interface response carries _links.self."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    # Ensure an interface exists.
    await client.put(
        f"/v1/capabilities/{entity_id}/interface",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "interface_source": "type Token = { name: string; value: string; }",
            "interface_format": "typescript",
        },
    )

    r = await client.get(
        f"/v1/capabilities/{entity_id}/interface",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "_links" in body, f"response missing _links; keys: {list(body)}"
    assert f"/v1/capabilities/{entity_id}/interface" in body["_links"]["self"]


@pytest.mark.asyncio
async def test_capability_detail_has_links_self(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """GET /v1/capabilities/<slug> response carries _links.self."""
    client, token, _ = con_client
    r = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "_links" in body, f"response missing _links; keys: {list(body)}"
    assert "self" in body["_links"]
    assert "salt-design-system" in body["_links"]["self"]


# ---------------------------------------------------------------------------
# 4. X-Idempotency-Key on POST endpoints (CON-T06)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_post_idempotency_replay(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """Same X-Idempotency-Key + same body replays the original 201 subscription."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    idem_key = f"con-idem-sub-{uuid.uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Idempotency-Key": idem_key,
    }
    body = {"event_kinds": ["version_published"]}

    r1 = await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers=headers,
        json=body,
    )
    assert r1.status_code == 201, r1.text
    first_id = r1.json()["subscription_id"]

    r2 = await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers=headers,
        json=body,
    )
    assert r2.status_code == 201, "idempotency replay must return 201"
    assert r2.json()["subscription_id"] == first_id, "replayed response must be identical"


@pytest.mark.asyncio
async def test_subscription_post_idempotency_conflict(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """Same key + different body returns 409 idempotency_key_conflict."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    idem_key = f"con-idem-conflict-{uuid.uuid4().hex}"
    headers = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": idem_key}

    # First call — establishes the key.
    r1 = await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers=headers,
        json={"event_kinds": ["version_published"]},
    )
    assert r1.status_code == 201, r1.text

    # Second call — same key, different body.
    r2 = await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers=headers,
        json={"event_kinds": ["deprecation"]},
    )
    assert r2.status_code == 409, r2.text
    errors = r2.json().get("errors", [])
    codes = [e.get("code") for e in errors]
    assert "idempotency_key_conflict" in codes, f"expected idempotency_key_conflict in {codes}"


@pytest.mark.asyncio
async def test_concept_post_idempotency_replay(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """POST /v1/concepts with X-Idempotency-Key replays on second call."""
    client, token, _ = con_client
    idem_key = f"con-idem-concept-{uuid.uuid4().hex}"
    headers = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": idem_key}
    body = {"name": f"con-idem-cpt-{uuid.uuid4().hex[:6]}", "entity_type": "concept"}

    r1 = await client.post("/v1/concepts", headers=headers, json=body)
    assert r1.status_code == 201, r1.text
    first_id = r1.json()["entity_id"]

    r2 = await client.post("/v1/concepts", headers=headers, json=body)
    assert r2.status_code == 201
    assert r2.json()["entity_id"] == first_id, "idempotent replay must return the same entity"


# ---------------------------------------------------------------------------
# 5. If-Match precondition on PATCH (CON-T07)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_subscription_stale_if_match_returns_412(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """PATCH /v1/subscriptions/<id> with stale ETag returns 412."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    # Create a subscription.
    idem_key = f"con-etag-sub-{uuid.uuid4().hex}"
    create_r = await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idem_key},
        json={"event_kinds": ["version_published"]},
    )
    assert create_r.status_code == 201, create_r.text
    sub_id = create_r.json()["subscription_id"]

    # PATCH with a deliberately stale ETag.
    patch_r = await client.patch(
        f"/v1/subscriptions/{sub_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "If-Match": 'W/"definitely-stale-etag"',
        },
        json={"is_enabled": False},
    )
    assert patch_r.status_code == 412, patch_r.text
    errors = patch_r.json().get("errors", [])
    codes = [e.get("code") for e in errors]
    assert "precondition_failed" in codes, f"expected precondition_failed in {codes}"


@pytest.mark.asyncio
async def test_patch_subscription_current_if_match_succeeds(
    con_client: tuple[AsyncClient, str, str],
) -> None:
    """PATCH /v1/subscriptions/<id> with the current ETag returns 200."""
    client, token, _ = con_client
    slug_resp = await client.get(
        "/v1/capabilities/salt-design-system",
        headers={"Authorization": f"Bearer {token}"},
    )
    entity_id = slug_resp.json()["entity_id"]

    # Create a subscription.
    idem_key = f"con-etag-current-{uuid.uuid4().hex}"
    create_r = await client.post(
        f"/v1/capabilities/{entity_id}/subscriptions",
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idem_key},
        json={"event_kinds": ["version_published"]},
    )
    assert create_r.status_code == 201
    sub_id = create_r.json()["subscription_id"]

    # PATCH once with no If-Match to get a valid ETag from the response.
    first_patch = await client.patch(
        f"/v1/subscriptions/{sub_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"is_enabled": True},
    )
    assert first_patch.status_code == 200, first_patch.text

    # The response body carries _links (subscription PATCH sets include_links=True).
    # Extract the current ETag from a follow-up list call.
    list_r = await client.get(
        f"/v1/capabilities/{entity_id}/subscriptions?view=audit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_r.status_code == 200
    subs = list_r.json()["items"]
    matching = [s for s in subs if str(s.get("subscription_id")) == str(sub_id)]
    assert matching, f"subscription {sub_id} not in list"
    ingested_at = matching[0].get("ingested_at")

    # Recompute the current ETag the same way the middleware does.
    import datetime

    from registry.api.middleware.etag import compute_etag, latest_timestamp

    ts = datetime.datetime.fromisoformat(ingested_at) if ingested_at else datetime.datetime.now(tz=datetime.UTC)
    current_etag = compute_etag(uuid.UUID(str(sub_id)), latest_timestamp(ts))

    second_patch = await client.patch(
        f"/v1/subscriptions/{sub_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "If-Match": current_etag,
        },
        json={"is_enabled": False},
    )
    assert second_patch.status_code == 200, second_patch.text


# ---------------------------------------------------------------------------
# 6. Whoami _links — endpoint-only check
# ---------------------------------------------------------------------------
#
# The original suite asserted that ``_links.tenant`` and ``_links.actor``
# from /v1/whoami both resolved to live endpoints — those endpoints were
# at ``/v1/admin/tenants/{slug}`` and ``/v1/admin/actors/{actor_id}``,
# both of which were removed when the registry stopped owning the
# tenant/actor admin surface. The whoami endpoint still emits its
# ``_links.self`` pointer; the deleted tests below covered:
#
#   - test_whoami_links_resolve            → admin/tenants + admin/actors gone
#   - test_tenant_endpoint_rejects_cross_tenant → admin/tenants gone
#   - test_actor_endpoint_returns_self     → admin/actors gone
#
# Whoami self-link is still asserted in test_api_ergonomics.py.
# ---------------------------------------------------------------------------
