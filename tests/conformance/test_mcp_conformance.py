"""MCP protocol conformance suite.

Transport strategy
------------------
The MCP server exposes SSE transport at runtime, but driving SSE over an
ASGI test client requires a persistent event-stream connection that's
hard to make deterministic in pytest-asyncio without a real running
server. Instead, this suite calls the FastMCP instance *in-process* via:

    await mcp_server.list_tools()
    await mcp_server.call_tool(name, arguments)

That path exercises every layer that matters for protocol conformance:

- The tool handler code (search, get, list, dependencies, workspaces, …).
- The ``_resolve_tenant`` auth shim, which reads the
  ``_request_token`` / ``_request_app`` / ``_request_x_tenant_id``
  ContextVars — the same vars ``handle_sse`` sets before delegating.
- The SQLAlchemy service layer against a real testcontainers Postgres.
- ``ToolError`` propagation on auth failure and cross-tenant access.

Auth uses tests/helpers/auth_harness.py: the OIDC validator is patched
to return a fixed identity, and the entitlement resolver's fetcher is
swapped for an ``AsyncMock``. The ContextVars are populated explicitly
inside the ``_call_as`` helper so the in-process call exactly mirrors
what the SSE handler does.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.routers.mcp import (
    _request_app,
    _request_token,
    _request_x_tenant_id,
    create_registry_mcp_server,
)
from registry.types import FakeClock
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_PAST = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)


class TenantFixture(NamedTuple):
    persona: TenantPersona
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    entity_id: uuid.UUID


class McpHarness(NamedTuple):
    mcp: Any  # FastMCP instance
    auth: EntitlementAuthHarness
    tenant_a: TenantFixture
    tenant_b: TenantFixture


async def _seed_vocabulary(pg_url: str, tenant_id: uuid.UUID) -> None:
    """Seed the minimum vocabulary the catalog services need for entity inserts."""
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            for kind, value in [
                ("entity_type", "capability"),
                ("entity_type", "concept"),
                ("entity_type", "operation"),
                ("fact_category", "overview"),
                ("fact_category", "adr"),
                ("fact_category", "dev_doc"),
                ("edge_rel", "concept_of"),
                ("edge_rel", "operation_of"),
                ("edge_rel", "depends_on"),
                ("edge_rel", "replaced_by"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()


async def _seed_entity_with_fact(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_name: str,
    fact_body: str,
    fact_valid_from: datetime.datetime,
) -> uuid.UUID:
    """Insert one capability + one fact via direct SQL.

    Bypasses the catalog services so the test can seed across tenants
    without juggling personas during fixture setup.
    """
    entity_id = uuid.uuid4()
    fact_id = uuid.uuid4()
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                ),
                {
                    "eid": entity_id,
                    "tid": tenant_id,
                    "name": entity_name,
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO facts "
                    "(fact_id, tenant_id, entity_id, category, body, "
                    "t_valid_from, t_ingested_at, created_by) "
                    "VALUES (:fid, :tid, :eid, 'overview', :body, "
                    ":valid_from, :now, :actor)"
                ),
                {
                    "fid": fact_id,
                    "tid": tenant_id,
                    "eid": entity_id,
                    "body": fact_body,
                    "valid_from": fact_valid_from,
                    "now": _NOW,
                    "actor": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return entity_id


async def _materialise_persona_via_whoami(
    h: EntitlementAuthHarness, persona: TenantPersona
) -> tuple[uuid.UUID, uuid.UUID]:
    """JIT-create the tenant + actor and return their ids.

    Done by issuing one HTTP /v1/whoami call inside a patched validator
    context. The harness's resolver chain takes care of the rest.
    """
    from httpx import ASGITransport, AsyncClient

    h.configure_fetcher_for(persona)
    transport = ASGITransport(app=h.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get(
                "/v1/whoami",
                headers={
                    "Authorization": "Bearer dummy",
                    "X-Tenant-ID": persona.slug,
                },
            )
            assert resp.status_code == 200, resp.text

    # Look up the materialised ids.
    engine = create_async_engine(
        h._pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row_t = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": persona.slug},
                )
            ).first()
            assert row_t is not None
            tenant_id = uuid.UUID(str(row_t[0]))
            row_a = (
                await session.execute(
                    text(
                        "SELECT actor_id FROM actors "
                        "WHERE tenant_id = :tid AND oidc_subject = :sub"
                    ),
                    {"tid": tenant_id, "sub": persona.oidc_subject},
                )
            ).first()
            assert row_a is not None
            actor_id = uuid.UUID(str(row_a[0]))
    finally:
        await engine.dispose()
    return tenant_id, actor_id


@pytest_asyncio.fixture
async def mcp_harness(pg_container: str) -> AsyncIterator[McpHarness]:
    """Build a FastMCP instance wired against two seeded tenants."""
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]

    async with EntitlementAuthHarness(pg_container) as auth:
        persona_a = auth.add_persona(
            f"mcp-alpha-{suffix_a}", roles=["producer", "consumer"]
        )
        persona_b = auth.add_persona(
            f"mcp-beta-{suffix_b}", roles=["producer", "consumer"]
        )
        tenant_a_id, actor_a_id = await _materialise_persona_via_whoami(auth, persona_a)
        tenant_b_id, actor_b_id = await _materialise_persona_via_whoami(auth, persona_b)
        await _seed_vocabulary(pg_container, tenant_a_id)
        await _seed_vocabulary(pg_container, tenant_b_id)
        entity_a_id = await _seed_entity_with_fact(
            pg_container,
            tenant_id=tenant_a_id,
            actor_id=actor_a_id,
            entity_name="payment-service",
            fact_body="Handles payment processing and gateway integrations.",
            fact_valid_from=_PAST,
        )
        entity_b_id = await _seed_entity_with_fact(
            pg_container,
            tenant_id=tenant_b_id,
            actor_id=actor_b_id,
            entity_name="billing-service",
            fact_body="Billing and invoice management.",
            fact_valid_from=_PAST,
        )

        # Reuse the app's already-built services so we don't have to
        # repeat the wiring (audit writer, pii scanner, visibility, etc.).
        # The MCP server has no production reason to live separately —
        # tests only need a FastMCP instance whose tool handlers reach
        # the same SQLAlchemy session factory the app uses.
        mcp = create_registry_mcp_server(
            retrieval=auth.app.state.retrieval,
            catalog=auth.app.state.catalog,
            session_factory=auth.app.state.session_factory,
            annotation_service=auth.app.state.annotation_service,
            workspace_service=auth.app.state.workspace_service,
            clock=FakeClock(_NOW),
        )

        yield McpHarness(
            mcp=mcp,
            auth=auth,
            tenant_a=TenantFixture(persona_a, tenant_a_id, actor_a_id, entity_a_id),
            tenant_b=TenantFixture(persona_b, tenant_b_id, actor_b_id, entity_b_id),
        )


async def _call_as(
    harness: McpHarness,
    *,
    persona: TenantPersona | None,
    tool: str,
    args: dict[str, Any],
    raw_token: str = "dummy.jwt",
) -> Any:
    """Set the three MCP ContextVars + patch the OIDC validator, then
    invoke the tool. Mirrors what handle_sse does at runtime.

    When ``persona`` is None the validator is not patched — the call
    will reach the real validate_oidc_token, which rejects 'dummy.jwt'
    with an authentication error and the test asserts ToolError.
    """
    cv_token = _request_token.set(raw_token)
    cv_app = _request_app.set(harness.auth.app)
    cv_tenant = _request_x_tenant_id.set(persona.slug if persona is not None else "")
    try:
        if persona is not None:
            harness.auth.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                return await harness.mcp.call_tool(tool, args)
        return await harness.mcp.call_tool(tool, args)
    finally:
        _request_token.reset(cv_token)
        _request_app.reset(cv_app)
        _request_x_tenant_id.reset(cv_tenant)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools(mcp_harness: McpHarness) -> None:
    """Server advertises the expected tool catalog with non-empty input
    schemas."""
    tools = await mcp_harness.mcp.list_tools()
    names = {t.name for t in tools}

    expected = {
        # Catalog / retrieval
        "whoami",
        "search_capabilities",
        "get_capability",
        "lookup_by_external_id",
        "get_dependencies",
        "list_capabilities",
        "get_dependents",
        "get_blast_radius",
        # Annotations
        "submit_annotation",
        "list_my_annotations",
        "triage_annotation",
        # Workspaces
        "create_workspace",
        "list_workspaces",
        "get_workspace",
        "add_workspace_entry",
        "update_workspace_entry",
        "search_workspace_entries",
    }
    missing = expected - names
    assert not missing, f"missing required MCP tools: {sorted(missing)}"

    for tool in tools:
        assert isinstance(tool.inputSchema, dict), f"{tool.name}: inputSchema not dict"
        assert tool.inputSchema, f"{tool.name}: inputSchema empty"

    search = next(t for t in tools if t.name == "search_capabilities")
    search_props = search.inputSchema.get("properties", {})
    assert "q" in search_props, "search_capabilities: missing 'q' in inputSchema"
    assert "top_k" in search_props, "search_capabilities: missing 'top_k' in inputSchema"

    get_cap = next(t for t in tools if t.name == "get_capability")
    assert "entity_id" in get_cap.inputSchema.get("properties", {}), (
        "get_capability: missing 'entity_id' in inputSchema"
    )


@pytest.mark.asyncio
async def test_search_capabilities_returns_results(mcp_harness: McpHarness) -> None:
    """search_capabilities returns a valid MCP tool result shape (TextContent
    with a JSON array body), even when the embedder is the stub and the
    lexical arm produces no hits."""
    result = await _call_as(
        mcp_harness,
        persona=mcp_harness.tenant_a.persona,
        tool="search_capabilities",
        args={"q": "payment", "top_k": 5},
    )
    content_blocks = result[0] if isinstance(result, tuple) else result
    assert content_blocks, "call_tool must return non-empty content blocks"
    first = content_blocks[0]
    assert first.type == "text", f"expected type='text', got {first.type!r}"
    parsed = json.loads(first.text)
    assert isinstance(parsed, list), f"search body must be a JSON array, got {type(parsed)}"


@pytest.mark.asyncio
async def test_get_capability_with_time_travel(mcp_harness: McpHarness) -> None:
    """get_capability with as_of returns the entity record. The seeded
    fact_valid_from is 2025-06-01 so as_of=2026-01-01 sees the entity."""
    entity_id = str(mcp_harness.tenant_a.entity_id)
    result = await _call_as(
        mcp_harness,
        persona=mcp_harness.tenant_a.persona,
        tool="get_capability",
        args={"entity_id": entity_id, "as_of": "2026-01-01T00:00:00Z"},
    )
    content_blocks = result[0] if isinstance(result, tuple) else result
    assert content_blocks
    first = content_blocks[0]
    assert first.type == "text"
    parsed = json.loads(first.text)
    assert isinstance(parsed, dict)
    assert parsed.get("entity", {}).get("entity_id") == entity_id, (
        "returned entity_id must match the requested one"
    )


@pytest.mark.asyncio
async def test_invalid_token_mcp_tool_error(mcp_harness: McpHarness) -> None:
    """Missing or invalid Bearer token raises ToolError — not HTTP 401.

    The MCP protocol carries auth failures as ToolError, not as a status
    code. The error message must not leak internal details.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    # Empty token (ContextVar default → '') — `_resolve_tenant` short-
    # circuits before validation.
    with pytest.raises(ToolError):
        await _call_as(
            mcp_harness,
            persona=None,
            tool="list_capabilities",
            args={},
            raw_token="",
        )

    # Token present but no validator-patch persona — validate_oidc_token
    # will reject 'invalid.jwt' (signature/issuer mismatch).
    with pytest.raises(ToolError):
        await _call_as(
            mcp_harness,
            persona=None,
            tool="list_capabilities",
            args={},
            raw_token="invalid-token-that-does-not-decode",
        )


@pytest.mark.asyncio
async def test_cross_tenant_mcp_isolation(mcp_harness: McpHarness) -> None:
    """Tenant B's persona cannot retrieve tenant A's entity via MCP —
    ToolError is raised, not a stripped success response."""
    from mcp.server.fastmcp.exceptions import ToolError

    entity_a_id = str(mcp_harness.tenant_a.entity_id)
    with pytest.raises(ToolError):
        await _call_as(
            mcp_harness,
            persona=mcp_harness.tenant_b.persona,
            tool="get_capability",
            args={"entity_id": entity_a_id},
        )


