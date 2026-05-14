"""Unit tests for IncludeService and MCP get_capability with include=.

Coverage
--------
IncludeService:
  - expand_components returns EntityCollectionExpansion on happy path.
  - expand_depends_on returns EntityCollectionExpansion on happy path.
  - expand_components signals truncated=True and next URL when rows > cap.
  - expand_depends_on signals truncated=True and next URL when rows > cap.
  - expand_external_ids returns ExternalIdsExpansion on happy path.
  - expand_external_ids signals truncated=True when rows > cap.
  - expand_interface returns InterfaceExpansion on happy path.
  - expand_interface returns empty InterfaceExpansion when CatalogError raised.
  - expand_interface returns empty InterfaceExpansion when record is None.

MCP get_capability with include=:
  - include=components returns components sub-object.
  - include=interface returns interface sub-object.
  - include=components,interface returns both sub-objects.
  - include param absent → no sub-objects in result.
  - Unknown include value silently ignored (no error).

All DB and service interactions are mocked — no Postgres or Docker required.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.api.routers.mcp import _request_token, create_registry_mcp_server
from registry.api.schemas import (
    EntityCollectionExpansion,
    ExternalIdsExpansion,
    IncludedEntityItem,
    InterfaceExpansion,
)
from registry.exceptions import NotFoundError
from registry.service.includes import IncludeService
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()
_COMP_ID = uuid.uuid4()
_FAKE_TOKEN = "fake-token-xyz"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx() -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT_ID,
        actor_id=_ACTOR_ID,
        roles=["consumer"],
    )


def _make_included_entity(entity_id: uuid.UUID = _COMP_ID) -> IncludedEntityItem:
    return IncludedEntityItem(
        entity_id=entity_id,
        tenant_id=_TENANT_ID,
        entity_type="capability",
        name="button",
        external_id=None,
        is_active=True,
        created_at=_NOW,
        attributes={},
    )


# ---------------------------------------------------------------------------
# Helpers: build a mock session_factory that returns parameterised DB rows
# ---------------------------------------------------------------------------


def _make_session_factory(edge_dst_ids: list[uuid.UUID], entity_rows: list[Any], attr_rows: list[Any]) -> Any:
    """Build a session factory mock that returns edge/entity/attribute rows in order.

    The session's execute() is called in sequence:
      1st call → edge dst_ids
      2nd call → entity rows
      3rd call → attribute rows
    """
    call_count = 0

    class _FakeScalars:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def all(self) -> list[Any]:
            return self._rows

    class _FakeResult:
        def __init__(self, rows: list[Any], is_edge_query: bool = False) -> None:
            self._rows = rows
            self._is_edge_query = is_edge_query

        def all(self) -> list[Any]:
            # Edge query returns (dst_id,) tuples
            return [(r,) for r in self._rows]

        def scalars(self) -> _FakeScalars:
            return _FakeScalars(self._rows)

    async def _execute(*_args: Any, **_kw: Any) -> _FakeResult:
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            return _FakeResult(edge_dst_ids, is_edge_query=True)
        elif call_count == 1:
            call_count += 1
            return _FakeResult(entity_rows)
        else:
            call_count += 1
            return _FakeResult(attr_rows)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    session_factory = MagicMock()
    session_factory.return_value = session
    return session_factory


def _make_entity_orm(entity_id: uuid.UUID) -> MagicMock:
    e = MagicMock()
    e.entity_id = entity_id
    e.tenant_id = _TENANT_ID
    e.entity_type = "capability"
    e.name = "button"
    e.external_id = None
    e.is_active = True
    e.created_at = _NOW
    return e


def _make_visibility_svc(visible_ids: list[uuid.UUID]) -> Any:
    svc = MagicMock()
    svc.filter_entities = AsyncMock(return_value=visible_ids)
    return svc


# ---------------------------------------------------------------------------
# IncludeService — expand_components / expand_depends_on happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_components_happy_path() -> None:
    """expand_components returns EntityCollectionExpansion with one item."""
    ctx = _make_ctx()
    entity_orm = _make_entity_orm(_COMP_ID)
    session_factory = _make_session_factory([_COMP_ID], [entity_orm], [])
    visibility = _make_visibility_svc([_COMP_ID])

    interface_storage = MagicMock()

    svc = IncludeService(
        session_factory=session_factory,
        visibility=visibility,
        interface_storage=interface_storage,
    )

    result = await svc.expand_components(ctx, _ENTITY_ID, handle_for_next=str(_ENTITY_ID))

    assert isinstance(result, EntityCollectionExpansion)
    assert len(result.items) == 1
    assert result.items[0].entity_id == _COMP_ID
    assert result.truncated is False
    assert result.next is None


@pytest.mark.asyncio
async def test_expand_depends_on_happy_path() -> None:
    """expand_depends_on returns EntityCollectionExpansion with one item."""
    ctx = _make_ctx()
    entity_orm = _make_entity_orm(_COMP_ID)
    session_factory = _make_session_factory([_COMP_ID], [entity_orm], [])

    svc = IncludeService(
        session_factory=session_factory,
        visibility=_make_visibility_svc([_COMP_ID]),
        interface_storage=MagicMock(),
    )

    result = await svc.expand_depends_on(ctx, _ENTITY_ID, handle_for_next=str(_ENTITY_ID))

    assert isinstance(result, EntityCollectionExpansion)
    assert len(result.items) == 1
    assert result.truncated is False


# ---------------------------------------------------------------------------
# IncludeService — truncation behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_components_truncates_at_cap() -> None:
    """expand_components with cap=2 and 3 rows → truncated=True + next URL."""
    ctx = _make_ctx()
    cap = 2

    # The service fetches cap+1 = 3 rows. We provide 3 IDs.
    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    entity_orms = [_make_entity_orm(i) for i in ids[:cap]]

    call_count = 0

    class _FakeResult:
        def __init__(self, rows: list[Any], edge_query: bool = False) -> None:
            self._rows = rows
            self._edge_query = edge_query

        def all(self) -> list[Any]:
            return [(r,) for r in self._rows]

        def scalars(self) -> Any:
            class _S:
                def __init__(self, r: list[Any]) -> None:
                    self._r = r

                def all(self) -> list[Any]:
                    return self._r

            return _S(self._rows)

    async def _execute(*_args: Any, **_kw: Any) -> _FakeResult:
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            # Return all 3 IDs (cap+1) to trigger truncation detection.
            return _FakeResult(ids, edge_query=True)
        elif call_count == 1:
            call_count += 1
            # Only the first cap entities are fetched after truncation.
            return _FakeResult(entity_orms)
        else:
            call_count += 1
            return _FakeResult([])

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock()
    session_factory.return_value = session

    visibility = _make_visibility_svc(ids[:cap])

    svc = IncludeService(
        session_factory=session_factory,
        visibility=visibility,
        interface_storage=MagicMock(),
    )

    handle = "my-capability"
    result = await svc.expand_components(ctx, _ENTITY_ID, handle_for_next=handle, cap=cap)

    assert result.truncated is True
    assert result.next is not None
    assert handle in result.next
    assert len(result.items) == cap


@pytest.mark.asyncio
async def test_expand_depends_on_truncates_at_cap() -> None:
    """expand_depends_on with cap=1 and 2 rows → truncated=True + next URL."""
    ctx = _make_ctx()
    cap = 1

    ids = [uuid.uuid4(), uuid.uuid4()]
    entity_orms = [_make_entity_orm(ids[0])]

    call_count = 0

    class _FakeResult:
        def __init__(self, rows: list[Any], edge_query: bool = False) -> None:
            self._rows = rows

        def all(self) -> list[Any]:
            return [(r,) for r in self._rows]

        def scalars(self) -> Any:
            class _S:
                def __init__(self, r: list[Any]) -> None:
                    self._r = r

                def all(self) -> list[Any]:
                    return self._r

            return _S(self._rows)

    async def _execute(*_args: Any, **_kw: Any) -> _FakeResult:
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            return _FakeResult(ids)
        elif call_count == 1:
            call_count += 1
            return _FakeResult(entity_orms)
        else:
            call_count += 1
            return _FakeResult([])

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock()
    session_factory.return_value = session

    visibility = _make_visibility_svc([ids[0]])

    svc = IncludeService(
        session_factory=session_factory,
        visibility=visibility,
        interface_storage=MagicMock(),
    )

    result = await svc.expand_depends_on(ctx, _ENTITY_ID, handle_for_next="payment-api", cap=cap)

    assert result.truncated is True
    assert result.next is not None
    assert len(result.items) == cap


# ---------------------------------------------------------------------------
# IncludeService — expand_external_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_external_ids_happy_path() -> None:
    """expand_external_ids returns ExternalIdsExpansion with one item."""
    ctx = _make_ctx()

    row = ("npm", "@salt-ds/core", "https://npmjs.com", None)

    class _FakeResult:
        def all(self) -> list[Any]:
            return [row]

    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock()
    session_factory.return_value = session

    svc = IncludeService(
        session_factory=session_factory,
        visibility=MagicMock(),
        interface_storage=MagicMock(),
    )

    result = await svc.expand_external_ids(ctx, _ENTITY_ID)

    assert isinstance(result, ExternalIdsExpansion)
    assert len(result.items) == 1
    assert result.items[0].external_system_slug == "npm"
    assert result.items[0].external_id == "@salt-ds/core"
    assert result.truncated is False


@pytest.mark.asyncio
async def test_expand_external_ids_truncation() -> None:
    """expand_external_ids with cap=1 and 2 rows → truncated=True."""
    ctx = _make_ctx()

    rows = [("npm", "@a/b", None, None), ("github", "org/repo", None, None)]

    class _FakeResult:
        def all(self) -> list[Any]:
            return rows  # returns cap+1 = 2 rows when cap=1

    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock()
    session_factory.return_value = session

    svc = IncludeService(
        session_factory=session_factory,
        visibility=MagicMock(),
        interface_storage=MagicMock(),
    )

    result = await svc.expand_external_ids(ctx, _ENTITY_ID, cap=1)

    assert result.truncated is True
    assert len(result.items) == 1


# ---------------------------------------------------------------------------
# IncludeService — expand_interface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_interface_happy_path() -> None:
    """expand_interface returns InterfaceExpansion with surface populated."""
    from registry.types import InterfaceSurface  # noqa: PLC0415

    ctx = _make_ctx()

    surface = InterfaceSurface(
        fields=[{"name": "id", "type": "string", "required": True}],
        operations=[],
        events=[],
    )

    record = MagicMock()
    record.interface_canonical = surface
    record.interface_source = {"type": "object", "properties": {"id": {"type": "string"}}}
    record.interface_format = "json_schema"

    interface_storage = MagicMock()
    interface_storage.get_interface = AsyncMock(return_value=record)

    svc = IncludeService(
        session_factory=MagicMock(),
        visibility=MagicMock(),
        interface_storage=interface_storage,
    )

    result = await svc.expand_interface(ctx, _ENTITY_ID)

    assert isinstance(result, InterfaceExpansion)
    assert result.format == "json_schema"
    # InterfaceSurface has no version field — version comes from canonical dict.
    assert result.version is None
    assert result.surface is not None
    assert "fields" in result.surface


@pytest.mark.asyncio
async def test_expand_interface_catalog_error_returns_empty() -> None:
    """expand_interface returns all-None InterfaceExpansion when CatalogError raised."""
    ctx = _make_ctx()

    interface_storage = MagicMock()
    interface_storage.get_interface = AsyncMock(side_effect=NotFoundError("not found"))

    svc = IncludeService(
        session_factory=MagicMock(),
        visibility=MagicMock(),
        interface_storage=interface_storage,
    )

    result = await svc.expand_interface(ctx, _ENTITY_ID)

    assert isinstance(result, InterfaceExpansion)
    assert result.surface is None
    assert result.raw is None
    assert result.format is None
    assert result.version is None


@pytest.mark.asyncio
async def test_expand_interface_none_record_returns_empty() -> None:
    """expand_interface returns all-None InterfaceExpansion when interface_storage returns None."""
    ctx = _make_ctx()

    interface_storage = MagicMock()
    interface_storage.get_interface = AsyncMock(return_value=None)

    svc = IncludeService(
        session_factory=MagicMock(),
        visibility=MagicMock(),
        interface_storage=interface_storage,
    )

    result = await svc.expand_interface(ctx, _ENTITY_ID)

    assert result.surface is None
    assert result.raw is None


# ---------------------------------------------------------------------------
# MCP get_capability with include=
# ---------------------------------------------------------------------------


def _build_mcp_with_includes(
    includes: IncludeService | None = None,
) -> Any:
    """Return a FastMCP server wired with mocked services including IncludeService."""
    from registry.types import CapabilityRecord, EntityRef  # noqa: PLC0415

    clock = FakeClock(_NOW)

    retrieval = MagicMock()
    catalog = MagicMock()

    entity_ref = EntityRef(
        entity_id=_ENTITY_ID,
        tenant_id=_TENANT_ID,
        entity_type="capability",
        name="stub",
        external_id=None,
        is_active=True,
        created_at=_NOW,
    )

    # resolve_entity_handle returns a stub EntityRef.
    async def _resolve(ctx_arg: object, handle: str, **_kw: object) -> EntityRef:
        try:
            eid = uuid.UUID(handle)
        except ValueError:
            eid = _ENTITY_ID
        return EntityRef(
            entity_id=eid,
            tenant_id=_TENANT_ID,
            entity_type="capability",
            name="stub",
            external_id=None,
            is_active=True,
            created_at=_NOW,
        )

    catalog.resolve_entity_handle = AsyncMock(side_effect=_resolve)

    # get_full_capability returns a minimal real CapabilityRecord.
    record = CapabilityRecord(
        entity=entity_ref,
        attributes={},
        lifecycle="draft",
        facts=[],
        edges_out=[],
        edges_in=[],
    )

    catalog.get_full_capability = AsyncMock(return_value=record)

    session_factory = MagicMock()

    mcp = create_registry_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
        clock=clock,
        includes=includes,
    )
    return mcp, catalog


async def _mcp_call(mcp: Any, tool: str, args: dict[str, Any]) -> Any:
    """Set auth ContextVar and invoke the MCP tool, returning parsed JSON."""
    cv_tok = _request_token.set(_FAKE_TOKEN)
    try:
        with patch("registry.api.routers.mcp._resolve_tenant", new=AsyncMock(return_value=_make_ctx())):
            content_blocks, _ = await mcp.call_tool(tool, args)
        return json.loads(content_blocks[0].text)
    finally:
        _request_token.reset(cv_tok)


@pytest.mark.asyncio
async def test_mcp_get_capability_no_include() -> None:
    """get_capability without include= returns core fields but no sub-objects."""
    mcp, _ = _build_mcp_with_includes()

    result = await _mcp_call(mcp, "get_capability", {"entity_id": str(_ENTITY_ID)})

    assert "components" not in result
    assert "depends_on" not in result
    assert "external_ids" not in result
    assert "interface" not in result


@pytest.mark.asyncio
async def test_mcp_get_capability_include_components() -> None:
    """get_capability with include=components attaches components sub-object."""
    exp = EntityCollectionExpansion(
        items=[_make_included_entity()],
        truncated=False,
        next=None,
    )

    includes = MagicMock(spec=IncludeService)
    includes.expand_components = AsyncMock(return_value=exp)

    mcp, _ = _build_mcp_with_includes(includes=includes)

    result = await _mcp_call(mcp, "get_capability", {"entity_id": str(_ENTITY_ID), "include": "components"})

    assert "components" in result
    assert result["components"]["truncated"] is False
    assert len(result["components"]["items"]) == 1
    includes.expand_components.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_get_capability_include_interface() -> None:
    """get_capability with include=interface attaches interface sub-object."""
    exp = InterfaceExpansion(surface={"type": "object"}, raw=None, format="json_schema", version=None)

    includes = MagicMock(spec=IncludeService)
    includes.expand_interface = AsyncMock(return_value=exp)

    mcp, _ = _build_mcp_with_includes(includes=includes)

    result = await _mcp_call(mcp, "get_capability", {"entity_id": str(_ENTITY_ID), "include": "interface"})

    assert "interface" in result
    assert result["interface"]["format"] == "json_schema"
    includes.expand_interface.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_get_capability_include_multiple() -> None:
    """get_capability with include=components,interface returns both sub-objects."""
    comp_exp = EntityCollectionExpansion(items=[], truncated=False, next=None)
    iface_exp = InterfaceExpansion(surface=None, raw=None, format=None, version=None)

    includes = MagicMock(spec=IncludeService)
    includes.expand_components = AsyncMock(return_value=comp_exp)
    includes.expand_interface = AsyncMock(return_value=iface_exp)

    mcp, _ = _build_mcp_with_includes(includes=includes)

    result = await _mcp_call(mcp, "get_capability", {"entity_id": str(_ENTITY_ID), "include": "components,interface"})

    assert "components" in result
    assert "interface" in result
    includes.expand_components.assert_awaited_once()
    includes.expand_interface.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_get_capability_unknown_include_silently_ignored() -> None:
    """get_capability with an unknown include value does not error."""
    includes = MagicMock(spec=IncludeService)

    mcp, _ = _build_mcp_with_includes(includes=includes)

    # Should not raise ToolError — unknown values are silently ignored.
    result = await _mcp_call(mcp, "get_capability", {"entity_id": str(_ENTITY_ID), "include": "foobar"})

    # No sub-objects in result.
    assert "components" not in result
    assert "interface" not in result
