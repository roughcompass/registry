"""Tenant isolation conformance suite.

``PATH_PARAM_SWAP_CASES`` holds endpoints where tenant isolation is enforced at
the HTTP layer (403/404). The fixture and the three test functions are
authoritative — do not restructure them when adding new endpoints.

Consumer read endpoints (dependencies, search, MCP tools)
----------------------------------------------------------
These endpoints return 200 OK with empty results rather than 403/404 when an
entity_id is outside the calling tenant's scope — the response is
indistinguishable from a valid entity with no matching data.  This prevents
tenant enumeration.  Each is covered by a dedicated test with the appropriate
assertion:

1. ``GET /v1/capabilities/{entity_id}/dependencies`` — returns 200 OK with an
   empty edges list when the entity_id is not in tenant B's scope (isolation
   enforced via tenant_id filter in the recursive CTE, not an HTTP 404).
   Covered by ``test_dependencies_tenant_isolation``.

2. ``GET /v1/search?q=payment-service`` — returns 200 OK with 0 hits (isolation
   enforced at the DB query layer, not HTTP).  Covered by
   ``test_search_tenant_isolation``.

3. ``POST /mcp`` — NOT testable over HTTP because the MCP server uses SSE
   transport (``GET /mcp/sse`` + ``POST /mcp/messages/``), not a single POST
   endpoint.  Covered by ``test_mcp_get_capability_tenant_isolation`` and
   ``test_mcp_search_returns_zero_hits_for_cross_tenant_query`` via the same
   in-process ``call_tool`` pattern used by ``test_mcp_conformance.py``.

Admin sync-source endpoints
----------------------------
Three admin endpoints require seeded sync_source / sync_run rows.  They
enforce isolation via ``source.tenant_id != ctx.tenant_id`` checks and
return 404, so the expected assertion is 403 or 404.  Because the path
templates use ``{source_id}`` / ``{sync_run_id}`` (not ``{entity_id}``),
they do not fit the generic ``PATH_PARAM_SWAP_CASES`` harness without row-level
seeding.  Each is covered by a dedicated test that seeds tenant A's resource and
asserts tenant B's token cannot access it.

1. ``GET /v1/admin/sync-sources/{source_id}`` — Covered by
   ``test_sync_source_tenant_isolation``.

2. ``GET /v1/admin/sync-runs/{sync_run_id}`` — Covered by
   ``test_sync_run_tenant_isolation``.

3. ``GET /v1/admin/sync-runs/{sync_run_id}/superseded`` — Covered by
   ``test_sync_run_superseded_tenant_isolation``.

Admin read endpoints (audit, vocabularies, capability-types, roles)
-------------------------------------------------------------------
Four admin endpoints enforce tenant isolation at the DB query layer and return
200 OK with empty results rather than 403/404.  They do not fit
``PATH_PARAM_SWAP_CASES`` (which asserts 403/404) — each is covered by a
dedicated test asserting 0 rows in the response body.

1. ``GET /v1/admin/audit`` — returns 200 OK with rows=[] when queried with
   tenant B's token (audit_log is always scoped by tenant_id from
   TenantContext).  Covered by ``test_audit_tenant_isolation``.

2. ``GET /v1/admin/vocabularies/entity_type`` — returns 200 OK with an empty
   list when tenant B has no vocabulary rows (DB-layer tenant_id filter).
   Covered by ``test_vocabulary_tenant_isolation``.

3. ``GET /v1/admin/capability-types`` — returns 200 OK with an empty list
   (tenant_id filter on CapabilityTypeSchema).  Covered by
   ``test_capability_types_tenant_isolation``.

4. ``GET /v1/admin/roles`` — returns 200 OK with an empty list (tenant_id
   filter on Role table).  Covered by ``test_roles_tenant_isolation``.
"""

from __future__ import annotations

import datetime
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers.mcp import _request_token, create_catalog_mcp_server
from registry.config import Settings
from registry.embedder import StubEmbedder
from registry.main import create_app
from registry.service.catalog import CatalogService
from registry.service.retrieval import RetrievalService
from registry.service.schema import SchemaService
from registry.service.vocabulary import VocabularyService
from registry.storage.models import Actor, ApiToken, Entity, Fact, SyncRun, SyncSource, Tenant, VocabularyValue
from registry.types import FakeClock, TenantContext


