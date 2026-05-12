"""Unit tests for ProjectionService.

All SQL is mocked at the ``session.execute`` boundary via an
SQL-string-keyed router so each test can return canned rows per query
shape.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.api.cursor import decode_cursor, encode_cursor
from registry.service.projections import (
    Projection,
    ProjectionService,
    _clamp_page_size,
)
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

_OWN_TENANT = uuid.uuid4()
_PROVIDER_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_OWN_TENANT, actor_id=_ACTOR, roles=["consumer"])


def _async_noop_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _adoption_row(cap_id: uuid.UUID, adoption_id: uuid.UUID | None = None) -> MagicMock:
    """Build a mock adoption_events row with the columns the helper reads."""
    row = MagicMock()
    row.provider_capability_id = cap_id
    row.adoption_id = adoption_id or uuid.uuid4()
    row.t_valid_from = _NOW
    return row


def _make_router(
    *,
    own_entity_count: int = 0,
    own_entities: list[dict] | None = None,
    internal_edges: list[dict] | None = None,
    outgoing_dep_edges: list[dict] | None = None,
    provides_outgoing: list[dict] | None = None,
    provides_inbound: list[dict] | None = None,
    adopted_cap_ids: list[uuid.UUID] | None = None,
    # When set, the adoption_events mock returns this many extra rows so the
    # helper detects has_more = True and emits a next_cursor for adopted caps.
    adopted_extra_rows: int = 0,
    by_id_entities: list[dict] | None = None,
):
    """Build an AsyncMock execute() that routes SELECTs by SQL keywords."""

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(stmt).split())  # collapse whitespace
        result = MagicMock()

        if "COUNT(*) FROM entities" in sql:
            result.scalar = MagicMock(return_value=own_entity_count)
            return result

        if "FROM entities" in sql and "ORDER BY" in sql and "LIMIT" in sql:
            # _fetch_own_entities_keyset
            result.mappings.return_value.all.return_value = own_entities or []
            return result

        if "FROM entities" in sql and "entity_id = ANY" in sql:
            # _fetch_entities_by_ids_visible — only runs after visibility filter
            result.mappings.return_value.all.return_value = by_id_entities or []
            return result

        if "FROM edges" in sql and "rel = ANY" in sql and "depends_on" in str(params):
            # outgoing dep edges
            result.mappings.return_value.all.return_value = outgoing_dep_edges or []
            return result

        if "FROM edges" in sql and "rel = ANY" in sql:
            # internal composition edges
            result.mappings.return_value.all.return_value = internal_edges or []
            return result

        if "FROM edges" in sql and "rel = 'provides_to'" in sql and "tenant_id = :tid" in sql:
            # provider's outgoing provides_to
            result.mappings.return_value.all.return_value = provides_outgoing or []
            return result

        if "FROM edges" in sql and "rel = 'provides_to'" in sql:
            # cross-tenant provides_to for adopted caps
            result.mappings.return_value.all.return_value = provides_inbound or []
            return result

        if "FROM adoption_events" in sql:
            # Return exactly the rows requested (limit inferred from :lim param).
            # adopted_extra_rows > 0 simulates has_more (SQL returns limit+1 rows).
            base_rows = [_adoption_row(cid) for cid in (adopted_cap_ids or [])]
            extra = [_adoption_row(uuid.uuid4()) for _ in range(adopted_extra_rows)]
            result.all = MagicMock(return_value=base_rows + extra)
            return result

        result.mappings.return_value.all.return_value = []
        result.scalar = MagicMock(return_value=0)
        result.all = MagicMock(return_value=[])
        return result

    session = MagicMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    return factory


def _make_visibility(visible_for: list[uuid.UUID] | None = None) -> MagicMock:
    vis = MagicMock()
    vis.filter_entities = AsyncMock(return_value=visible_for or [])
    return vis


def _entity_row(eid: uuid.UUID, tenant_id: uuid.UUID, name: str = "x") -> dict:
    return {
        "entity_id": eid,
        "tenant_id": tenant_id,
        "entity_type": "capability",
        "name": name,
        "external_id": None,
        "is_active": True,
        "created_at": _NOW,
    }


def _edge_row(eid: uuid.UUID, tenant_id: uuid.UUID, src: uuid.UUID, rel: str, dst: uuid.UUID) -> dict:
    return {
        "edge_id": eid,
        "tenant_id": tenant_id,
        "src_entity_id": src,
        "rel": rel,
        "dst_entity_id": dst,
        "properties": None,
        "t_valid_from": _NOW,
        "t_valid_to": None,
        "t_ingested_at": _NOW,
        "t_invalidated_at": None,
    }


# ---------------------------------------------------------------------------
# Page-size clamp helper
# ---------------------------------------------------------------------------


def test_clamp_page_size_defaults_small_values() -> None:
    assert _clamp_page_size(0) == 100
    assert _clamp_page_size(-10) == 100


def test_clamp_page_size_caps_oversized() -> None:
    assert _clamp_page_size(9999) == 500


def test_clamp_page_size_passthrough_valid() -> None:
    assert _clamp_page_size(50) == 50
    assert _clamp_page_size(500) == 500


# ---------------------------------------------------------------------------
# Provider projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_projection_includes_own_entities_and_provides_to() -> None:
    cap_id = uuid.uuid4()
    own_ent = _entity_row(cap_id, _OWN_TENANT)
    edge_id = uuid.uuid4()
    provides_edge = _edge_row(edge_id, _OWN_TENANT, cap_id, "provides_to", cap_id)

    factory = _make_router(
        own_entity_count=1,
        own_entities=[own_ent],
        provides_outgoing=[provides_edge],
    )
    svc = ProjectionService(factory, FakeClock(_NOW), _make_visibility())

    proj = await svc.get_provider_projection(_ctx(), page_size=10)
    assert isinstance(proj, Projection)
    assert [n.entity_id for n in proj.nodes] == [cap_id]
    assert any(e.rel == "provides_to" for e in proj.edges)


@pytest.mark.asyncio
async def test_provider_projection_no_more_pages_when_result_fits() -> None:
    """next_cursor is None when the result fits in one page."""
    cap_id = uuid.uuid4()
    factory = _make_router(own_entity_count=1, own_entities=[_entity_row(cap_id, _OWN_TENANT)])
    svc = ProjectionService(factory, FakeClock(_NOW), _make_visibility())

    proj = await svc.get_provider_projection(_ctx(), page_size=50)
    assert proj.next_cursor is None


@pytest.mark.asyncio
async def test_provider_projection_emits_cursor_when_more_rows() -> None:
    """When the service returns page_size+1 rows the extra is trimmed and a cursor is set."""
    page_size = 2
    # Return 3 rows so has_more triggers; mock returns page_size+1.
    rows = [_entity_row(uuid.uuid4(), _OWN_TENANT) for _ in range(page_size + 1)]
    factory = _make_router(own_entity_count=10, own_entities=rows)
    svc = ProjectionService(factory, FakeClock(_NOW), _make_visibility())

    proj = await svc.get_provider_projection(_ctx(), page_size=page_size)
    # Only page_size nodes returned, not page_size+1.
    assert len(proj.nodes) == page_size
    assert proj.next_cursor is not None
    # next_cursor must be a decodable cursor payload.
    decoded = decode_cursor(encode_cursor(proj.next_cursor))
    assert "ts" in decoded and "id" in decoded


@pytest.mark.asyncio
async def test_provider_projection_empty_result() -> None:
    """Empty result returns empty nodes and no cursor."""
    factory = _make_router(own_entity_count=0, own_entities=[])
    svc = ProjectionService(factory, FakeClock(_NOW), _make_visibility())

    proj = await svc.get_provider_projection(_ctx(), page_size=20)
    assert proj.nodes == []
    assert proj.edges == []
    assert proj.next_cursor is None


@pytest.mark.asyncio
async def test_provider_projection_cursor_accepted() -> None:
    """A cursor dict from a previous page is accepted without error."""
    cap_id = uuid.uuid4()
    factory = _make_router(own_entity_count=1, own_entities=[_entity_row(cap_id, _OWN_TENANT)])
    svc = ProjectionService(factory, FakeClock(_NOW), _make_visibility())

    cursor = {"ts": _NOW.isoformat(), "id": str(uuid.uuid4())}
    proj = await svc.get_provider_projection(_ctx(), cursor=cursor, page_size=10)
    assert isinstance(proj, Projection)


# ---------------------------------------------------------------------------
# Consumer projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_projection_includes_adopted_provider_cap() -> None:
    own_cap = uuid.uuid4()
    adopted_cap = uuid.uuid4()
    factory = _make_router(
        own_entity_count=1,
        own_entities=[_entity_row(own_cap, _OWN_TENANT)],
        adopted_cap_ids=[adopted_cap],
        by_id_entities=[_entity_row(adopted_cap, _PROVIDER_TENANT, "provider-cap")],
        provides_inbound=[_edge_row(uuid.uuid4(), _PROVIDER_TENANT, adopted_cap, "provides_to", adopted_cap)],
    )
    vis = _make_visibility(visible_for=[adopted_cap])

    svc = ProjectionService(factory, FakeClock(_NOW), vis)
    proj = await svc.get_consumer_projection(_ctx(), page_size=10)

    node_ids = {n.entity_id for n in proj.nodes}
    assert own_cap in node_ids
    assert adopted_cap in node_ids
    # provides_to from adopted cap appears in edges.
    assert any(e.rel == "provides_to" and e.src_entity_id == adopted_cap for e in proj.edges)


@pytest.mark.asyncio
async def test_consumer_projection_filters_invisible_adopted_caps() -> None:
    """If VisibilityService rejects an adopted cap, it must NOT appear in nodes."""
    own_cap = uuid.uuid4()
    adopted_invisible = uuid.uuid4()
    factory = _make_router(
        own_entity_count=1,
        own_entities=[_entity_row(own_cap, _OWN_TENANT)],
        adopted_cap_ids=[adopted_invisible],
        by_id_entities=[],  # visibility rejects → no rows returned
    )
    vis = _make_visibility(visible_for=[])  # nothing visible

    svc = ProjectionService(factory, FakeClock(_NOW), vis)
    proj = await svc.get_consumer_projection(_ctx(), page_size=10)

    node_ids = {n.entity_id for n in proj.nodes}
    assert own_cap in node_ids
    assert adopted_invisible not in node_ids


@pytest.mark.asyncio
async def test_consumer_projection_no_cursor_on_single_page() -> None:
    """next_cursor is None when own entities fit in one page."""
    own_cap = uuid.uuid4()
    factory = _make_router(
        own_entity_count=1,
        own_entities=[_entity_row(own_cap, _OWN_TENANT)],
        adopted_cap_ids=[],
    )
    svc = ProjectionService(factory, FakeClock(_NOW), _make_visibility())
    proj = await svc.get_consumer_projection(_ctx(), page_size=50)
    assert proj.next_cursor is None


# ---------------------------------------------------------------------------
# Consumer projection — adopted-cap SQL pagination (CPR-T15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_projection_adopted_caps_sql_limited_to_page() -> None:
    """With N adopted caps exceeding page_size, only page_size items are returned.

    The mock simulates ``_fetch_adopted_provider_caps`` returning limit+1 rows
    (has_more=True). The helper trims to limit; no Python slicing occurs.
    """
    page_size = 3
    # No own entities — all page slots go to adopted caps.
    adopted_ids = [uuid.uuid4() for _ in range(page_size)]
    factory = _make_router(
        own_entities=[],
        # adopted_extra_rows=1 makes the mock return limit+1 so the helper
        # detects has_more and emits an adopted-cap next_cursor.
        adopted_cap_ids=adopted_ids,
        adopted_extra_rows=1,
        by_id_entities=[_entity_row(cid, _PROVIDER_TENANT) for cid in adopted_ids],
    )
    vis = _make_visibility(visible_for=adopted_ids)
    svc = ProjectionService(factory, FakeClock(_NOW), vis)

    proj = await svc.get_consumer_projection(_ctx(), page_size=page_size)

    # Exactly page_size adopted-cap nodes returned — no extras.
    assert len(proj.nodes) == page_size
    # next_cursor is populated because adopted_extra_rows=1 triggers has_more.
    assert proj.next_cursor is not None
    # Adopted-cap cursor keys are embedded in the combined next_cursor.
    assert "adp_ts" in proj.next_cursor
    assert "adp_id" in proj.next_cursor


@pytest.mark.asyncio
async def test_consumer_projection_next_cursor_set_when_adopted_caps_overflow() -> None:
    """next_cursor is populated when the SQL helper reports more adopted rows."""
    page_size = 2
    adopted_ids = [uuid.uuid4() for _ in range(page_size)]
    factory = _make_router(
        own_entities=[],
        adopted_cap_ids=adopted_ids,
        adopted_extra_rows=1,  # signals has_more inside _fetch_adopted_provider_caps
        by_id_entities=[_entity_row(cid, _PROVIDER_TENANT) for cid in adopted_ids],
    )
    vis = _make_visibility(visible_for=adopted_ids)
    svc = ProjectionService(factory, FakeClock(_NOW), vis)

    proj = await svc.get_consumer_projection(_ctx(), page_size=page_size)

    assert proj.next_cursor is not None


@pytest.mark.asyncio
async def test_consumer_projection_following_adopted_cursor_uses_keyset() -> None:
    """Following the cursor from page 1 passes adp_ts/adp_id to the SQL helper.

    This test verifies that the combined cursor round-trips through
    encode_cursor/decode_cursor and the extracted adopted-cap sub-cursor is
    non-empty, which is what drives the SQL keyset predicate on the next call.
    """
    page_size = 2
    adopted_ids = [uuid.uuid4() for _ in range(page_size)]
    factory = _make_router(
        own_entities=[],
        adopted_cap_ids=adopted_ids,
        adopted_extra_rows=1,
        by_id_entities=[_entity_row(cid, _PROVIDER_TENANT) for cid in adopted_ids],
    )
    vis = _make_visibility(visible_for=adopted_ids)
    svc = ProjectionService(factory, FakeClock(_NOW), vis)

    # Page 1 — capture next_cursor.
    proj1 = await svc.get_consumer_projection(_ctx(), page_size=page_size)
    assert proj1.next_cursor is not None

    # Encode → decode round-trip (as the router does).
    encoded = encode_cursor(proj1.next_cursor)
    decoded = decode_cursor(encoded)

    # The decoded cursor must carry adopted-cap position keys.
    assert "adp_ts" in decoded
    assert "adp_id" in decoded

    # Page 2 — pass the decoded cursor back. The mock returns no extra rows,
    # so next_cursor on page 2 is None (end of list).
    factory2 = _make_router(
        own_entities=[],
        adopted_cap_ids=[uuid.uuid4()],  # one more adopted cap on page 2
        adopted_extra_rows=0,
        by_id_entities=[_entity_row(uuid.uuid4(), _PROVIDER_TENANT)],
    )
    vis2 = _make_visibility(visible_for=[uuid.uuid4()])
    svc2 = ProjectionService(factory2, FakeClock(_NOW), vis2)

    proj2 = await svc2.get_consumer_projection(_ctx(), cursor=decoded, page_size=page_size)
    # No overflow on page 2 → next_cursor for adopted caps is None.
    # (own next_cursor is also None since own_entities is empty.)
    assert proj2.next_cursor is None
