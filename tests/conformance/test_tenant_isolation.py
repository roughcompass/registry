"""Cross-tenant isolation conformance suite.

Adversarial gate: every change touching cross-tenant visibility must keep
these assertions green. Drives the live FastAPI app via ASGITransport
against a testcontainers Postgres, with the entitlement-resolved auth
path mocked through ``tests/helpers/auth_harness.py``:

  - ``validate_oidc_token`` is patched to return a chosen actor's
    identity (no JWT decode, no JWKS fetch).
  - ``app.state.claim_resolver``'s fetcher is replaced with an
    ``AsyncMock`` returning entitlement strings the resolver parses,
    materializing the tenant + actor JIT.

What this suite gates
---------------------

1. ``test_capability_path_param_swap`` — tenant B's actor cannot
   GET / PATCH / DELETE a capability that belongs to tenant A by
   substituting tenant A's entity_id; the endpoint MUST return 403/404.
2. ``test_x_tenant_id_cannot_promote_to_unowned_tenant`` — an actor
   whose grants live only in tenant A cannot pick tenant B by sending
   ``X-Tenant-ID: tenant-b``; the middleware MUST reject the choice.
3. ``test_tenant_context_reflects_actor_grants`` — whoami responses
   reflect the persona's actual tenant, regardless of what the JWT
   subject would suggest in isolation.
4. ``test_search_returns_no_cross_tenant_hits`` — the consumer search
   endpoint enforces isolation at the DB layer (200 + zero hits, not
   403), so an enumeration attempt yields no leakage.
5. ``test_admin_audit_returns_no_cross_tenant_rows`` — admin audit
   listing, scoped by tenant_id from the resolved context, returns 0
   rows when an outsider tenant queries — never the rows of another
   tenant.
6. ``test_no_bearer_returns_401`` — calls without Authorization MUST
   never reach the resolver; the middleware short-circuits to 401.

The suite is intentionally light on endpoint count — we cover a small
number of representative routes per category instead of every URL,
because the isolation guarantee lives in shared middleware + service
helpers (``service/visibility.py``, ``resolve_entity_handle``), not in
each router. Adding a router does not require a new isolation test;
fixing isolation in those shared helpers does.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

# ---------------------------------------------------------------------------
# Two-tenant fixture
# ---------------------------------------------------------------------------


class _TwoTenantHarness:
    """Bundles the harness, an HTTP client, and seeded capability ids
    for tenant A so a single test can flip personas easily."""

    def __init__(
        self,
        harness: EntitlementAuthHarness,
        client: AsyncClient,
        persona_a: TenantPersona,
        persona_b: TenantPersona,
        cap_a_id: uuid.UUID,
    ) -> None:
        self.harness = harness
        self.client = client
        self.a = persona_a
        self.b = persona_b
        self.cap_a_id = cap_a_id


async def _create_capability(
    client: AsyncClient, harness: EntitlementAuthHarness, persona: TenantPersona, name: str
) -> uuid.UUID:
    """Create a capability as ``persona`` and return its id."""
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/capabilities",
            json={"name": name, "capability_type": "service"},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 201, f"capability create failed: {resp.status_code} {resp.text}"
    return uuid.UUID(resp.json()["entity_id"])


@pytest_asyncio.fixture
async def two_tenant(pg_container: str) -> AsyncIterator[_TwoTenantHarness]:
    """Build a harness with two personas A and B, seed one capability in A."""
    async with EntitlementAuthHarness(pg_container) as harness:
        # Random slugs keep the testcontainer reusable across runs without
        # bumping into JIT-materialised rows from prior tests.
        a_slug = f"alpha-{uuid.uuid4().hex[:6]}"
        b_slug = f"beta-{uuid.uuid4().hex[:6]}"
        persona_a = harness.add_persona(a_slug, roles=["admin", "producer", "consumer"])
        persona_b = harness.add_persona(b_slug, roles=["admin", "producer", "consumer"])

        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cap_a_id = await _create_capability(client, harness, persona_a, "isolation-target")
            yield _TwoTenantHarness(harness, client, persona_a, persona_b, cap_a_id)


# ---------------------------------------------------------------------------
# (1) Capability path-param swap — direct read/write must reject cross-tenant
# ---------------------------------------------------------------------------


# Each entry: (HTTP method, body or None). Path is built from cap_a_id.
# Producer + admin grants on persona_b satisfy the role gate for all three —
# the only thing that should keep the call out is tenant isolation.
PATH_PARAM_SWAP_CASES: list[tuple[str, dict[str, Any] | None]] = [
    ("GET", None),
    ("PATCH", {"updates": {"name": "should-be-rejected"}}),
    ("DELETE", None),
]


@pytest.mark.parametrize("method,body", PATH_PARAM_SWAP_CASES)
@pytest.mark.asyncio
async def test_capability_path_param_swap(
    two_tenant: _TwoTenantHarness,
    method: str,
    body: dict[str, Any] | None,
) -> None:
    """Persona B cannot read or mutate persona A's capability by id."""
    two_tenant.harness.configure_fetcher_for(two_tenant.b)
    with patch_validator_for_actor(two_tenant.b):
        resp = await two_tenant.client.request(
            method,
            f"/v1/capabilities/{two_tenant.cap_a_id}",
            json=body,
            headers=bearer_headers(tenant_slug=two_tenant.b.slug),
        )
    # 403 (forbidden) or 404 (not found) — both surface "you can't see it"
    # without leaking whether the id exists. 200 would be a leak.
    assert resp.status_code in (403, 404), (
        f"{method} /v1/capabilities/{{id}} returned {resp.status_code} cross-tenant"
    )