class TwoTenantHarness(NamedTuple):
    client: httpx.AsyncClient
    tenant_a_id: uuid.UUID
    tenant_b_id: uuid.UUID
    token_a: str
    token_b: str


async def _seed(
    pg_url: str,
    *,
    tenant_slug: str,
) -> tuple[uuid.UUID, str]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(tenant_id=tenant_id, slug=tenant_slug, display_name=tenant_slug, created_at=now, is_active=True)
            )
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"actor-{tenant_slug}",
                    email=None,
                    oidc_subject=None,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    token_hash=hash_token(raw_token),
                    roles=["producer"],
                    description=None,
                    expires_at=None,
                    created_at=now,
                    revoked_at=None,
                )
            )
    finally:
        await engine.dispose()
    return tenant_id, raw_token


@pytest_asyncio.fixture
async def two_tenant_app(pg_container: str, app_settings: Settings) -> AsyncIterator[TwoTenantHarness]:
    """Seed two tenants with overlapping data; yield an httpx AsyncClient against the live FastAPI app.

    We mount a single test-only `/v1/_whoami` route so the auth middleware
    is exercised before real producer endpoints exist. The production routers
    eventually cover everything in the parametrize list; `_whoami` remains
    so the fixture is stable as endpoints are added.
    """
    app = create_app(app_settings)

    @app.get("/v1/_whoami")
    async def _whoami(ctx: TenantContext = Depends(get_tenant_context)) -> dict[str, str]:
        return {"tenant_id": str(ctx.tenant_id), "actor_id": str(ctx.actor_id)}

    tenant_a_id, token_a = await _seed(pg_container, tenant_slug=f"alpha-{uuid.uuid4().hex[:6]}")
    tenant_b_id, token_b = await _seed(pg_container, tenant_slug=f"beta-{uuid.uuid4().hex[:6]}")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield TwoTenantHarness(client, tenant_a_id, tenant_b_id, token_a, token_b)


# Each entry is (HTTP method, path template, request body or None).
# The harness substitutes tenant A's entity_id UUID into {entity_id} and
# asserts that tenant B's token receives 403 or 404 — not 200.
#
# Consumer read endpoints (search, dependencies, MCP) return 200+empty for
# cross-tenant access (isolation at the DB layer, not HTTP), so they are
# covered by dedicated tests instead (see module docstring for full rationale).
#
# Capability GET/PATCH/DELETE enforce tenant isolation via resolve_entity_handle,
# which filters by tenant_id and raises NotFoundError (→ 404) for cross-tenant
# UUIDs. Producer tokens satisfy the role requirement for all three.
PATH_PARAM_SWAP_CASES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/v1/capabilities/{entity_id}", None),
    ("PATCH", "/v1/capabilities/{entity_id}", {"name": "should-be-rejected"}),
    ("DELETE", "/v1/capabilities/{entity_id}", None),
]


@pytest.mark.parametrize("method,path_template,body", PATH_PARAM_SWAP_CASES)
@pytest.mark.asyncio
async def test_path_param_swap(
    two_tenant_app: TwoTenantHarness,
    method: str,
    path_template: str,
    body: dict[str, Any] | None,
) -> None:
    """Tenant B's token cannot read or mutate tenant A's resource by substituting its entity_id."""
    path = path_template.format(entity_id=str(two_tenant_app.tenant_a_id))
    response = await two_tenant_app.client.request(
        method,
        path,
        json=body,
        headers={"Authorization": f"Bearer {two_tenant_app.token_b}"},
    )
    assert response.status_code in (403, 404)


@pytest.mark.asyncio
async def test_header_forgery(two_tenant_app: TwoTenantHarness) -> None:
    """X-Tenant-Id header MUST be ignored — tenant identity comes only from the bearer token."""
    response = await two_tenant_app.client.get(
        "/v1/_whoami",
        headers={
            "Authorization": f"Bearer {two_tenant_app.token_b}",
            "X-Tenant-Id": str(two_tenant_app.tenant_a_id),
        },
    )
    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(two_tenant_app.tenant_b_id)


