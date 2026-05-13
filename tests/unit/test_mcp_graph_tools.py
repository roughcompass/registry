"""Unit tests for MCP tools get_dependents and get_blast_radius.

Verifies:
  - Tool registration: both tools appear in list_tools with correct inputSchema.
  - get_dependents delegates to retrieval.get_reverse_traversal and returns
    JSON matching the REST TraversalResult shape byte-for-byte.
  - get_blast_radius delegates to retrieval.get_blast_radius and returns
    the same shape.
  - Validation errors (bad UUID, out-of-range depth, bad direction) raise
    ToolError — not HTTP exceptions (MCP protocol uses ToolError for auth and validation failures).
  - Missing auth token raises ToolError (inherited from existing auth shim).
  - Service CatalogError is mapped to ToolError (not leaked as exception).
  - edge_types list forwarded without transformation.
  - as_of ISO-8601 string parsed and forwarded as datetime to the service.

All DB and service interactions are mocked — no Postgres or Docker required.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.api.routers.mcp import _request_token, create_catalog_mcp_server
from registry.exceptions import NotFoundError, TenantIsolationError
from registry.types import (
    EdgeRef,
    EntityRef,
    FakeClock,
    TenantContext,
    TraversalResult,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()
_EDGE_ID = uuid.uuid4()
_DST_ENTITY_ID = uuid.uuid4()
_FAKE_TOKEN = "fake-token-abc"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_ctx() -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT_ID,
        actor_id=_ACTOR_ID,
        roles=["reader"],
    )


def _make_entity_ref(entity_id: uuid.UUID = _ENTITY_ID) -> EntityRef:
    return EntityRef(
        entity_id=entity_id,
        tenant_id=_TENANT_ID,
        entity_type="service",
        name="payment-service",
        external_id=None,
        is_active=True,
        created_at=_NOW,
    )


def _make_edge_ref() -> EdgeRef:
    return EdgeRef(
        edge_id=_EDGE_ID,
        tenant_id=_TENANT_ID,
        src_entity_id=_ENTITY_ID,
        rel="depends_on",
        dst_entity_id=_DST_ENTITY_ID,
        properties=None,
        t_valid_from=_NOW,
        t_valid_to=None,
        t_ingested_at=_NOW,
        t_invalidated_at=None,
    )


def _make_traversal_result(
    direction: str = "reverse",
    cache_hit: bool = False,
) -> TraversalResult:
    return TraversalResult(
        root_entity_id=_ENTITY_ID,
        depth=2,
        direction=direction,  # type: ignore[arg-type]
        as_of=None,
        nodes=[_make_entity_ref(_DST_ENTITY_ID)],
        edges=[_make_edge_ref()],
        version_satisfied={_EDGE_ID: True},
        cache_hit=cache_hit,
    )


# ---------------------------------------------------------------------------
# Fixture: MCP server wired to mocked service layer
# ---------------------------------------------------------------------------


def _build_mcp(
    traversal_result: TraversalResult | None = None,
    blast_radius_result: TraversalResult | None = None,
    reverse_side_effect: Exception | None = None,
    blast_side_effect: Exception | None = None,
) -> Any:
    """Return a FastMCP server with mocked retrieval and catalog services."""
    clock = FakeClock(_NOW)

    retrieval = MagicMock()
    retrieval.get_reverse_traversal = AsyncMock(
        return_value=traversal_result or _make_traversal_result(direction="reverse"),
        side_effect=reverse_side_effect,
    )
    retrieval.get_blast_radius = AsyncMock(
        return_value=blast_radius_result or _make_traversal_result(direction="reverse", cache_hit=True),
        side_effect=blast_side_effect,
    )

    catalog = MagicMock()
    # The MCP tools call catalog.resolve_entity_handle(ctx, entity_id)
    # before issuing the traversal. Mock with REAL slug validation so
    # invalid-format inputs raise ValidationError and valid-but-unknown
    # ones raise NotFoundError — both bubble up to the tool as a ToolError.
    from registry.exceptions import NotFoundError  # noqa: PLC0415
    from registry.service.slugs import validate_slug  # noqa: PLC0415
    from registry.types import EntityRef  # noqa: PLC0415

    async def _resolve(ctx_arg: object, handle: str, **_kw: object) -> EntityRef:
        import datetime as _dt  # noqa: PLC0415
        import uuid as _uuid  # noqa: PLC0415

        try:
            eid = _uuid.UUID(handle)
        except ValueError:
            validate_slug(handle, field="capability handle")
            # Valid slug but the mock has no DB — treat as found, return a stub.
            eid = _uuid.uuid4()
        return EntityRef(
            entity_id=eid,
            tenant_id=ctx_arg.tenant_id if hasattr(ctx_arg, "tenant_id") else _uuid.uuid4(),  # type: ignore[arg-type]
            entity_type="capability",
            name="stub",
            external_id=None,
            is_active=True,
            created_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
        )

    catalog.resolve_entity_handle = AsyncMock(side_effect=_resolve)
    _ = NotFoundError  # imported for downstream tests that build their own mocks
    session_factory = MagicMock()

    ctx = _make_ctx()

    mcp = create_catalog_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        clock=clock,
    )

    # Patch _resolve_tenant so no real DB is needed.
    mcp._retrieval = retrieval
    mcp._ctx = ctx
    return mcp, retrieval


async def _call(mcp: Any, tool: str, args: dict[str, Any]) -> Any:
    """Set the auth ContextVar and call the MCP tool in-process.

    call_tool returns (content_blocks, result_dict). We return the JSON string
    from the first TextContent block so callers can json.loads it directly.
    """
    cv_tok = _request_token.set(_FAKE_TOKEN)
    try:
        content_blocks, _result_meta = await mcp.call_tool(tool, args)
        return content_blocks[0].text
    finally:
        _request_token.reset(cv_tok)


# ---------------------------------------------------------------------------
# Helper: patch _resolve_tenant so unit tests bypass the DB entirely.
# This mirrors what the integration suite does with a real DB.
# ---------------------------------------------------------------------------

_PATCH_TARGET = "registry.api.routers.mcp._resolve_tenant"


# ---------------------------------------------------------------------------
# 1. Tool registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_registration_includes_graph_tools() -> None:
    """list_tools must include get_dependents and get_blast_radius."""
    mcp, _ = _build_mcp()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "get_dependents" in names, f"get_dependents missing from tool set: {names}"
    assert "get_blast_radius" in names, f"get_blast_radius missing from tool set: {names}"


@pytest.mark.asyncio
async def test_get_dependents_input_schema() -> None:
    """get_dependents inputSchema must declare entity_id, depth, edge_types, as_of."""
    mcp, _ = _build_mcp()
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_dependents")
    props = tool.inputSchema.get("properties", {})
    assert "entity_id" in props, "get_dependents must declare entity_id in inputSchema"
    assert "depth" in props, "get_dependents must declare depth in inputSchema"
    assert "edge_types" in props, "get_dependents must declare edge_types in inputSchema"
    assert "as_of" in props, "get_dependents must declare as_of in inputSchema"


@pytest.mark.asyncio
async def test_get_blast_radius_input_schema() -> None:
    """get_blast_radius inputSchema must declare entity_id, direction, depth, edge_types, as_of."""
    mcp, _ = _build_mcp()
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_blast_radius")
    props = tool.inputSchema.get("properties", {})
    assert "entity_id" in props, "get_blast_radius must declare entity_id"
    assert "direction" in props, "get_blast_radius must declare direction"
    assert "depth" in props, "get_blast_radius must declare depth"
    assert "edge_types" in props, "get_blast_radius must declare edge_types"
    assert "as_of" in props, "get_blast_radius must declare as_of"


@pytest.mark.asyncio
async def test_total_tool_count() -> None:
    """Server must expose exactly the documented set of tools."""
    mcp, _ = _build_mcp()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "whoami",
        "search_capabilities",
        "get_capability",
        "lookup_by_external_id",
        "get_dependencies",
        "list_capabilities",
        "get_dependents",
        "get_blast_radius",
    }
    assert names == expected, f"tool set mismatch: {names}"


# ---------------------------------------------------------------------------
# 2. get_dependents — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dependents_returns_traversal_result() -> None:
    """get_dependents returns JSON that matches the REST TraversalResult shape."""
    expected_result = _make_traversal_result(direction="reverse")
    mcp, retrieval = _build_mcp(traversal_result=expected_result)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(mcp, "get_dependents", {"entity_id": str(_ENTITY_ID), "depth": 2})

    # raw is a list of content blocks from FastMCP; extract text.
    payload = json.loads(raw)

    assert payload["root_entity_id"] == str(_ENTITY_ID)
    assert payload["direction"] == "reverse"
    assert payload["depth"] == 2
    assert payload["cache_hit"] is False
    assert len(payload["nodes"]) == 1
    assert len(payload["edges"]) == 1
    assert payload["edges"][0]["rel"] == "depends_on"
    # version_satisfied keys are stringified UUIDs.
    assert str(_EDGE_ID) in payload["version_satisfied"]
    assert payload["version_satisfied"][str(_EDGE_ID)] is True


@pytest.mark.asyncio
async def test_get_dependents_delegates_depth_and_edge_types() -> None:
    """get_dependents forwards depth and edge_types to get_reverse_traversal."""
    mcp, retrieval = _build_mcp()
    ctx = _make_ctx()
    edge_types_arg = ["depends_on", "requires"]

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "get_dependents",
            {
                "entity_id": str(_ENTITY_ID),
                "depth": 3,
                "edge_types": edge_types_arg,
            },
        )

    retrieval.get_reverse_traversal.assert_awaited_once()
    call_kwargs = retrieval.get_reverse_traversal.call_args.kwargs
    assert call_kwargs["depth"] == 3
    assert call_kwargs["edge_types"] == edge_types_arg


@pytest.mark.asyncio
async def test_get_dependents_forwards_as_of() -> None:
    """get_dependents parses the as_of ISO-8601 string and forwards it as datetime."""
    mcp, retrieval = _build_mcp()
    ctx = _make_ctx()
    as_of_str = "2026-01-01T00:00:00+00:00"

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "get_dependents",
            {"entity_id": str(_ENTITY_ID), "as_of": as_of_str},
        )

    call_kwargs = retrieval.get_reverse_traversal.call_args.kwargs
    expected_dt = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    assert call_kwargs["as_of"] == expected_dt


@pytest.mark.asyncio
async def test_get_dependents_null_edge_types_forwarded() -> None:
    """get_dependents passes edge_types=None when not provided."""
    mcp, retrieval = _build_mcp()
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(mcp, "get_dependents", {"entity_id": str(_ENTITY_ID)})

    call_kwargs = retrieval.get_reverse_traversal.call_args.kwargs
    assert call_kwargs["edge_types"] is None


# ---------------------------------------------------------------------------
# 3. get_blast_radius — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_blast_radius_returns_traversal_result() -> None:
    """get_blast_radius returns JSON that matches the REST TraversalResult shape."""
    expected_result = _make_traversal_result(direction="reverse", cache_hit=True)
    mcp, retrieval = _build_mcp(blast_radius_result=expected_result)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "get_blast_radius",
            {"entity_id": str(_ENTITY_ID), "direction": "reverse", "depth": 5},
        )

    payload = json.loads(raw)
    assert payload["root_entity_id"] == str(_ENTITY_ID)
    assert payload["direction"] == "reverse"
    assert payload["cache_hit"] is True
    assert len(payload["edges"]) == 1


@pytest.mark.asyncio
async def test_get_blast_radius_forward_direction() -> None:
    """get_blast_radius delegates direction='forward' to the service."""
    mcp, retrieval = _build_mcp(blast_radius_result=_make_traversal_result(direction="forward", cache_hit=False))
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "get_blast_radius",
            {"entity_id": str(_ENTITY_ID), "direction": "forward"},
        )

    call_kwargs = retrieval.get_blast_radius.call_args.kwargs
    assert call_kwargs["direction"] == "forward"


@pytest.mark.asyncio
async def test_get_blast_radius_delegates_all_params() -> None:
    """get_blast_radius forwards all parameters to retrieval.get_blast_radius."""
    mcp, retrieval = _build_mcp()
    ctx = _make_ctx()
    edge_types_arg = ["depends_on"]

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        await _call(
            mcp,
            "get_blast_radius",
            {
                "entity_id": str(_ENTITY_ID),
                "direction": "reverse",
                "depth": 4,
                "edge_types": edge_types_arg,
            },
        )

    call_kwargs = retrieval.get_blast_radius.call_args.kwargs
    assert call_kwargs["entity_id"] == _ENTITY_ID
    assert call_kwargs["direction"] == "reverse"
    assert call_kwargs["depth"] == 4
    assert call_kwargs["edge_types"] == edge_types_arg


# ---------------------------------------------------------------------------
# 4. Validation — ToolError on bad inputs (not HTTP exceptions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dependents_bad_handle_raises_tool_error() -> None:
    """get_dependents raises ToolError on a handle that's neither a UUID nor a valid slug.

    Slug-or-UUID acceptance now allows hyphen-separated names; the failing
    case is truly-malformed input (spaces, uppercase, underscores).
    """
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError):
            await _call(mcp, "get_dependents", {"entity_id": "Not A Slug"})


@pytest.mark.asyncio
async def test_get_dependents_depth_out_of_range_raises_tool_error() -> None:
    """get_dependents raises ToolError when depth < 1 or depth > 5."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="depth must be between 1 and 5"):
            await _call(mcp, "get_dependents", {"entity_id": str(_ENTITY_ID), "depth": 0})

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="depth must be between 1 and 5"):
            await _call(mcp, "get_dependents", {"entity_id": str(_ENTITY_ID), "depth": 6})


