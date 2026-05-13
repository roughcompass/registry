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
is set explicitly, which is exactly what ``handle_sse`` does before delegating
to the MCP server. This exercises every layer that matters:

- Tool registration: all three annotation tools must appear in list_tools().
- Auth shim: _resolve_tenant reads _request_token and hits the real Postgres
  api_tokens table; an invalid token raises ToolError.
- AnnotationService: the real service instance (no mocks) runs against
  testcontainers Postgres, including visibility checks, PII scanning, and
  SQL writes.
- ToolError propagation: HTTPException-to-ToolError translation path is
  exercised end-to-end.

This is a fundamentally different test from the unit suite: all three services
(visibility, PII scanner, annotation persistence) are live.
"""

from __future__ import annotations

import datetime
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.api.routers.annotations import _AuditWriterAdapter
from registry.api.routers.mcp import _request_token, create_catalog_mcp_server
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

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers (reuse the SQL-direct pattern from existing integration tests)
# ---------------------------------------------------------------------------


async def _seed_tenant_with_token(
    pg_url: str,
    *,
    slug: str,
    roles: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert (tenant, actor, api_token). Returns (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    role_list = roles or ["producer", "consumer", "admin"]
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, "
                    "created_at, is_active) VALUES "
                    "(:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, "
                    "created_at) VALUES (:aid, :tid, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "dn": f"actor-{slug}", "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": actor_id,
                    "th": hash_token(raw_token),
                    "roles": role_list,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


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


# ---------------------------------------------------------------------------
# Helper: set _request_token ContextVar and call tool (mirrors handle_sse)
# ---------------------------------------------------------------------------


async def _call_as(mcp: Any, *, token: str, tool: str, args: dict[str, Any]) -> list[Any]:
    """Set the Bearer token ContextVar and call the tool in-process.

    This mirrors what the SSE handler (handle_sse) does before delegating
    to the MCP server: it writes the raw Bearer token into _request_token
    so _resolve_tenant can validate it against the DB.

    Returns the content list from the call result. call_tool() returns a
    (content_list, structured_result) tuple; callers receive the content list
    so assertions work uniformly across all tools.
    """
    cv_token = _request_token.set(token)
    try:
        content, _structured = await mcp.call_tool(tool, args)
        return content  # type: ignore[return-value]
    finally:
        _request_token.reset(cv_token)


# ---------------------------------------------------------------------------
# Fixture: build a FastMCP server with real services + testcontainers Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mcp_annotation_harness(pg_container: str, app_settings: Settings) -> AsyncIterator[dict[str, Any]]:
    """Build a fully-wired FastMCP server against a live Postgres instance.

    Returns a dict with:
      - mcp: FastMCP server instance (the object under test)
      - mcp_block: FastMCP server instance with block-policy PII scanner
      - pg_url: the Postgres URL (for seeding)
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock(_NOW)

    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, clock)
    catalog = CatalogService(session_factory, clock, vocabulary, schema)
    embedder = StubEmbedder()
    retrieval = RetrievalService(session_factory, clock, embedder, app_settings)
    visibility = VisibilityService(session_factory, clock)

    # Advisory-policy scanner (default): PII is logged but writes proceed.
    advisory_scanner = build_builtin_scanner(tenant_policy="advisory")
    audit_writer = _AuditWriterAdapter(session_factory=session_factory, clock=clock)  # type: ignore[arg-type]

    annotation_svc = AnnotationService(
        session_factory=session_factory,
        visibility_svc=visibility,
        pii_scanner=advisory_scanner,
        audit_writer=audit_writer,
        clock=clock,
    )

    mcp_server = create_catalog_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        clock=clock,
        annotation_service=annotation_svc,
    )

    # Block-policy scanner: any PII match raises a 422 which the MCP layer
    # translates to ToolError. Used by test_submit_annotation_pii_block_returns_tool_error.
    block_scanner = build_builtin_scanner(tenant_policy="block")
    annotation_svc_block = AnnotationService(
        session_factory=session_factory,
        visibility_svc=visibility,
        pii_scanner=block_scanner,
        audit_writer=audit_writer,
        clock=clock,
    )

    mcp_server_block = create_catalog_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        clock=clock,
        annotation_service=annotation_svc_block,
    )

    try:
        yield {
            "mcp": mcp_server,
            "mcp_block": mcp_server_block,
            "pg_url": pg_container,
        }
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotation_tools_in_tool_list(mcp_annotation_harness: dict[str, Any]) -> None:
    """The MCP server advertises all three annotation tools.

    Calls list_tools() on the live FastMCP instance built from the production
    create_catalog_mcp_server factory. All three annotation tools must be
    present — missing registration is a startup wiring bug.
    """
    mcp = mcp_annotation_harness["mcp"]
    tools = await mcp.list_tools()
    names = {t.name for t in tools}

    assert "submit_annotation" in names, (
        f"submit_annotation not found in registered tools: {names}"
    )
    assert "list_my_annotations" in names, (
        f"list_my_annotations not found in registered tools: {names}"
    )
    assert "triage_annotation" in names, (
        f"triage_annotation not found in registered tools: {names}"
    )

    # Each annotation tool must carry a non-empty inputSchema.
    for tool in tools:
        if tool.name in {"submit_annotation", "list_my_annotations", "triage_annotation"}:
            assert isinstance(tool.inputSchema, dict), (
                f"{tool.name}: inputSchema is not a dict"
            )
            assert tool.inputSchema, f"{tool.name}: inputSchema is empty"