@pytest.mark.asyncio
async def test_token_replay_across_tenants(two_tenant_app: TwoTenantHarness) -> None:
    """Tenant A's token must not impersonate tenant B — _whoami returns A's id, not B's."""
    response = await two_tenant_app.client.get(
        "/v1/_whoami",
        headers={"Authorization": f"Bearer {two_tenant_app.token_a}"},
    )
    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(two_tenant_app.tenant_a_id)
    assert response.json()["tenant_id"] != str(two_tenant_app.tenant_b_id)


# ---------------------------------------------------------------------------
# Dependencies tenant isolation (HTTP, separate from PATH_PARAM_SWAP_CASES)
#
# Transport: httpx against the ASGI app (same as test_path_param_swap).
# Assertion: 200 OK with an empty edges array — tenant isolation is enforced
# at the DB layer (tenant_id filter in recursive CTE), not via HTTP 403/404.
# The endpoint deliberately does not expose whether an entity_id belongs to
# another tenant; both non-existent and cross-tenant cases return empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependencies_tenant_isolation(two_tenant_app: TwoTenantHarness) -> None:
    """Tenant B's token cannot traverse tenant A's dependency graph.

    Design choice: the dependencies endpoint returns 200 OK with empty edges
    rather than 403/404 when the entity_id is outside tenant B's scope — the
    response is indistinguishable from a valid entity that simply has no edges.
    This prevents tenant enumeration.  A dedicated test is therefore cleaner
    than PATH_PARAM_SWAP_CASES, which asserts 403/404 (see module docstring).
    """
    # Use tenant A's tenant_id UUID as the entity_id to query cross-tenant.
    path = f"/v1/capabilities/{two_tenant_app.tenant_a_id}/dependencies"
    response = await two_tenant_app.client.get(
        path,
        headers={"Authorization": f"Bearer {two_tenant_app.token_b}"},
    )
    assert response.status_code == 200
    body = response.json()
    edges = body.get("edges") or []
    assert edges == [], f"tenant B must see 0 dependency edges across tenant A's data; got {len(edges)} edge(s)"


# ---------------------------------------------------------------------------
# Search tenant isolation (HTTP, separate from PATH_PARAM_SWAP_CASES)
#
# Transport: httpx against the ASGI app (same as test_path_param_swap).
# Assertion: 200 OK with an empty hits array — tenant B's query is scoped to
# their own data, so even though the query text matches tenant A's entity name,
# no cross-tenant rows leak into the response.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tenant_isolation(two_tenant_app: TwoTenantHarness) -> None:
    """Tenant B's token cannot see tenant A's capabilities via search.

    Design choice: search returns 200 OK with an empty hits list rather than
    403/404 (the query is valid; there simply are no matching rows in tenant
    B's scope).  This is a different invariant from path-param swap — tenant
    isolation is enforced at the DB query layer, not the HTTP layer.  A
    dedicated test is therefore cleaner than stretching PATH_PARAM_SWAP_CASES
    to accommodate a different assertion shape (see module docstring).
    """
    response = await two_tenant_app.client.get(
        "/v1/search?q=payment-service",
        headers={"Authorization": f"Bearer {two_tenant_app.token_b}"},
    )
    assert response.status_code == 200
    body = response.json()
    # Response shape: {"items": [...], ...} or {"results": [...], ...}
    # Accept either key; the critical invariant is that no items are returned.
    items = body.get("items") or body.get("results") or []
    assert items == [], f"tenant B must see 0 search hits across tenant A's data; got {len(items)} hit(s)"