@pytest.mark.asyncio
async def test_get_blast_radius_bad_handle_raises_tool_error() -> None:
    """get_blast_radius raises ToolError on a handle that's neither a UUID nor a valid slug."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError):
            await _call(mcp, "get_blast_radius", {"entity_id": "Not A Slug"})


@pytest.mark.asyncio
async def test_get_blast_radius_bad_direction_raises_tool_error() -> None:
    """get_blast_radius raises ToolError on an invalid direction string."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="direction must be"):
            await _call(
                mcp,
                "get_blast_radius",
                {"entity_id": str(_ENTITY_ID), "direction": "sideways"},
            )


@pytest.mark.asyncio
async def test_get_blast_radius_depth_out_of_range_raises_tool_error() -> None:
    """get_blast_radius raises ToolError when depth is out of 1–5 range."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="depth must be between 1 and 5"):
            await _call(
                mcp,
                "get_blast_radius",
                {"entity_id": str(_ENTITY_ID), "depth": 10},
            )


@pytest.mark.asyncio
async def test_get_dependents_naive_as_of_raises_tool_error() -> None:
    """A naive (timezone-unaware) as_of raises ToolError — not a 422."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="timezone-aware"):
            await _call(
                mcp,
                "get_dependents",
                {"entity_id": str(_ENTITY_ID), "as_of": "2026-01-01T00:00:00"},
            )


