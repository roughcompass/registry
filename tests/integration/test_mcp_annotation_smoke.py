"""Integration smoke test: MCP annotation tools registered and callable over the live transport.

Proves that the three annotation tools (submit_annotation, list_my_annotations,
triage_annotation) are correctly wired into the production MCP server and return
valid responses when called through the MCP protocol layer with a real Postgres
backend.

Transport approach
------------------
The MCP server uses SSE transport at runtime. The SSE protocol (persistent
event-stream + back-channel POST) is driven by anyio memory streams and is not
practical to exercise via httpx.ASGITransport in a pytest-asyncio environment
without a live socket.

Instead, tool calls are made in-process via ``mcp_server.call_tool()`` and
``mcp_server.list_tools()`` — the same interface the SSE handler uses
internally. Before each call, the Bearer token ContextVar (``_request_token``)
and the app-reference ContextVar (``_request_app``) are set explicitly, which
is exactly what ``handle_sse`` does. The OIDC validator is patched via the same
``patch_validator_for_actor`` helper used by the REST integration tests, so no
real JWT signing is needed.

This is a fundamentally different test from the unit suite: all three services
(visibility, PII scanner, annotation persistence) are live.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.routers.annotations import _AuditWriterAdapter
from registry.api.routers.mcp import (
    _request_app,
    _request_token,
    _request_x_tenant_id,
    create_registry_mcp_server,
)
from registry.config import Settings
from registry.embedder import StubEmbedder
from registry.security.pii_scanner import build_builtin_scanner
from registry.service.annotations import AnnotationService
from registry.service.catalog import CatalogService
from registry.service.retrieval import RetrievalService
from registry.service.schema import SchemaService
from registry.service.visibility import VISIBILITY_PUBLIC, VisibilityService
from registry.service.vocabulary import VocabularyService
from registry.types import FakeClock
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_capability(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
    visibility: str = VISIBILITY_PUBLIC,
) -> uuid.UUID:
    """Insert one capability entity owned by tenant_id with given visibility."""
    cap_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, "
                    " created_at, visibility) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now, :vis)"
                ),
                {
                    "eid": cap_id,
                    "tid": tenant_id,
                    "name": name,
                    "now": _NOW,
                    "vis": visibility,
                },
            )
    finally:
        await engine.dispose()
    return cap_id


async def _get_tenant_id(pg_url: str, slug: str) -> uuid.UUID:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"), {"slug": slug}
                )
            ).first()
            assert row is not None, f"tenant {slug} not materialised"
            return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Helper: set ContextVars and call tool (mirrors handle_sse)
# ---------------------------------------------------------------------------


async def _call_as(
    mcp: Any,
    *,
    persona: TenantPersona,
    harness_app: Any,
    tool: str,
    args: dict[str, Any],
) -> list[Any]:
    """Set auth ContextVars and call the MCP tool in-process.

    This mirrors what handle_sse does before delegating to the MCP server:
    it writes the raw Bearer token into ``_request_token`` and the FastAPI
    app into ``_request_app``. The OIDC validator is patched for the
    duration of the call via ``patch_validator_for_actor`` so no real JWT
    is needed.

    Returns the content list from the call result.
    """
    cv_token = _request_token.set("harness.dummy.jwt")
    cv_app = _request_app.set(harness_app)
    cv_xtid = _request_x_tenant_id.set(persona.slug)
    try:
        with patch_validator_for_actor(persona):
            content, _structured = await mcp.call_tool(tool, args)
        return content  # type: ignore[return-value]
    finally:
        _request_token.reset(cv_token)
        _request_app.reset(cv_app)
        _request_x_tenant_id.reset(cv_xtid)


# ---------------------------------------------------------------------------
# Fixture: build a FastMCP server with real services + testcontainers Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mcp_annotation_harness(
    pg_container: str, app_settings: Settings
) -> AsyncIterator[dict[str, Any]]:
    """Build a fully-wired FastMCP server against a live Postgres instance.

    Returns a dict with:
      - mcp: FastMCP server instance (the object under test)
      - mcp_block: FastMCP server instance with block-policy PII scanner
      - harness: EntitlementAuthHarness (auth mock + app + personas)
      - pg_url: the Postgres URL (for seeding)
    """
    async with EntitlementAuthHarness(pg_container) as h:
        engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        clock = FakeClock(_NOW)

        vocabulary = VocabularyService(session_factory)
        schema = SchemaService(session_factory, clock)
        catalog = CatalogService(session_factory, clock, vocabulary, schema)
        embedder = StubEmbedder()
        retrieval = RetrievalService(session_factory, clock, embedder, app_settings)
        visibility = VisibilityService(session_factory, clock)

        advisory_scanner = build_builtin_scanner(tenant_policy="advisory")
        audit_writer = _AuditWriterAdapter(session_factory=session_factory, clock=clock)  # type: ignore[arg-type]

        annotation_svc = AnnotationService(
            session_factory=session_factory,
            visibility_svc=visibility,
            pii_scanner=advisory_scanner,
            audit_writer=audit_writer,
            clock=clock,
        )

        mcp_server = create_registry_mcp_server(
            retrieval=retrieval,
            catalog=catalog,
            session_factory=session_factory,
            clock=clock,
            annotation_service=annotation_svc,
            workspace_service=MagicMock(),
        )

        block_scanner = build_builtin_scanner(tenant_policy="block")
        annotation_svc_block = AnnotationService(
            session_factory=session_factory,
            visibility_svc=visibility,
            pii_scanner=block_scanner,
            audit_writer=audit_writer,
            clock=clock,
        )

        mcp_server_block = create_registry_mcp_server(
            retrieval=retrieval,
            catalog=catalog,
            session_factory=session_factory,
            clock=clock,
            annotation_service=annotation_svc_block,
            workspace_service=MagicMock(),
        )

        try:
            yield {
                "mcp": mcp_server,
                "mcp_block": mcp_server_block,
                "harness": h,
                "pg_url": pg_container,
            }
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Helper: materialise a persona for MCP tests
# ---------------------------------------------------------------------------


async def _make_mcp_persona(
    harness: EntitlementAuthHarness,
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> TenantPersona:
    """Register + materialise a persona via /v1/whoami on the harness app."""
    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    persona = harness.add_persona(slug, roles=roles)
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    return persona


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotation_tools_in_tool_list(mcp_annotation_harness: dict[str, Any]) -> None:
    """The MCP server advertises all three annotation tools."""
    mcp = mcp_annotation_harness["mcp"]
    tools = await mcp.list_tools()
    names = {t.name for t in tools}

    assert "submit_annotation" in names, f"submit_annotation not found in registered tools: {names}"
    assert "list_my_annotations" in names, f"list_my_annotations not found in registered tools: {names}"
    assert "triage_annotation" in names, f"triage_annotation not found in registered tools: {names}"

    for tool in tools:
        if tool.name in {"submit_annotation", "list_my_annotations", "triage_annotation"}:
            assert isinstance(tool.inputSchema, dict), f"{tool.name}: inputSchema is not a dict"
            assert tool.inputSchema, f"{tool.name}: inputSchema is empty"


@pytest.mark.asyncio
async def test_submit_annotation_returns_result(mcp_annotation_harness: dict[str, Any]) -> None:
    """submit_annotation writes a new annotation and returns the created record."""
    mcp = mcp_annotation_harness["mcp"]
    harness: EntitlementAuthHarness = mcp_annotation_harness["harness"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-smk-prov-{suffix}", roles=["producer", "consumer"]
    )
    consumer_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-smk-cons-{suffix}", roles=["producer", "consumer"]
    )
    provider_tid = await _get_tenant_id(pg_url, provider_persona.slug)
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-smk-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    harness.configure_fetcher_for(consumer_persona)
    result = await _call_as(
        mcp,
        persona=consumer_persona,
        harness_app=harness.app,
        tool="submit_annotation",
        args={
            "capability_id": str(cap_id),
            "body": "This endpoint lacks rate-limit response headers.",
            "category": "feedback",
        },
    )

    assert result, "submit_annotation must return a non-empty result sequence"
    first = result[0]
    assert first.type == "text", f"expected type='text', got {first.type!r}"

    parsed = json.loads(first.text)
    assert isinstance(parsed, dict), "submit_annotation must return a JSON object"
    assert "annotation_id" in parsed, f"Response must contain annotation_id; got keys: {list(parsed.keys())}"
    assert "capability_id" in parsed
    assert parsed["capability_id"] == str(cap_id)
    assert parsed["status"] == "open"
    assert parsed["category"] == "feedback"


@pytest.mark.asyncio
async def test_list_my_annotations_returns_result(mcp_annotation_harness: dict[str, Any]) -> None:
    """list_my_annotations returns a paginated list without ToolError."""
    mcp = mcp_annotation_harness["mcp"]
    harness: EntitlementAuthHarness = mcp_annotation_harness["harness"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-list-prov-{suffix}", roles=["producer", "consumer"]
    )
    consumer_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-list-cons-{suffix}", roles=["producer", "consumer"]
    )
    provider_tid = await _get_tenant_id(pg_url, provider_persona.slug)
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-list-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    harness.configure_fetcher_for(consumer_persona)
    await _call_as(
        mcp,
        persona=consumer_persona,
        harness_app=harness.app,
        tool="submit_annotation",
        args={
            "capability_id": str(cap_id),
            "body": "Missing pagination in the response headers.",
            "category": "suggestion",
        },
    )

    result = await _call_as(
        mcp,
        persona=consumer_persona,
        harness_app=harness.app,
        tool="list_my_annotations",
        args={"capability_id": str(cap_id)},
    )

    assert result, "list_my_annotations must return a non-empty result sequence"
    first = result[0]
    assert first.type == "text", f"expected type='text', got {first.type!r}"

    parsed = json.loads(first.text)
    assert isinstance(parsed, dict), "list_my_annotations must return a JSON object"
    assert "items" in parsed, f"Response must contain 'items'; got keys: {list(parsed.keys())}"
    assert "next_cursor" in parsed
    assert isinstance(parsed["items"], list)
    assert len(parsed["items"]) >= 1, "items list must contain at least the annotation we submitted"


@pytest.mark.asyncio
async def test_triage_annotation_returns_result(mcp_annotation_harness: dict[str, Any]) -> None:
    """triage_annotation updates annotation status and returns the updated record."""
    mcp = mcp_annotation_harness["mcp"]
    harness: EntitlementAuthHarness = mcp_annotation_harness["harness"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-tri-prov-{suffix}", roles=["producer", "consumer", "admin"]
    )
    consumer_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-tri-cons-{suffix}", roles=["producer", "consumer"]
    )
    provider_tid = await _get_tenant_id(pg_url, provider_persona.slug)
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-tri-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    harness.configure_fetcher_for(consumer_persona)
    submit_result = await _call_as(
        mcp,
        persona=consumer_persona,
        harness_app=harness.app,
        tool="submit_annotation",
        args={
            "capability_id": str(cap_id),
            "body": "The error codes in the API response are not well documented.",
            "category": "doc_gap",
        },
    )
    assert submit_result, "submit_annotation must return a result"
    submit_parsed = json.loads(submit_result[0].text)
    annotation_id = submit_parsed["annotation_id"]

    harness.configure_fetcher_for(provider_persona)
    triage_result = await _call_as(
        mcp,
        persona=provider_persona,
        harness_app=harness.app,
        tool="triage_annotation",
        args={
            "annotation_id": annotation_id,
            "new_status": "triaged",
            "triage_note": "Acknowledged — adding documentation in next release.",
        },
    )

    assert triage_result, "triage_annotation must return a non-empty result sequence"
    first = triage_result[0]
    assert first.type == "text", f"expected type='text', got {first.type!r}"

    parsed = json.loads(first.text)
    assert isinstance(parsed, dict), "triage_annotation must return a JSON object"
    assert parsed.get("status") == "triaged", f"Expected status='triaged', got: {parsed.get('status')!r}"
    assert parsed.get("annotation_id") == annotation_id


@pytest.mark.asyncio
async def test_submit_annotation_pii_block_returns_tool_error(
    mcp_annotation_harness: dict[str, Any],
) -> None:
    """submit_annotation with PII in body raises ToolError when policy=block."""
    from mcp.server.fastmcp.exceptions import ToolError  # noqa: PLC0415

    mcp_block = mcp_annotation_harness["mcp_block"]
    harness: EntitlementAuthHarness = mcp_annotation_harness["harness"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-pii-prov-{suffix}", roles=["producer", "consumer"]
    )
    consumer_persona = await _make_mcp_persona(
        harness, pg_url, slug=f"mcp-pii-cons-{suffix}", roles=["producer", "consumer"]
    )
    provider_tid = await _get_tenant_id(pg_url, provider_persona.slug)
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-pii-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    # Body contains a structurally valid SSN (area=123, group=45, serial=6789).
    pii_body = "My SSN is 123-45-6789 and I need help with the API."

    harness.configure_fetcher_for(consumer_persona)
    with pytest.raises(ToolError) as exc_info:
        await _call_as(
            mcp_block,
            persona=consumer_persona,
            harness_app=harness.app,
            tool="submit_annotation",
            args={
                "capability_id": str(cap_id),
                "body": pii_body,
                "category": "feedback",
            },
        )

    error_message = str(exc_info.value)
    assert "PII detected" in error_message, (
        f"ToolError message must mention 'PII detected'; got: {error_message!r}"
    )