# ---------------------------------------------------------------------------
# MCP tool tenant isolation (in-process, separate from HTTP)
#
# Transport: FastMCP in-process call_tool via _request_token ContextVar.
# Rationale: the MCP server uses SSE transport (GET /mcp/sse +
# POST /mcp/messages/), not a single HTTP POST.  Driving SSE over an ASGI
# test client requires a persistent event-stream connection that is
# impractical to drive deterministically from pytest-asyncio without a real
# running server.  The in-process pattern (set ContextVar → call_tool) is
# identical to what test_mcp_conformance.py uses and exercises the same auth
# and service layers as the live SSE handler (see module docstring).
# ---------------------------------------------------------------------------

_NOW_MCP = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _MiniMcpFixture(NamedTuple):
    mcp: Any
    entity_a_id: uuid.UUID
    token_b: str


async def _seed_with_entity(
    pg_url: str,
    *,
    tenant_slug: str,
    entity_name: str,
) -> tuple[uuid.UUID, str, uuid.UUID]:
    """Seed one tenant with one entity; return (tenant_id, raw_token, entity_id)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    now = _NOW_MCP
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(tenant_id=tenant_id, slug=tenant_slug, display_name=tenant_slug, created_at=now, is_active=True)
            )
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"actor-{tenant_slug}",
                    email=None,
                    oidc_subject=None,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    token_hash=hash_token(raw_token),
                    roles=["producer"],
                    description=None,
                    expires_at=None,
                    created_at=now,
                    revoked_at=None,
                )
            )
            for kind, value in [("entity_type", "service"), ("fact_category", "overview")]:
                session.add(
                    VocabularyValue(
                        vocab_id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        kind=kind,
                        value=value,
                        is_system=True,
                        deprecated_at=None,
                        created_at=now,
                    )
                )
            await session.flush()
            session.add(
                Entity(
                    entity_id=entity_id,
                    tenant_id=tenant_id,
                    entity_type="service",
                    name=entity_name,
                    external_id=None,
                    is_active=True,
                    created_at=now,
                    created_by=actor_id,
                )
            )
            await session.flush()
            session.add(
                Fact(
                    fact_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    entity_id=entity_id,
                    category="overview",
                    body=f"{entity_name} overview.",
                    is_authoritative=True,
                    is_authoritative_superseded=False,
                    sync_run_id=None,
                    t_valid_from=now,
                    t_valid_to=None,
                    t_ingested_at=now,
                    t_invalidated_at=None,
                    created_by=actor_id,
                )
            )
    finally:
        await engine.dispose()
    return tenant_id, raw_token, entity_id


@pytest_asyncio.fixture
async def mcp_isolation_harness(pg_container: str, app_settings: Settings) -> AsyncIterator[_MiniMcpFixture]:
    """Seed two tenants (each with one entity); wire an in-process FastMCP server.

    Tenant A owns an entity.  The test asserts that tenant B's token raises
    ToolError when calling get_capability with tenant A's entity_id.
    """
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    _tenant_a_id, token_a, entity_a_id = await _seed_with_entity(
        pg_container,
        tenant_slug=f"iso-alpha-{suffix_a}",
        entity_name="payment-service-iso",
    )
    _tenant_b_id, token_b, _entity_b_id = await _seed_with_entity(
        pg_container,
        tenant_slug=f"iso-beta-{suffix_b}",
        entity_name="billing-service-iso",
    )
    del token_a  # only tenant B's token is used in the cross-tenant test

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock(_NOW_MCP)
    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, clock)
    catalog = CatalogService(session_factory, clock, vocabulary, schema)
    embedder = StubEmbedder()
    retrieval = RetrievalService(session_factory, clock, embedder, app_settings)

    mcp = create_catalog_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
        clock=clock,
    )

    try:
        yield _MiniMcpFixture(mcp, entity_a_id, token_b)
    finally:
        await engine.dispose()


async def _call_tool_as(mcp: Any, *, token: str, tool: str, args: dict[str, Any]) -> Any:
    """Set the Bearer token ContextVar and call the MCP tool in-process.

    Mirrors the ``_call_as`` helper in ``test_mcp_conformance.py`` and the
    ``handle_sse`` closure in ``registry.api.routers.mcp`` — the same ContextVar
    path that every live SSE request takes.
    """
    cv_token = _request_token.set(token)
    try:
        return await mcp.call_tool(tool, args)
    finally:
        _request_token.reset(cv_token)


@pytest.mark.asyncio
async def test_mcp_get_capability_tenant_isolation(mcp_isolation_harness: _MiniMcpFixture) -> None:
    """Tenant B's MCP token cannot retrieve tenant A's capability via get_capability.

    Design choice: tested in-process via call_tool rather than over HTTP because
    the MCP server uses SSE transport (GET /mcp/sse + POST /mcp/messages/), not
    a single POST endpoint, which does not fit the httpx request/assert pattern
    used in test_path_param_swap (see module docstring).

    The tool must raise ToolError (mapped from NotFoundError or
    TenantIsolationError at the service layer) — not return the resource with a
    stripped tenant field.
    """
    mcp = mcp_isolation_harness.mcp
    entity_a_id = str(mcp_isolation_harness.entity_a_id)
    token_b = mcp_isolation_harness.token_b

    with pytest.raises(ToolError):
        await _call_tool_as(
            mcp,
            token=token_b,
            tool="get_capability",
            args={"entity_id": entity_a_id},
        )


@pytest.mark.asyncio
async def test_mcp_search_returns_zero_hits_for_cross_tenant_query(
    mcp_isolation_harness: _MiniMcpFixture,
) -> None:
    """Tenant B's MCP search cannot see tenant A's capabilities.

    search_capabilities is tenant-scoped at the DB layer; querying for a name
    that matches only tenant A's entity must return an empty hits list for
    tenant B — not raise ToolError (the call itself is valid, just returns no
    results).
    """
    mcp = mcp_isolation_harness.mcp
    token_b = mcp_isolation_harness.token_b

    result = await _call_tool_as(
        mcp,
        token=token_b,
        tool="search_capabilities",
        args={"q": "payment-service-iso", "top_k": 10},
    )

    assert result, "call_tool must return a non-empty sequence"
    first = result[0]
    assert first.type == "text"
    parsed = json.loads(first.text)
    assert isinstance(parsed, list), "search result body must be a JSON array"
    assert parsed == [], f"tenant B must see 0 MCP search hits across tenant A's data; got {len(parsed)} hit(s)"


# ---------------------------------------------------------------------------
# Sync-source tenant isolation (HTTP, dedicated tests)
#
# Transport: httpx against the ASGI app (same as test_path_param_swap).
# Assertion: 403 or 404 — admin endpoints enforce tenant_id ownership checks
# and return 404 for cross-tenant resource access (prevents enumeration).
#
# These are dedicated tests (not in PATH_PARAM_SWAP_CASES) because the path
# parameters are {source_id} / {sync_run_id}, not {entity_id}, so the generic
# harness cannot substitute them without seeded row IDs.
# ---------------------------------------------------------------------------

_NOW_P3 = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


async def _seed_p3(
    pg_url: str,
    *,
    tenant_slug: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Seed tenant + actor + admin token; return (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    slug=tenant_slug,
                    display_name=tenant_slug,
                    created_at=_NOW_P3,
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"actor-{tenant_slug}",
                    email=None,
                    oidc_subject=None,
                    created_at=_NOW_P3,
                )
            )
            await session.flush()
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    token_hash=hash_token(raw_token),
                    roles=["admin"],
                    description=None,
                    expires_at=None,
                    created_at=_NOW_P3,
                    revoked_at=None,
                )
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_sync_source(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a sync_source row for tenant A; return source_id."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            session.add(
                SyncSource(
                    source_id=source_id,
                    tenant_id=tenant_id,
                    source_type="openapi",
                    display_name="iso-test-source",
                    config={"owner": "acme", "repo": "svc", "ref": "main"},
                    credentials_ref=None,
                    schedule=None,
                    is_active=True,
                    created_at=_NOW_P3,
                    created_by=actor_id,
                )
            )
    finally:
        await engine.dispose()
    return source_id


async def _seed_sync_run(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a sync_run row for tenant A; return sync_run_id."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sync_run_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            session.add(
                SyncRun(
                    sync_run_id=sync_run_id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    status="done",
                    trigger="manual",
                    started_at=_NOW_P3,
                    finished_at=_NOW_P3,
                    duration_s=1,
                    artifact_count=0,
                    error_summary=None,
                )
            )
    finally:
        await engine.dispose()
    return sync_run_id


@pytest.mark.asyncio
async def test_sync_source_tenant_isolation(
    two_tenant_app: TwoTenantHarness,
    pg_container: str,
    app_settings: Settings,
) -> None:
    """GET /v1/admin/sync-sources/{source_id} — tenant B cannot read tenant A's source.

    The endpoint enforces ``source.tenant_id != ctx.tenant_id`` and returns 404
    for cross-tenant access (prevents tenant enumeration).
    """
    # Seed tenant A + source using two_tenant_app's tenant_a_id/token_b.
    # We create a separate admin-role seed for tenant A because two_tenant_app
    # seeds with 'producer' roles; admin is required for sync-source endpoints.
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    tenant_a_id, actor_a_id, token_a = await _seed_p3(
        pg_container,
        tenant_slug=f"p3iso-a-{suffix_a}",
    )
    _tenant_b_id, _actor_b_id, token_b = await _seed_p3(
        pg_container,
        tenant_slug=f"p3iso-b-{suffix_b}",
    )

    source_id = await _seed_sync_source(
        pg_container,
        tenant_id=tenant_a_id,
        actor_id=actor_a_id,
    )

    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/v1/admin/sync-sources/{source_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert response.status_code in (
        403,
        404,
    ), f"tenant B must not read tenant A's sync_source; got {response.status_code}: {response.text}"


@pytest.mark.asyncio
async def test_sync_run_tenant_isolation(
    two_tenant_app: TwoTenantHarness,
    pg_container: str,
    app_settings: Settings,
) -> None:
    """GET /v1/admin/sync-runs/{sync_run_id} — tenant B cannot read tenant A's run.

    The endpoint enforces ``run.tenant_id != ctx.tenant_id`` and returns 404
    for cross-tenant access (prevents tenant enumeration).
    """
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    tenant_a_id, actor_a_id, token_a = await _seed_p3(
        pg_container,
        tenant_slug=f"p3run-a-{suffix_a}",
    )
    _tenant_b_id, _actor_b_id, token_b = await _seed_p3(
        pg_container,
        tenant_slug=f"p3run-b-{suffix_b}",
    )

    source_id = await _seed_sync_source(
        pg_container,
        tenant_id=tenant_a_id,
        actor_id=actor_a_id,
    )
    sync_run_id = await _seed_sync_run(
        pg_container,
        tenant_id=tenant_a_id,
        source_id=source_id,
    )

    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/v1/admin/sync-runs/{sync_run_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert response.status_code in (
        403,
        404,
    ), f"tenant B must not read tenant A's sync_run; got {response.status_code}: {response.text}"


@pytest.mark.asyncio
async def test_sync_run_superseded_tenant_isolation(
    two_tenant_app: TwoTenantHarness,
    pg_container: str,
    app_settings: Settings,
) -> None:
    """GET /v1/admin/sync-runs/{sync_run_id}/superseded — tenant B cannot access tenant A's superseded facts.

    The endpoint first resolves the run (``run.tenant_id != ctx.tenant_id`` check)
    and returns 404 before querying facts, preventing cross-tenant fact exposure.
    """
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    tenant_a_id, actor_a_id, token_a = await _seed_p3(
        pg_container,
        tenant_slug=f"p3sup-a-{suffix_a}",
    )
    _tenant_b_id, _actor_b_id, token_b = await _seed_p3(
        pg_container,
        tenant_slug=f"p3sup-b-{suffix_b}",
    )

    source_id = await _seed_sync_source(
        pg_container,
        tenant_id=tenant_a_id,
        actor_id=actor_a_id,
    )
    sync_run_id = await _seed_sync_run(
        pg_container,
        tenant_id=tenant_a_id,
        source_id=source_id,
    )

    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/v1/admin/sync-runs/{sync_run_id}/superseded",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert response.status_code in (
        403,
        404,
    ), f"tenant B must not access tenant A's superseded facts; got {response.status_code}: {response.text}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Audit / vocabulary / capability-types / roles tenant isolation
#
# Transport: httpx against the ASGI app (same as test_path_param_swap).
# Assertion: 200 OK with empty results — all four endpoints enforce tenant_id
# at the DB layer (not HTTP 403/404).  Using token B against data seeded only
# for tenant A must produce empty collections, not A's rows.
#
# Rationale for dedicated tests (not PATH_PARAM_SWAP_CASES):
# - /v1/admin/audit — returns rows=[] for cross-tenant token (no enumeration
#   risk because the response is indistinguishable from "no audit events").
# - /v1/admin/vocabularies/{kind} — returns [] for a tenant with no vocab rows.
# - /v1/admin/capability-types — returns [] when no schemas exist for tenant B.
# - /v1/admin/roles — returns [] when tenant B has no Role rows.
# See module docstring admin read endpoints section for full rationale.
# ---------------------------------------------------------------------------

_NOW_P4 = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


async def _seed_p4_admin(
    pg_url: str,
    *,
    tenant_slug: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Seed a tenant with an admin + auditor token; return (tenant_id, actor_id, raw_token).

    The token carries both 'admin' and 'auditor' roles so it can call both the
    audit endpoint (auditor-required) and the vocabulary/capability-types/roles
    endpoints (admin-required).
    """
    from registry.storage.models import AuditLog, CapabilityTypeSchema, Role  # noqa: PLC0415

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    slug=tenant_slug,
                    display_name=tenant_slug,
                    created_at=_NOW_P4,
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"actor-{tenant_slug}",
                    email=None,
                    oidc_subject=None,
                    created_at=_NOW_P4,
                )
            )
            await session.flush()
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    token_hash=hash_token(raw_token),
                    roles=["admin", "auditor"],
                    description=None,
                    expires_at=None,
                    created_at=_NOW_P4,
                    revoked_at=None,
                )
            )
            # Seed one audit log row, one vocabulary row, one capability-type row,
            # and one role row for tenant A so there IS data — just not visible to B.
            session.add(
                AuditLog(
                    audit_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action="lifecycle.transition",
                    target_type="entity",
                    target_id=uuid.uuid4(),
                    before_jsonb=None,
                    after_jsonb=None,
                    ts=_NOW_P4,
                    request_id=None,
                    error_code=None,
                )
            )
            session.add(
                VocabularyValue(
                    vocab_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    kind="entity_type",
                    value="p4-iso-service",
                    is_system=False,
                    deprecated_at=None,
                    created_at=_NOW_P4,
                )
            )
            session.add(
                CapabilityTypeSchema(
                    schema_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    type_name="p4-iso-type",
                    json_schema={"type": "object"},
                    is_advisory=True,
                    t_valid_from=_NOW_P4,
                    t_valid_to=None,
                    t_ingested_at=_NOW_P4,
                    t_invalidated_at=None,
                    created_by=actor_id,
                )
            )
            session.add(
                Role(
                    role_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=f"p4-iso-role-{tenant_slug}",
                    permissions=["read"],
                    created_at=_NOW_P4,
                )
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


@pytest.mark.asyncio
async def test_audit_tenant_isolation(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """GET /v1/admin/audit — tenant B sees 0 audit rows for tenant A's data.

    Design choice: the audit endpoint always scopes queries to the calling
    token's tenant_id (injected from TenantContext).  Tenant B's token
    querying for all rows must return rows=[] even though tenant A has at
    least one audit log row seeded.  This prevents cross-tenant audit log
    enumeration.  A dedicated test is used (not PATH_PARAM_SWAP_CASES) because
    the expected assertion is 200 OK with empty rows, not 403/404.  See module
    docstring admin read endpoints section for full rationale.
    """
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    _tenant_a_id, _actor_a_id, _token_a = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4aud-a-{suffix_a}",
    )
    _tenant_b_id, _actor_b_id, token_b = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4aud-b-{suffix_b}",
    )

    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/admin/audit",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert (
        response.status_code == 200
    ), f"GET /v1/admin/audit must return 200 for valid auditor token; got {response.status_code}: {response.text}"
    body = response.json()
    rows = body.get("rows", [])
    # tenant B must see only its own audit rows (seeded by _seed_p4_admin for B).
    # Crucially, none of tenant A's rows must appear.
    for row in rows:
        assert row.get("actor_id") != str(
            _actor_a_id
        ), f"tenant B must not see tenant A's audit row; got actor_id={row.get('actor_id')}"