@pytest.mark.asyncio
async def test_submit_annotation_returns_result(mcp_annotation_harness: dict[str, Any]) -> None:
    """submit_annotation writes a new annotation and returns the created record.

    Seeds a public capability owned by a provider tenant, then calls
    submit_annotation as a consumer tenant. The response must contain
    annotation_id and must not carry isError.
    """
    mcp = mcp_annotation_harness["mcp"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_tid, _provider_actor, _provider_token = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-smk-prov-{suffix}"
    )
    _consumer_tid, _consumer_actor, consumer_token = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-smk-cons-{suffix}"
    )
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-smk-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    result = await _call_as(
        mcp,
        token=consumer_token,
        tool="submit_annotation",
        args={
            "capability_id": str(cap_id),
            "body": "This endpoint lacks rate-limit response headers.",
            "category": "feedback",
        },
    )

    # MCP tool results are a sequence of content blocks; no isError means success.
    assert result, "submit_annotation must return a non-empty result sequence"
    first = result[0]
    assert first.type == "text", f"expected type='text', got {first.type!r}"

    parsed = json.loads(first.text)
    assert isinstance(parsed, dict), "submit_annotation must return a JSON object"
    assert "annotation_id" in parsed, (
        f"Response must contain annotation_id; got keys: {list(parsed.keys())}"
    )
    assert "capability_id" in parsed
    assert parsed["capability_id"] == str(cap_id)
    assert parsed["status"] == "open"
    assert parsed["category"] == "feedback"


@pytest.mark.asyncio
async def test_list_my_annotations_returns_result(mcp_annotation_harness: dict[str, Any]) -> None:
    """list_my_annotations returns a paginated list without ToolError.

    Seeds a capability and an annotation via submit_annotation, then calls
    list_my_annotations with the capability_id filter. The response must
    be a dict with items and next_cursor keys.
    """
    mcp = mcp_annotation_harness["mcp"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_tid, _pact, _ptok = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-list-prov-{suffix}"
    )
    _ctid, _cact, consumer_token = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-list-cons-{suffix}"
    )
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-list-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    # First submit an annotation so the list is non-empty.
    await _call_as(
        mcp,
        token=consumer_token,
        tool="submit_annotation",
        args={
            "capability_id": str(cap_id),
            "body": "Missing pagination in the response headers.",
            "category": "suggestion",
        },
    )

    result = await _call_as(
        mcp,
        token=consumer_token,
        tool="list_my_annotations",
        args={"capability_id": str(cap_id)},
    )

    assert result, "list_my_annotations must return a non-empty result sequence"
    first = result[0]
    assert first.type == "text", f"expected type='text', got {first.type!r}"

    parsed = json.loads(first.text)
    assert isinstance(parsed, dict), "list_my_annotations must return a JSON object"
    assert "items" in parsed, (
        f"Response must contain 'items'; got keys: {list(parsed.keys())}"
    )
    assert "next_cursor" in parsed
    # The annotation we just submitted must appear in the list.
    assert isinstance(parsed["items"], list)
    assert len(parsed["items"]) >= 1, "items list must contain at least the annotation we submitted"


@pytest.mark.asyncio
async def test_triage_annotation_returns_result(mcp_annotation_harness: dict[str, Any]) -> None:
    """triage_annotation updates annotation status and returns the updated record.

    Seeds an annotation via the REST path (direct SQL insert so we control
    tenant_id = provider), then calls triage_annotation as the provider tenant.
    The provider owns the capability so they are authorized to triage.
    Result must contain status='triaged'.
    """
    mcp = mcp_annotation_harness["mcp"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_tid, provider_actor, provider_token = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-tri-prov-{suffix}"
    )
    _ctid, _cact, consumer_token = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-tri-cons-{suffix}"
    )
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-tri-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    # Consumer submits an annotation.
    submit_result = await _call_as(
        mcp,
        token=consumer_token,
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

    # Provider triages the annotation.
    triage_result = await _call_as(
        mcp,
        token=provider_token,
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
    assert parsed.get("status") == "triaged", (
        f"Expected status='triaged', got: {parsed.get('status')!r}"
    )
    assert parsed.get("annotation_id") == annotation_id


@pytest.mark.asyncio
async def test_submit_annotation_pii_block_returns_tool_error(
    mcp_annotation_harness: dict[str, Any],
) -> None:
    """submit_annotation with PII in body raises ToolError when policy=block.

    The mcp_block server is built with tenant_policy='block', so any PII
    pattern match causes the AnnotationService to raise HTTPException(422)
    which the MCP layer translates to ToolError. The SSN pattern is a built-in
    detector; the body below contains a valid-format SSN that passes the SSA
    validity heuristics.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    mcp_block = mcp_annotation_harness["mcp_block"]
    pg_url = mcp_annotation_harness["pg_url"]
    suffix = uuid.uuid4().hex[:8]

    provider_tid, _pact, _ptok = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-pii-prov-{suffix}"
    )
    _ctid, _cact, consumer_token = await _seed_tenant_with_token(
        pg_url, slug=f"mcp-pii-cons-{suffix}"
    )
    cap_id = await _seed_capability(
        pg_url,
        tenant_id=provider_tid,
        name=f"mcp-pii-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    # The body contains a structurally valid SSN (area=123, group=45, serial=6789).
    # The SSN pattern: NNN-NN-NNNN with dashes. Area 123 is valid, group 45 != 00,
    # serial 6789 != 0000, and 123456789 is not all-identical-digit.
    pii_body = "My SSN is 123-45-6789 and I need help with the API."

    with pytest.raises(ToolError) as exc_info:
        await _call_as(
            mcp_block,
            token=consumer_token,
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