# ---------------------------------------------------------------------------
# 5. Service error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dependents_not_found_maps_to_tool_error() -> None:
    """NotFoundError from the service layer maps to ToolError with 'not found' message."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp(reverse_side_effect=NotFoundError("entity not found"))
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="not found"):
            await _call(mcp, "get_dependents", {"entity_id": str(_ENTITY_ID)})


@pytest.mark.asyncio
async def test_get_blast_radius_not_found_maps_to_tool_error() -> None:
    """NotFoundError from get_blast_radius maps to ToolError."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp(blast_side_effect=NotFoundError("entity not found"))
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError, match="not found"):
            await _call(mcp, "get_blast_radius", {"entity_id": str(_ENTITY_ID)})


@pytest.mark.asyncio
async def test_get_dependents_tenant_isolation_maps_to_not_found() -> None:
    """TenantIsolationError maps to generic 'not found' ToolError (avoids oracle attacks)."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp(reverse_side_effect=TenantIsolationError("wrong tenant"))
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        with pytest.raises(ToolError) as exc_info:
            await _call(mcp, "get_dependents", {"entity_id": str(_ENTITY_ID)})
    # Must say "not found", not "wrong tenant" (oracle protection).
    assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 6. Auth — missing token raises ToolError (inherited auth shim, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dependents_missing_token_raises_tool_error() -> None:
    """Calling get_dependents without a Bearer token raises ToolError."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()

    # Do NOT set _request_token — default is empty string.
    with pytest.raises(ToolError, match="missing bearer token"):
        # No patch on _resolve_tenant: let the real shim reject the empty token.
        await mcp.call_tool("get_dependents", {"entity_id": str(_ENTITY_ID)})