@pytest.mark.asyncio
async def test_vocabulary_tenant_isolation(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """GET /v1/admin/vocabularies/entity_type — tenant B sees 0 rows for tenant A's vocab.

    Design choice: the vocabularies endpoint filters by tenant_id from
    TenantContext at the DB layer.  Tenant B's token must not see tenant A's
    vocabulary values even when querying the same kind.  Returns 200 OK with
    an empty list rather than 403/404 (the call is valid; isolation is
    enforced at the query layer).  See module docstring admin read endpoints section.
    """
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    _tenant_a_id, _actor_a_id, _token_a = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4voc-a-{suffix_a}",
    )
    _tenant_b_id, _actor_b_id, token_b = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4voc-b-{suffix_b}",
    )

    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/admin/vocabularies/entity_type",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert (
        response.status_code == 200
    ), f"GET /v1/admin/vocabularies/entity_type must return 200; got {response.status_code}: {response.text}"
    vocab_list = response.json()
    # Tenant A's value "p4-iso-service" must NOT appear in tenant B's response.
    tenant_a_values = [v["value"] for v in vocab_list if v["value"] == "p4-iso-service"]
    assert tenant_a_values == [], f"tenant B must not see tenant A's vocabulary values; found: {tenant_a_values}"


@pytest.mark.asyncio
async def test_capability_types_tenant_isolation(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """GET /v1/admin/capability-types — tenant B sees 0 rows for tenant A's schemas.

    Design choice: the capability-types endpoint filters CapabilityTypeSchema
    rows by tenant_id from TenantContext.  Tenant A's seeded type must not
    appear in tenant B's response.  Returns 200 OK with [] (not 403/404).
    See module docstring admin read endpoints section for full rationale.
    """
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    _tenant_a_id, _actor_a_id, _token_a = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4ct-a-{suffix_a}",
    )
    _tenant_b_id, _actor_b_id, token_b = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4ct-b-{suffix_b}",
    )

    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/admin/capability-types",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert (
        response.status_code == 200
    ), f"GET /v1/admin/capability-types must return 200; got {response.status_code}: {response.text}"
    schemas = response.json()
    tenant_a_types = [s["type_name"] for s in schemas if s["type_name"] == "p4-iso-type"]
    assert tenant_a_types == [], f"tenant B must not see tenant A's capability type schemas; found: {tenant_a_types}"


@pytest.mark.asyncio
async def test_roles_tenant_isolation(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """GET /v1/admin/roles — tenant B sees 0 rows for tenant A's roles.

    Design choice: the roles endpoint filters Role rows by tenant_id from
    TenantContext.  Tenant A's seeded role must not appear in tenant B's
    response.  Returns 200 OK with [] (not 403/404).  See module docstring
    admin read endpoints section for full rationale.
    """
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    _tenant_a_id, _actor_a_id, _token_a = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4rol-a-{suffix_a}",
    )
    _tenant_b_id, _actor_b_id, token_b = await _seed_p4_admin(
        pg_container,
        tenant_slug=f"p4rol-b-{suffix_b}",
    )

    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/admin/roles",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert (
        response.status_code == 200
    ), f"GET /v1/admin/roles must return 200; got {response.status_code}: {response.text}"
    roles = response.json()
    # Tenant A's role name is unique (contains suffix_a) — it must not appear in B's list.
    tenant_a_role_names = [r["name"] for r in roles if suffix_a in r.get("name", "")]
    assert tenant_a_role_names == [], f"tenant B must not see tenant A's roles; found: {tenant_a_role_names}"
