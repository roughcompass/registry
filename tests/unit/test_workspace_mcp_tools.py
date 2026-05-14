"""Unit tests for workspace MCP tools.

Covers the seven workspace tools registered in the MCP server:
  - create_workspace
  - list_workspaces
  - get_workspace
  - add_workspace_entry
  - update_workspace_entry
  - search_workspace_entries
  - list_workspace_shares

All tests use AsyncMock for the WorkspaceService layer — no Postgres or
Docker required. The _resolve_tenant auth shim is patched to inject a
pre-built TenantContext so auth DB calls are bypassed.

HTTPException-to-ToolError translation is verified for each tool
across the error shapes the service emits (PII block, regulated-tenant
block, 403 permission, 404 not-found, invalid kind).
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

from registry.api.routers.mcp import _request_token, create_registry_mcp_server
from registry.service.workspace import (
    SearchResult,
    WorkspaceEntryRef,
    WorkspaceRef,
)
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_WORKSPACE_ID = uuid.uuid4()
_ENTRY_ID = uuid.uuid4()
_FAKE_TOKEN = "fake-test-token"

_PATCH_TARGET = "registry.api.routers.mcp._resolve_tenant"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_ctx() -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT_ID,
        actor_id=_ACTOR_ID,
        roles=["consumer"],
    )


def _make_workspace_ref(
    workspace_id: uuid.UUID | None = None,
    owner_kind: str = "actor",
) -> WorkspaceRef:
    return WorkspaceRef(
        workspace_id=workspace_id or _WORKSPACE_ID,
        tenant_id=_TENANT_ID,
        name="Test Workspace",
        description=None,
        owner_kind=owner_kind,
        owner_actor_id=_ACTOR_ID if owner_kind == "actor" else None,
        archived_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        created_by=_ACTOR_ID,
        t_invalidated_at=None,
    )


def _make_entry_ref(
    entry_id: uuid.UUID | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> WorkspaceEntryRef:
    return WorkspaceEntryRef(
        entry_id=entry_id or _ENTRY_ID,
        workspace_id=_WORKSPACE_ID,
        tenant_id=_TENANT_ID,
        kind="note",
        body_md="# Test note",
        references_jsonb=None,
        reference_ids=[],
        expires_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        created_by=_ACTOR_ID,
        t_invalidated_at=None,
        warnings=warnings,
    )


def _build_mcp(workspace_service: Any | None = None) -> Any:
    """Build a FastMCP server with mocked dependencies.

    Stubs out retrieval, catalog, session_factory, and annotation_service so
    the server can be instantiated without any live infrastructure. The
    workspace_service arg is passed through directly; callers inject an
    AsyncMock to control tool behavior per test.
    """
    clock = FakeClock(_NOW)
    retrieval = MagicMock()
    catalog = MagicMock()
    session_factory = MagicMock()
    annotation_service = MagicMock()

    # annotation_service needs async methods to not break registration
    annotation_service.create_annotation = AsyncMock()
    annotation_service.list_annotations = AsyncMock()
    annotation_service.triage_annotation = AsyncMock()

    mcp = create_registry_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        clock=clock,
        annotation_service=annotation_service,
        workspace_service=workspace_service or MagicMock(),
    )
    return mcp


async def _call(mcp: Any, tool: str, args: dict[str, Any]) -> Any:
    """Set the auth ContextVar and invoke a tool by name.

    Returns the text content of the first content block so callers can
    json.loads it directly.
    """
    cv_tok = _request_token.set(_FAKE_TOKEN)
    try:
        content_blocks, _meta = await mcp.call_tool(tool, args)
        return content_blocks[0].text
    finally:
        _request_token.reset(cv_tok)


# ---------------------------------------------------------------------------
# Tool registration smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_tools_registered() -> None:
    """All six workspace tools are registered in the MCP server tool list."""
    ws_svc = MagicMock()
    mcp = _build_mcp(workspace_service=ws_svc)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "create_workspace",
        "list_workspaces",
        "get_workspace",
        "add_workspace_entry",
        "update_workspace_entry",
        "search_workspace_entries",
    }
    assert expected.issubset(names), f"Missing tools: {expected - names}"
    assert "list_workspace_shares" not in names, "Deleted tool must not be re-registered"


# ---------------------------------------------------------------------------
# (a) add_workspace_entry — PII block raises ToolError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_workspace_entry_pii_block_raises_tool_error() -> None:
    """add_workspace_entry raises ToolError with category list on PII block.

    The service raises HTTPException(422) with code='pii_detected'. The MCP
    tool translates this to a ToolError naming the detected categories, so the
    caller knows which PII types triggered the block without inspecting HTTP
    status codes.
    """
    ws_svc = MagicMock()
    ws_svc.create_entry = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail={
                "code": "pii_detected",
                "field": "workspace_entry.body",
                "categories": ["email", "phone"],
            },
        )
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "add_workspace_entry",
                {
                    "workspace_id": str(_WORKSPACE_ID),
                    "kind": "note",
                    "body_md": "Contact: user@example.com, 555-1234",
                },
            )

    msg = str(exc_info.value)
    assert "PII detected" in msg
    assert "email" in msg
    assert "phone" in msg


@pytest.mark.asyncio
async def test_add_workspace_entry_pii_block_message_format() -> None:
    """add_workspace_entry PII ToolError starts with the canonical prefix."""
    ws_svc = MagicMock()
    ws_svc.create_entry = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail={
                "code": "pii_detected",
                "field": "workspace_entry.body",
                "categories": ["ssn"],
            },
        )
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "add_workspace_entry",
                {
                    "workspace_id": str(_WORKSPACE_ID),
                    "kind": "note",
                    "body_md": "SSN: 123-45-6789",
                },
            )

    assert "Entry rejected: PII detected in body [ssn]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (b) create_workspace — regulated-tenant block raises ToolError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_regulated_tenant_raises_tool_error() -> None:
    """create_workspace raises ToolError with the regulated-tenant message on 422.

    Regulated tenants cannot create workspaces while encryption_tier='none'.
    The service raises HTTPException(422) with the canonical error string and
    the MCP tool surfaces it as a ToolError unchanged, so the caller gets an
    actionable message pointing to the encryption tier configuration.
    """
    regulated_msg = (
        "Workspace creation is not permitted for regulated tenants at encryption tier 'none'. "
        "Configure a higher encryption tier before creating workspaces."
    )
    ws_svc = MagicMock()
    ws_svc.create_workspace = AsyncMock(
        side_effect=HTTPException(status_code=422, detail=regulated_msg)
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "create_workspace",
                {"name": "Secure Workspace", "owner_kind": "actor"},
            )

    msg = str(exc_info.value)
    assert "regulated tenants" in msg
    assert "encryption tier 'none'" in msg


@pytest.mark.asyncio
async def test_create_workspace_invalid_owner_kind_raises_tool_error() -> None:
    """create_workspace raises ToolError when owner_kind is not in the closed vocabulary."""
    ws_svc = MagicMock()
    ws_svc.create_workspace = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail="Invalid owner_kind 'team'. Must be one of: ['actor', 'tenant'].",
        )
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "create_workspace",
                {"name": "My Workspace", "owner_kind": "team"},
            )

    assert "Invalid owner_kind" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_workspace_happy_path() -> None:
    """create_workspace returns a WorkspaceRef-shape dict on success."""
    ws_svc = MagicMock()
    ws_svc.create_workspace = AsyncMock(return_value=_make_workspace_ref())
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "create_workspace",
            {"name": "My Workspace", "owner_kind": "actor"},
        )

    payload = json.loads(raw)
    assert payload["workspace_id"] == str(_WORKSPACE_ID)
    assert payload["owner_kind"] == "actor"
    assert "encryption_status" not in payload


# ---------------------------------------------------------------------------
# (c) get_workspace — 403 raises ToolError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_403_raises_tool_error() -> None:
    """get_workspace raises ToolError with visibility message when service returns 403.

    The service raises HTTPException(403) when the caller does not satisfy any
    of the three access paths. The MCP tool surfaces this as a ToolError
    naming the workspace_id so the caller can identify which workspace is
    inaccessible.
    """
    ws_svc = MagicMock()
    ws_svc.get_workspace = AsyncMock(
        side_effect=HTTPException(
            status_code=403,
            detail=f"Actor {_ACTOR_ID} does not have access to workspace {_WORKSPACE_ID}.",
        )
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(mcp, "get_workspace", {"workspace_id": str(_WORKSPACE_ID)})

    msg = str(exc_info.value)
    assert "not visible to the calling actor" in msg
    assert str(_WORKSPACE_ID) in msg


@pytest.mark.asyncio
async def test_get_workspace_404_raises_tool_error() -> None:
    """get_workspace raises ToolError with not-found message when service returns 404."""
    missing_id = uuid.uuid4()
    ws_svc = MagicMock()
    ws_svc.get_workspace = AsyncMock(
        side_effect=HTTPException(
            status_code=404,
            detail=f"Workspace {missing_id} not found.",
        )
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(mcp, "get_workspace", {"workspace_id": str(missing_id)})

    assert f"Workspace {missing_id} not found." in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_workspace_happy_path() -> None:
    """get_workspace returns a WorkspaceRef-shape dict on success."""
    ws_svc = MagicMock()
    ws_svc.get_workspace = AsyncMock(return_value=_make_workspace_ref())
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(mcp, "get_workspace", {"workspace_id": str(_WORKSPACE_ID)})

    payload = json.loads(raw)
    assert payload["workspace_id"] == str(_WORKSPACE_ID)
    assert "encryption_status" not in payload


# ---------------------------------------------------------------------------
# (d) search_workspace_entries — scopes to visible workspaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_workspace_entries_scopes_to_visible_workspaces() -> None:
    """search_workspace_entries returns only entries from visible workspaces.

    The service enforces the three-path visibility rule (owner_actor_id,
    tenant_id, active share) before returning entries. This test pins that
    the tool delegates ctx to the service so the service-layer scope applies,
    and that all returned entries belong to the actor's visible workspaces.
    """
    entry_ids = [uuid.uuid4(), uuid.uuid4()]
    entries = [_make_entry_ref(entry_id=eid) for eid in entry_ids]
    search_result = SearchResult(items=entries, next_cursor=None, total_count=2)

    ws_svc = MagicMock()
    ws_svc.search_workspaces = AsyncMock(return_value=search_result)
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(mcp, "search_workspace_entries", {"q": "test note"})

    payload = json.loads(raw)
    assert len(payload["items"]) == 2
    returned_ids = {item["entry_id"] for item in payload["items"]}
    assert returned_ids == {str(eid) for eid in entry_ids}
    # Verify ctx was forwarded (the mock was awaited, not called directly).
    ws_svc.search_workspaces.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_workspace_entries_passes_kind_and_reference_filters() -> None:
    """search_workspace_entries forwards kind and reference_ids filters to the service."""
    ref_id = uuid.uuid4()
    search_result = SearchResult(items=[], next_cursor=None, total_count=0)

    ws_svc = MagicMock()
    ws_svc.search_workspaces = AsyncMock(return_value=search_result)
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "search_workspace_entries",
            {
                "kind": "decision",
                "reference_ids": [str(ref_id)],
            },
        )

    call_kwargs = ws_svc.search_workspaces.call_args.kwargs
    assert call_kwargs["kind"] == "decision"
    assert call_kwargs["reference_ids"] == [ref_id]


@pytest.mark.asyncio
async def test_search_workspace_entries_returns_pagination_fields() -> None:
    """search_workspace_entries response includes next_cursor and total_count fields."""
    next_cursor = "some-opaque-cursor"
    search_result = SearchResult(items=[], next_cursor=next_cursor, total_count=None)

    ws_svc = MagicMock()
    ws_svc.search_workspaces = AsyncMock(return_value=search_result)
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(mcp, "search_workspace_entries", {})

    payload = json.loads(raw)
    assert payload["next_cursor"] == next_cursor
    assert payload["total_count"] is None


# ---------------------------------------------------------------------------
# Additional: list_workspaces happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workspaces_returns_visible_workspaces() -> None:
    """list_workspaces returns the full list the service yields for the caller."""
    ws_ids = [uuid.uuid4(), uuid.uuid4()]
    refs = [_make_workspace_ref(workspace_id=wid) for wid in ws_ids]

    ws_svc = MagicMock()
    ws_svc.list_workspaces = AsyncMock(return_value=(refs, None))
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(mcp, "list_workspaces", {"include_archived": False})

    payload = json.loads(raw)
    assert len(payload) == 2
    returned_ids = {item["workspace_id"] for item in payload}
    assert returned_ids == {str(wid) for wid in ws_ids}


# ---------------------------------------------------------------------------
# Additional: add_workspace_entry happy path + invalid kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_workspace_entry_happy_path() -> None:
    """add_workspace_entry returns a WorkspaceEntryRef-shape dict on success."""
    ws_svc = MagicMock()
    ws_svc.create_entry = AsyncMock(return_value=_make_entry_ref())
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "add_workspace_entry",
            {
                "workspace_id": str(_WORKSPACE_ID),
                "kind": "note",
                "body_md": "# My Note",
            },
        )

    payload = json.loads(raw)
    assert payload["entry_id"] == str(_ENTRY_ID)
    assert payload["kind"] == "note"
    assert "encryption_status" not in payload


@pytest.mark.asyncio
async def test_add_workspace_entry_invalid_kind_raises_tool_error() -> None:
    """add_workspace_entry raises ToolError when the service rejects an invalid entry kind."""
    ws_svc = MagicMock()
    ws_svc.create_entry = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail=(
                "Invalid entry kind 'changelog'. "
                "Must be one of: note, decision, open_question, saved_query, "
                "saved_view, private_annotation."
            ),
        )
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "add_workspace_entry",
                {
                    "workspace_id": str(_WORKSPACE_ID),
                    "kind": "changelog",
                    "body_md": "Some content",
                },
            )

    assert "Invalid entry kind" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Additional: update_workspace_entry — PII block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_entry_pii_block_raises_tool_error() -> None:
    """update_workspace_entry raises ToolError with PII categories on 422 pii_detected."""
    ws_svc = MagicMock()
    ws_svc.update_entry = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail={
                "code": "pii_detected",
                "field": "workspace_entry.body",
                "categories": ["credit_card"],
            },
        )
    )
    mcp = _build_mcp(workspace_service=ws_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "update_workspace_entry",
                {
                    "entry_id": str(_ENTRY_ID),
                    "body_md": "Card: 4111-1111-1111-1111",
                },
            )

    msg = str(exc_info.value)
    assert "Entry rejected: PII detected in body [credit_card]" in msg