# ---------------------------------------------------------------------------
# (2) X-Tenant-ID cannot grant access to a tenant the actor has no grants in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_tenant_id_cannot_promote_to_unowned_tenant(
    two_tenant: _TwoTenantHarness,
) -> None:
    """Actor B sends X-Tenant-ID: <tenant-A>; middleware MUST reject it.

    The selection rule is: X-Tenant-ID picks among the actor's granted
    tenants; it cannot create a grant. Asking for a tenant outside the
    grant set yields 403 (or, equivalently, the actor's only tenant is
    chosen and the cross-tenant resource is not visible — 403/404).
    """
    two_tenant.harness.configure_fetcher_for(two_tenant.b)
    with patch_validator_for_actor(two_tenant.b):
        resp = await two_tenant.client.get(
            f"/v1/capabilities/{two_tenant.cap_a_id}",
            headers=bearer_headers(tenant_slug=two_tenant.a.slug),
        )
    # Must NOT be 200 — that would prove the X-Tenant-ID header bypassed
    # the grant check and leaked tenant A's resource to actor B.
    assert resp.status_code in (400, 403, 404), (
        f"actor B + X-Tenant-ID=A returned {resp.status_code}; "
        "this would mean X-Tenant-ID can promote past the grant set"
    )


# ---------------------------------------------------------------------------
# (3) Whoami reflects the actor's tenant, never another
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_context_reflects_actor_grants(
    two_tenant: _TwoTenantHarness,
) -> None:
    """Each persona's /v1/whoami response must contain their own slug."""
    two_tenant.harness.configure_fetcher_for(two_tenant.a)
    with patch_validator_for_actor(two_tenant.a):
        resp_a = await two_tenant.client.get(
            "/v1/whoami", headers=bearer_headers(tenant_slug=two_tenant.a.slug)
        )
    assert resp_a.status_code == 200
    assert resp_a.json().get("tenant_slug") == two_tenant.a.slug

    two_tenant.harness.configure_fetcher_for(two_tenant.b)
    with patch_validator_for_actor(two_tenant.b):
        resp_b = await two_tenant.client.get(
            "/v1/whoami", headers=bearer_headers(tenant_slug=two_tenant.b.slug)
        )
    assert resp_b.status_code == 200
    assert resp_b.json().get("tenant_slug") == two_tenant.b.slug


# ---------------------------------------------------------------------------
# (4) Search isolation — 200 + zero hits, never another tenant's rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_no_cross_tenant_hits(
    two_tenant: _TwoTenantHarness,
) -> None:
    """Persona B searches for the exact name of persona A's capability;
    response is 200 OK with zero hits, never persona A's row.

    Search isolation lives at the DB query layer (tenant_id filter in
    the SQL that backs ``RetrievalService``). The endpoint deliberately
    does not 403/404 cross-tenant — that would leak existence — so the
    invariant is "the row never appears in B's response body".
    """
    two_tenant.harness.configure_fetcher_for(two_tenant.b)
    with patch_validator_for_actor(two_tenant.b):
        resp = await two_tenant.client.get(
            "/v1/search?q=isolation-target",
            headers=bearer_headers(tenant_slug=two_tenant.b.slug),
        )
    assert resp.status_code == 200
    body = resp.json()
    # The capability id and the tenant id must never appear in B's body.
    assert str(two_tenant.cap_a_id) not in resp.text, "cross-tenant capability id leaked into search"
    # results / hits / items — accept either schema shape.
    items = body.get("results") or body.get("hits") or body.get("items") or []
    assert all(item.get("entity_id") != str(two_tenant.cap_a_id) for item in items)


# ---------------------------------------------------------------------------
# (5) Admin audit isolation — 200 + zero rows for an outsider tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_audit_returns_no_cross_tenant_rows(
    two_tenant: _TwoTenantHarness, pg_container: str
) -> None:
    """Persona B queries /v1/admin/audit; cannot see persona A's audit
    rows from creating the seeded capability.

    The audit table is scoped by tenant_id on every read. Asserting B
    sees zero rows belonging to A's tenant_id (cross-checked via DB)
    keeps that invariant honest.
    """
    # Look up tenant A's tenant_id in the DB so we can scan the response
    # body for it. A has been JIT-materialised by the seed call.
    engine = create_async_engine(
        pg_container, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": two_tenant.a.slug},
                )
            ).first()
        assert row is not None
        a_tenant_id = str(row[0])
    finally:
        await engine.dispose()

    # /v1/admin/audit requires the auditor role specifically. Build a
    # fresh persona inside tenant B with that role and act as them.
    auditor_b = two_tenant.harness.add_persona(
        two_tenant.b.slug, roles=["auditor"], actor_id=uuid.uuid4()
    )
    two_tenant.harness.configure_fetcher_for(auditor_b)
    with patch_validator_for_actor(auditor_b):
        resp = await two_tenant.client.get(
            "/v1/admin/audit",
            headers=bearer_headers(tenant_slug=auditor_b.slug),
        )
    assert resp.status_code == 200, f"admin audit returned {resp.status_code}: {resp.text}"
    # Tenant A's id must never appear in tenant B's audit response body.
    assert a_tenant_id not in resp.text, "cross-tenant tenant_id leaked into admin audit response"


# ---------------------------------------------------------------------------
# (6) No bearer token short-circuits to 401 before reaching the resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_bearer_returns_401(two_tenant: _TwoTenantHarness) -> None:
    """A request with no Authorization header MUST be rejected at the
    middleware layer with 401 — the resolver must never be consulted."""
    resp = await two_tenant.client.get("/v1/whoami")
    assert resp.status_code == 401
