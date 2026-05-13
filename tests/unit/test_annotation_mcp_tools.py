"""Unit tests for annotation MCP tools.

Covers the three annotation tools registered in the MCP server:
  - submit_annotation: create an annotation on a capability.
  - list_my_annotations: list annotations authored by the calling tenant.
  - triage_annotation: update annotation status (and optional triage note).

All tests use AsyncMock for the AnnotationService layer — no Postgres or
Docker required. The _resolve_tenant auth shim is patched to inject a
pre-built TenantContext so auth DB calls are bypassed.

HTTPException-to-ToolError translation is verified for all three tools
across the error shapes the service emits (PII block, invalid category,
403 permission, 404 not-found).
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

from registry.api.routers.mcp import _request_token, create_catalog_mcp_server
from registry.service.annotations import AnnotationRef
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_CAP_ID = uuid.uuid4()
_ANN_ID = uuid.uuid4()
_FAKE_TOKEN = "fake-test-token"

# Patch target: _resolve_tenant lives in the registry.api.routers.mcp module.
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


def _make_annotation_ref(
    annotation_id: uuid.UUID | None = None,
    status: str = "open",
    warnings: list[dict] | None = None,
) -> AnnotationRef:
    return AnnotationRef(
        annotation_id=annotation_id or _ANN_ID,
        tenant_id=_TENANT_ID,
        capability_id=_CAP_ID,
        author_actor_id=_ACTOR_ID,
        author_tenant_id=_TENANT_ID,
        body="This is a test annotation body.",
        triage_note=None,
        category="feedback",
        status=status,
        version_target=None,
        created_at=_NOW,
        updated_at=_NOW,
        warnings=warnings,
    )


def _build_mcp(annotation_service: Any | None = None) -> Any:
    """Build a FastMCP server with mocked dependencies.

    Stubs out retrieval, catalog, session_factory, and workspace_service so
    the server can be instantiated without any live infrastructure. The
    annotation_service arg is passed through directly; callers inject an
    AsyncMock to control tool behavior per test.
    """
    clock = FakeClock(_NOW)
    retrieval = MagicMock()
    catalog = MagicMock()
    session_factory = MagicMock()
    workspace_service = MagicMock()

    mcp = create_catalog_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        clock=clock,
        annotation_service=annotation_service,
        workspace_service=workspace_service,
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
# 1. submit_annotation — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_annotation_happy_path() -> None:
    """submit_annotation returns an AnnotationRef-shape dict on success.

    Verifies annotation_id, status='open', and no warnings key.
    """
    ann_svc = MagicMock()
    ann_svc.create_annotation = AsyncMock(return_value=_make_annotation_ref())
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "submit_annotation",
            {
                "capability_id": str(_CAP_ID),
                "body": "This is a test annotation body.",
                "category": "feedback",
            },
        )

    payload = json.loads(raw)
    assert payload["annotation_id"] == str(_ANN_ID)
    assert payload["status"] == "open"
    assert "warnings" not in payload or payload["warnings"] is None


@pytest.mark.asyncio
async def test_submit_annotation_delegates_all_fields() -> None:
    """submit_annotation passes body, category, version_target to the service."""
    ann_svc = MagicMock()
    ann_svc.create_annotation = AsyncMock(return_value=_make_annotation_ref())
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "submit_annotation",
            {
                "capability_id": str(_CAP_ID),
                "body": "Detailed feedback here.",
                "category": "bug",
                "version_target": "v1.2.3",
            },
        )

    ann_svc.create_annotation.assert_awaited_once()
    call_kwargs = ann_svc.create_annotation.call_args.kwargs
    assert call_kwargs["body"] == "Detailed feedback here."
    assert call_kwargs["category"] == "bug"
    assert call_kwargs["version_target"] == "v1.2.3"


# ---------------------------------------------------------------------------
# 2. submit_annotation — PII block on body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_annotation_pii_block_body_raises_tool_error() -> None:
    """submit_annotation raises ToolError with PII-detected message on body block.

    The service raises HTTPException(422) with a dict detail containing
    code='pii_detected'. The tool translates this to a ToolError naming the
    field ('body') and the detected categories.
    """
    ann_svc = MagicMock()
    ann_svc.create_annotation = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail={
                "code": "pii_detected",
                "field": "annotation.body",
                "categories": ["email", "ssn"],
            },
        )
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "submit_annotation",
                {
                    "capability_id": str(_CAP_ID),
                    "body": "My email is test@example.com",
                    "category": "feedback",
                },
            )

    msg = str(exc_info.value)
    assert "PII detected" in msg
    assert "body" in msg
    # Both categories must appear in the message.
    assert "email" in msg
    assert "ssn" in msg


@pytest.mark.asyncio
async def test_submit_annotation_pii_block_message_format() -> None:
    """submit_annotation PII ToolError message starts with the canonical prefix."""
    ann_svc = MagicMock()
    ann_svc.create_annotation = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail={
                "code": "pii_detected",
                "field": "annotation.body",
                "categories": ["phone"],
            },
        )
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "submit_annotation",
                {
                    "capability_id": str(_CAP_ID),
                    "body": "Call me at 555-1234",
                    "category": "feedback",
                },
            )

    msg = str(exc_info.value)
    assert "Annotation rejected: PII detected in body" in msg, (
        f"Expected PII message, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# 3. submit_annotation — invalid category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_annotation_invalid_category_raises_tool_error() -> None:
    """submit_annotation raises ToolError with the canonical invalid-category message.

    The service raises HTTPException(422) with a plain string detail containing
    'Invalid category'. The tool reformats this into the canonical vocabulary
    error message.
    """
    bad_category = "rant"
    ann_svc = MagicMock()
    ann_svc.create_annotation = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail=(
                f"Invalid category {bad_category!r}. "
                "Must be one of: ['bug', 'doc_gap', 'feedback', 'question', 'suggestion']."
            ),
        )
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "submit_annotation",
                {
                    "capability_id": str(_CAP_ID),
                    "body": "Some feedback",
                    "category": bad_category,
                },
            )

    msg = str(exc_info.value)
    # The canonical message names the category value and the valid vocabulary.
    assert "Invalid category" in msg
    assert bad_category in msg
    assert "feedback" in msg
    assert "bug" in msg


# ---------------------------------------------------------------------------
# 4. submit_annotation — capability not visible (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_annotation_capability_not_visible_raises_tool_error() -> None:
    """submit_annotation raises ToolError 'Capability not visible or not found' on 403.

    The service raises HTTPException(403) when assert_visible fails. The tool
    maps this to the canonical not-visible message so the MCP caller gets
    an actionable error without learning the capability's existence.
    """
    ann_svc = MagicMock()
    ann_svc.create_annotation = AsyncMock(
        side_effect=HTTPException(
            status_code=403,
            detail="Visibility check failed.",
        )
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "submit_annotation",
                {
                    "capability_id": str(_CAP_ID),
                    "body": "Some feedback",
                    "category": "feedback",
                },
            )

    assert "Capability not visible or not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. list_my_annotations — filters to caller's tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_my_annotations_returns_caller_tenant_items() -> None:
    """list_my_annotations returns all items returned by the service author path.

    The service applies the author-path filter (author_tenant_id == ctx.tenant_id)
    so only the caller's own annotations are returned. This test pins that the
    tool forwards ctx to the service and returns the full set the service yields.
    """
    ann_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    refs = [_make_annotation_ref(annotation_id=aid) for aid in ann_ids]

    ann_svc = MagicMock()
    ann_svc.list_annotations = AsyncMock(return_value=(refs, None))
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "list_my_annotations",
            {"capability_id": str(_CAP_ID)},
        )

    payload = json.loads(raw)
    assert len(payload["items"]) == 3
    returned_ids = {item["annotation_id"] for item in payload["items"]}
    assert returned_ids == {str(aid) for aid in ann_ids}
    # All items belong to the caller's tenant.
    for item in payload["items"]:
        assert item["author_tenant_id"] == str(_TENANT_ID)


@pytest.mark.asyncio
async def test_list_my_annotations_passes_status_filter() -> None:
    """list_my_annotations forwards the status filter argument to the service."""
    ann_svc = MagicMock()
    ann_svc.list_annotations = AsyncMock(return_value=([], None))
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "list_my_annotations",
            {"capability_id": str(_CAP_ID), "status": "triaged"},
        )

    call_kwargs = ann_svc.list_annotations.call_args.kwargs
    assert call_kwargs["status"] == "triaged"


@pytest.mark.asyncio
async def test_list_my_annotations_passes_cursor_and_propagates_next_cursor() -> None:
    """list_my_annotations forwards cursor and propagates next_cursor in the response."""
    import base64

    next_cursor = base64.urlsafe_b64encode(
        b'{"t": "2026-01-01T00:00:00+00:00", "id": "' + str(uuid.uuid4()).encode() + b'"}'
    ).decode()

    ann_svc = MagicMock()
    ann_svc.list_annotations = AsyncMock(
        return_value=([_make_annotation_ref()], next_cursor)
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    input_cursor = "some-cursor-string"
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "list_my_annotations",
            {"capability_id": str(_CAP_ID), "cursor": input_cursor},
        )

    # Cursor was forwarded to the service.
    call_kwargs = ann_svc.list_annotations.call_args.kwargs
    assert call_kwargs["cursor"] == input_cursor

    # next_cursor was propagated in the response.
    payload = json.loads(raw)
    assert payload["next_cursor"] == next_cursor


@pytest.mark.asyncio
async def test_list_my_annotations_no_capability_id_returns_empty() -> None:
    """list_my_annotations returns an empty list when capability_id is not provided.

    The service's list_annotations is scoped to a single capability; without a
    capability_id the tool cannot issue a meaningful query and returns empty
    rather than scanning all capabilities.
    """
    ann_svc = MagicMock()
    ann_svc.list_annotations = AsyncMock(return_value=([], None))
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(mcp, "list_my_annotations", {})

    payload = json.loads(raw)
    assert payload["items"] == []
    assert payload["next_cursor"] is None
    # Service was not called — no capability to query against.
    ann_svc.list_annotations.assert_not_awaited()


# ---------------------------------------------------------------------------
# 8. triage_annotation — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_annotation_happy_path() -> None:
    """triage_annotation returns the updated annotation with new status."""
    updated_ref = _make_annotation_ref(status="triaged")
    ann_svc = MagicMock()
    ann_svc.triage_annotation = AsyncMock(return_value=updated_ref)
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "triage_annotation",
            {
                "annotation_id": str(_ANN_ID),
                "new_status": "triaged",
            },
        )

    payload = json.loads(raw)
    assert payload["status"] == "triaged"
    assert payload["annotation_id"] == str(_ANN_ID)


@pytest.mark.asyncio
async def test_triage_annotation_delegates_triage_note() -> None:
    """triage_annotation forwards triage_note to the service."""
    ann_svc = MagicMock()
    ann_svc.triage_annotation = AsyncMock(return_value=_make_annotation_ref(status="acknowledged"))
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "triage_annotation",
            {
                "annotation_id": str(_ANN_ID),
                "new_status": "acknowledged",
                "triage_note": "Provider reviewed and confirmed.",
            },
        )

    call_kwargs = ann_svc.triage_annotation.call_args.kwargs
    assert call_kwargs["triage_note"] == "Provider reviewed and confirmed."
    assert call_kwargs["new_status"] == "acknowledged"


@pytest.mark.asyncio
async def test_triage_annotation_delegates_version_target() -> None:
    """triage_annotation forwards version_target to the service."""
    ann_svc = MagicMock()
    ann_svc.triage_annotation = AsyncMock(return_value=_make_annotation_ref(status="triaged"))
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "triage_annotation",
            {
                "annotation_id": str(_ANN_ID),
                "new_status": "triaged",
                "version_target": "v1.0",
            },
        )

    call_kwargs = ann_svc.triage_annotation.call_args.kwargs
    assert call_kwargs["version_target"] == "v1.0"
    assert call_kwargs["new_status"] == "triaged"


# ---------------------------------------------------------------------------
# 9. triage_annotation — non-owner tenant (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_annotation_non_owner_raises_tool_error() -> None:
    """triage_annotation raises ToolError 'Capability not visible or not found' on 403.

    When a non-owner tenant calls triage_annotation, the service raises
    HTTPException(403). The tool maps 403 to the canonical not-visible message.
    """
    ann_svc = MagicMock()
    ann_svc.triage_annotation = AsyncMock(
        side_effect=HTTPException(
            status_code=403,
            detail=f"Tenant {_TENANT_ID} does not own the capability for annotation {_ANN_ID}.",
        )
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "triage_annotation",
                {
                    "annotation_id": str(_ANN_ID),
                    "new_status": "triaged",
                },
            )

    assert "Capability not visible or not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 10. triage_annotation — PII block on triage_note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_annotation_pii_block_triage_note_raises_tool_error() -> None:
    """triage_annotation raises ToolError with PII-detected message on triage_note block.

    The service raises HTTPException(422) with a dict detail containing
    code='pii_detected' and field='annotation.triage_note'. The tool normalises
    the field name to 'triage_note' and includes the detected categories.
    """
    ann_svc = MagicMock()
    ann_svc.triage_annotation = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail={
                "code": "pii_detected",
                "field": "annotation.triage_note",
                "categories": ["credit_card"],
            },
        )
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "triage_annotation",
                {
                    "annotation_id": str(_ANN_ID),
                    "new_status": "triaged",
                    "triage_note": "Card number is 4111-1111-1111-1111",
                },
            )

    msg = str(exc_info.value)
    assert "PII detected" in msg
    assert "triage_note" in msg
    assert "credit_card" in msg


# ---------------------------------------------------------------------------
# 11. triage_annotation — annotation not found (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_annotation_not_found_raises_tool_error() -> None:
    """triage_annotation raises ToolError 'Annotation not found' when the service returns 404."""
    missing_id = uuid.uuid4()
    ann_svc = MagicMock()
    ann_svc.triage_annotation = AsyncMock(
        side_effect=HTTPException(
            status_code=404,
            detail=f"Annotation {missing_id} not found.",
        )
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp,
                "triage_annotation",
                {
                    "annotation_id": str(missing_id),
                    "new_status": "triaged",
                },
            )

    assert "Annotation not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_annotation_tool_registered_when_service_provided() -> None:
    """submit_annotation, list_my_annotations, and triage_annotation are registered
    in the tool set when annotation_service is provided to create_catalog_mcp_server.
    """
    ann_svc = MagicMock()
    mcp = _build_mcp(annotation_service=ann_svc)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "submit_annotation" in names
    assert "list_my_annotations" in names
    assert "triage_annotation" in names


@pytest.mark.asyncio
async def test_submit_annotation_warns_field_in_response() -> None:
    """submit_annotation response includes warnings when the service returns them."""
    warnings = [{"field": "body", "categories": ["email"]}]
    ann_svc = MagicMock()
    ann_svc.create_annotation = AsyncMock(
        return_value=_make_annotation_ref(warnings=warnings)
    )
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "submit_annotation",
            {
                "capability_id": str(_CAP_ID),
                "body": "My email is user@example.com",
                "category": "feedback",
            },
        )

    payload = json.loads(raw)
    assert payload.get("warnings") is not None
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0]["field"] == "body"
    assert "email" in payload["warnings"][0]["categories"]


@pytest.mark.asyncio
async def test_triage_annotation_invalid_uuid_raises_tool_error() -> None:
    """triage_annotation raises ToolError when annotation_id is not a valid UUID."""
    ann_svc = MagicMock()
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="annotation_id must be a valid UUID"):
            await _call(
                mcp,
                "triage_annotation",
                {
                    "annotation_id": "not-a-uuid",
                    "new_status": "triaged",
                },
            )


@pytest.mark.asyncio
async def test_submit_annotation_invalid_capability_uuid_raises_tool_error() -> None:
    """submit_annotation raises ToolError when capability_id is not a valid UUID."""
    ann_svc = MagicMock()
    mcp = _build_mcp(annotation_service=ann_svc)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="capability_id must be a valid UUID"):
            await _call(
                mcp,
                "submit_annotation",
                {
                    "capability_id": "not-a-uuid",
                    "body": "Some feedback",
                    "category": "feedback",
                },
            )