@pytest.mark.asyncio
async def test_get_blast_radius_missing_token_raises_tool_error() -> None:
    """Calling get_blast_radius without a Bearer token raises ToolError."""
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, _ = _build_mcp()

    with pytest.raises(ToolError, match="missing bearer token"):
        await mcp.call_tool("get_blast_radius", {"entity_id": str(_ENTITY_ID)})


# ---------------------------------------------------------------------------
# 7. REST parity: output shape matches serialized TraversalResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dependents_output_matches_rest_shape() -> None:
    """get_dependents JSON shape is byte-compatible with REST TraversalResultResponse.

    This guards the CAP-§2.3 invariant: MCP tools mirror REST byte-for-byte.
    """
    from registry.api.routers.mcp import _serialize

    result = _make_traversal_result(direction="reverse")
    expected = json.loads(json.dumps(_serialize(result)))

    mcp, _ = _build_mcp(traversal_result=result)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(mcp, "get_dependents", {"entity_id": str(_ENTITY_ID)})

    payload = json.loads(raw)
    assert payload == expected, "MCP output does not match REST serialization"


@pytest.mark.asyncio
async def test_get_blast_radius_output_matches_rest_shape() -> None:
    """get_blast_radius JSON shape is byte-compatible with REST TraversalResultResponse."""
    from registry.api.routers.mcp import _serialize

    result = _make_traversal_result(direction="forward", cache_hit=True)
    expected = json.loads(json.dumps(_serialize(result)))

    mcp, _ = _build_mcp(blast_radius_result=result)
    ctx = _make_ctx()

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=ctx)):
        raw = await _call(
            mcp,
            "get_blast_radius",
            {"entity_id": str(_ENTITY_ID), "direction": "forward"},
        )

    payload = json.loads(raw)
    assert payload == expected, "MCP output does not match REST serialization"
