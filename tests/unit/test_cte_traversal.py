"""Unit tests for RetrievalService._traverse_cte.

Contract under test
-------------------
``_traverse_cte(session, tenant_id, root_entity_id, direction, depth,
               edge_types, temporal_filter, as_of)``

* ``direction='reverse'``: seed clause ``WHERE dst_entity_id = :root_id``
  — returns nodes that point *to* root.
* ``direction='forward'``: seed clause ``WHERE src_entity_id = :root_id``
  — returns nodes that root points *to* (existing dependency semantics).
* Depth is internally capped at 5 (_MAX_DEPTH).
* ``edge_types=None`` → all vocab minus ``concept_of``, ``operation_of``,
  ``instance_of`` (``_DEFAULT_TRAVERSAL_EDGE_TYPES``).
* Returns ``list[dict]`` with keys ``member_entity_id``, ``depth``,
  ``edge_path``, ``edge_rels``.
* No version predicate evaluation; no visibility filtering (T08 / T05).

All tests mock the database session — no Postgres or Docker required.

Fixtures
--------
Five-capability chain A → B → C → D → E (edges with rel='depends_on').
Forward from A:  B(1), C(2), D(3), E(4).
Reverse from E:  D(1), C(2), B(3), A(4).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from registry.config import Settings
from registry.service.retrieval import (
    _ALL_VOCAB_RELS,
    _DEFAULT_TRAVERSAL_EDGE_TYPES,
    _MAX_DEPTH,
    _TRAVERSAL_EXCLUDED_RELS,
    RetrievalService,
)
from registry.types import FakeClock, TemporalFilter

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()

# Five-capability chain entities.
_A, _B, _C, _D, _E = (uuid.uuid4() for _ in range(5))

# Edge IDs for A→B, B→C, C→D, D→E.
_EDGE_AB, _EDGE_BC, _EDGE_CD, _EDGE_DE = (uuid.uuid4() for _ in range(4))

# Mapping of (src, dst) → edge_id for the chain.
_CHAIN_EDGES: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {
    (_A, _B): _EDGE_AB,
    (_B, _C): _EDGE_BC,
    (_C, _D): _EDGE_CD,
    (_D, _E): _EDGE_DE,
}


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
    )


def _stub_embedder() -> MagicMock:
    emb = MagicMock()
    emb.model_version = "stub-v1"
    emb.encode = MagicMock(side_effect=lambda texts: np.ones((len(texts), 4), dtype=np.float32))
    return emb


def _make_service() -> RetrievalService:
    """Build a RetrievalService whose session_factory must never be called directly.

    Tests that exercise _traverse_cte pass a pre-built mock session explicitly.
    """
    factory = MagicMock(side_effect=AssertionError("session factory must not be used"))
    clock = FakeClock(_NOW)
    return RetrievalService(
        session_factory=factory,
        clock=clock,
        embedder=_stub_embedder(),
        settings=_settings(),
    )


def _tf(as_of: datetime.datetime | None = None) -> TemporalFilter:
    return TemporalFilter(as_of=as_of)


# ---------------------------------------------------------------------------
# Session mock helpers
# ---------------------------------------------------------------------------

_Row = dict[str, Any]


def _make_session_returning(rows: list[_Row]) -> AsyncMock:
    """Return a mock AsyncSession whose execute() yields *rows* as MappingResult."""
    mappings_mock = MagicMock()
    mappings_mock.all.return_value = rows
    result_mock = MagicMock()
    result_mock.mappings.return_value = mappings_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    return session


def _chain_row(
    src: uuid.UUID,
    dst: uuid.UUID,
    direction: str,
    depth: int,
    edge_path: list[uuid.UUID],
    edge_rels: list[str],
    rel: str = "depends_on",
) -> _Row:
    """Build one result row as the DB would return it from the CTE query.

    The CTE query uses ``DISTINCT ON (member_entity_id)`` and projects only
    ``member_entity_id``, ``depth``, ``edge_path``, and ``edge_rels``.
    """
    member_entity_id = dst if direction == "forward" else src
    return {
        "member_entity_id": member_entity_id,
        "depth": depth,
        "edge_path": edge_path,
        "edge_rels": edge_rels,
    }


# ---------------------------------------------------------------------------
# Synthetic chain data
# ---------------------------------------------------------------------------


def _forward_rows() -> list[_Row]:
    """Simulate the DB result for forward traversal from A in chain A→B→C→D→E."""
    return [
        _chain_row(_A, _B, "forward", 1, [_EDGE_AB], ["depends_on"]),
        _chain_row(_B, _C, "forward", 2, [_EDGE_AB, _EDGE_BC], ["depends_on", "depends_on"]),
        _chain_row(_C, _D, "forward", 3, [_EDGE_AB, _EDGE_BC, _EDGE_CD], ["depends_on"] * 3),
        _chain_row(
            _D,
            _E,
            "forward",
            4,
            [_EDGE_AB, _EDGE_BC, _EDGE_CD, _EDGE_DE],
            ["depends_on"] * 4,
        ),
    ]


def _reverse_rows() -> list[_Row]:
    """Simulate the DB result for reverse traversal from E in chain A→B→C→D→E.

    Reverse: D(depth=1), C(depth=2), B(depth=3), A(depth=4).
    """
    return [
        _chain_row(_D, _E, "reverse", 1, [_EDGE_DE], ["depends_on"]),
        _chain_row(_C, _E, "reverse", 2, [_EDGE_DE, _EDGE_CD], ["depends_on", "depends_on"]),
        _chain_row(_B, _E, "reverse", 3, [_EDGE_DE, _EDGE_CD, _EDGE_BC], ["depends_on"] * 3),
        _chain_row(
            _A,
            _E,
            "reverse",
            4,
            [_EDGE_DE, _EDGE_CD, _EDGE_BC, _EDGE_AB],
            ["depends_on"] * 4,
        ),
    ]


# ---------------------------------------------------------------------------
# Default edge-type set tests
# ---------------------------------------------------------------------------


class TestDefaultTraversalEdgeTypes:
    """_DEFAULT_TRAVERSAL_EDGE_TYPES excludes structural-typing edge rels."""

    def test_excluded_rels_absent(self) -> None:
        for rel in _TRAVERSAL_EXCLUDED_RELS:
            assert (
                rel not in _DEFAULT_TRAVERSAL_EDGE_TYPES
            ), f"'{rel}' should be excluded from default traversal edge types"

    def test_dependency_rels_present(self) -> None:
        expected = {
            "depends_on",
            "integrates_with",
            "event_source",
            "replaced_by",
            "requires",
            "conflicts_with",
            "composes",
            "provides_to",
        }
        for rel in expected:
            assert rel in _DEFAULT_TRAVERSAL_EDGE_TYPES, f"'{rel}' should be in default traversal edge types"

    def test_all_vocab_rels_accounted_for(self) -> None:
        """Every vocab rel is either in the default set or in the excluded set."""
        for rel in _ALL_VOCAB_RELS:
            in_default = rel in _DEFAULT_TRAVERSAL_EDGE_TYPES
            in_excluded = rel in _TRAVERSAL_EXCLUDED_RELS
            assert in_default ^ in_excluded, f"'{rel}' must be in exactly one of default or excluded sets"


# ---------------------------------------------------------------------------
# Forward traversal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_traversal_returns_four_members() -> None:
    """Forward from A in A→B→C→D→E returns B,C,D,E at depths 1,2,3,4."""
    svc = _make_service()
    session = _make_session_returning(_forward_rows())

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    member_ids = [r["member_entity_id"] for r in rows]
    assert len(rows) == 4
    assert _B in member_ids
    assert _C in member_ids
    assert _D in member_ids
    assert _E in member_ids
    assert _A not in member_ids


@pytest.mark.asyncio
async def test_forward_traversal_depths_are_correct() -> None:
    """Forward from A: B at depth 1, C at 2, D at 3, E at 4."""
    svc = _make_service()
    session = _make_session_returning(_forward_rows())

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    depth_by_member = {r["member_entity_id"]: r["depth"] for r in rows}
    assert depth_by_member[_B] == 1
    assert depth_by_member[_C] == 2
    assert depth_by_member[_D] == 3
    assert depth_by_member[_E] == 4


@pytest.mark.asyncio
async def test_forward_traversal_edge_path_grows_per_hop() -> None:
    """Edge path length equals depth for each row in the forward direction."""
    svc = _make_service()
    session = _make_session_returning(_forward_rows())

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    for row in rows:
        assert (
            len(row["edge_path"]) == row["depth"]
        ), f"edge_path length {len(row['edge_path'])} != depth {row['depth']}"
        assert (
            len(row["edge_rels"]) == row["depth"]
        ), f"edge_rels length {len(row['edge_rels'])} != depth {row['depth']}"


# ---------------------------------------------------------------------------
# Reverse traversal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_traversal_five_cap_chain() -> None:
    """Reverse from E in A→B→C→D→E returns D,C,B,A at depths 1,2,3,4."""
    svc = _make_service()
    session = _make_session_returning(_reverse_rows())

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_E,
        direction="reverse",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    assert len(rows) == 4, f"expected 4 reverse members, got {len(rows)}"
    member_ids = {r["member_entity_id"] for r in rows}
    assert _D in member_ids
    assert _C in member_ids
    assert _B in member_ids
    assert _A in member_ids
    assert _E not in member_ids


@pytest.mark.asyncio
async def test_reverse_traversal_depths_are_correct() -> None:
    """Reverse from E: D at depth 1, C at 2, B at 3, A at 4."""
    svc = _make_service()
    session = _make_session_returning(_reverse_rows())

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_E,
        direction="reverse",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    depth_by_member = {r["member_entity_id"]: r["depth"] for r in rows}
    assert depth_by_member[_D] == 1
    assert depth_by_member[_C] == 2
    assert depth_by_member[_B] == 3
    assert depth_by_member[_A] == 4


@pytest.mark.asyncio
async def test_reverse_traversal_root_not_in_result() -> None:
    """The root entity must never appear in the traversal result."""
    svc = _make_service()
    # Inject rows that include the root_entity_id to verify the filter works.
    rows_with_root = _reverse_rows() + [
        {
            "member_entity_id": _E,  # root — must be dropped
            "depth": 0,
            "edge_path": [],
            "edge_rels": [],
        }
    ]
    session = _make_session_returning(rows_with_root)

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_E,
        direction="reverse",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    member_ids = {r["member_entity_id"] for r in rows}
    assert _E not in member_ids, "root entity must not appear in traversal result"


# ---------------------------------------------------------------------------
# Depth capping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_depth_capped_at_max_depth() -> None:
    """Caller-requested depth > 5 is silently capped; the SQL receives max_depth=5."""
    svc = _make_service()

    captured_params: list[dict[str, Any]] = []

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=99,  # requested > _MAX_DEPTH
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    assert len(captured_params) == 1
    assert (
        captured_params[0]["max_depth"] == _MAX_DEPTH
    ), f"expected max_depth={_MAX_DEPTH}, got {captured_params[0].get('max_depth')}"


@pytest.mark.asyncio
async def test_depth_at_max_is_preserved() -> None:
    """Caller-requested depth exactly at 5 is passed through unchanged."""
    svc = _make_service()
    captured_params: list[dict[str, Any]] = []

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    assert captured_params[0]["max_depth"] == 5


# ---------------------------------------------------------------------------
# Edge type filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_edge_types_passed_to_query() -> None:
    """When edge_types is provided, only those types are included in the SQL params."""
    svc = _make_service()
    captured_params: list[dict[str, Any]] = []

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    custom_types = ["requires", "composes"]
    await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=2,
        edge_types=custom_types,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    assert len(captured_params) == 1
    assert set(captured_params[0]["edge_types"]) == set(
        custom_types
    ), f"expected edge_types={custom_types}, got {captured_params[0].get('edge_types')}"


@pytest.mark.asyncio
async def test_none_edge_types_uses_default_set() -> None:
    """edge_types=None resolves to _DEFAULT_TRAVERSAL_EDGE_TYPES in the query params."""
    svc = _make_service()
    captured_params: list[dict[str, Any]] = []

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="reverse",
        depth=3,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    assert len(captured_params) == 1
    actual = set(captured_params[0]["edge_types"])
    expected = set(_DEFAULT_TRAVERSAL_EDGE_TYPES)
    assert actual == expected, f"default edge_types mismatch: got {sorted(actual)}, expected {sorted(expected)}"


@pytest.mark.asyncio
async def test_default_edge_types_exclude_structural_rels() -> None:
    """Structural rels (concept_of, operation_of, instance_of) are never in the default set."""
    svc = _make_service()
    captured_params: list[dict[str, Any]] = []

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="reverse",
        depth=3,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    passed_types = set(captured_params[0]["edge_types"])
    for excluded in _TRAVERSAL_EXCLUDED_RELS:
        assert excluded not in passed_types, f"'{excluded}' must not be in default traversal edge_types"


# ---------------------------------------------------------------------------
# Invalid direction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_direction_raises_value_error() -> None:
    """Unknown direction raises ValueError before any query is issued."""
    svc = _make_service()
    session = _make_session_returning([])

    with pytest.raises(ValueError, match="direction must be"):
        await svc._traverse_cte(
            session=session,
            tenant_id=_TENANT_ID,
            root_entity_id=_A,
            direction="diagonal",
            depth=2,
            edge_types=None,
            temporal_filter=_tf(),
            as_of=_NOW,
        )


# ---------------------------------------------------------------------------
# Return shape validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_rows_have_required_keys() -> None:
    """Every row in the result must have member_entity_id, depth, edge_path, edge_rels."""
    svc = _make_service()
    session = _make_session_returning(_reverse_rows())

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_E,
        direction="reverse",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    required_keys = {"member_entity_id", "depth", "edge_path", "edge_rels"}
    for i, row in enumerate(rows):
        assert required_keys.issubset(row.keys()), f"row {i} missing keys: {required_keys - set(row.keys())}"


@pytest.mark.asyncio
async def test_edge_path_is_list_of_uuids() -> None:
    """edge_path values must be lists (converted from PG array)."""
    svc = _make_service()
    session = _make_session_returning(_reverse_rows())

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_E,
        direction="reverse",
        depth=5,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    for row in rows:
        assert isinstance(row["edge_path"], list), f"edge_path must be a list, got {type(row['edge_path'])}"
        assert isinstance(row["edge_rels"], list), f"edge_rels must be a list, got {type(row['edge_rels'])}"


# ---------------------------------------------------------------------------
# Empty result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_graph_returns_empty_list() -> None:
    """No edges from root → empty result, no exception."""
    svc = _make_service()
    session = _make_session_returning([])

    rows = await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=3,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    assert rows == []


# ---------------------------------------------------------------------------
# Temporal filter — as_of is passed through to query params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_as_of_temporal_filter_passes_correct_params() -> None:
    """When temporal_filter.as_of is set, the query receives tf_valid_from param."""
    svc = _make_service()
    captured_params: list[dict[str, Any]] = []
    as_of_dt = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="forward",
        depth=3,
        edge_types=None,
        temporal_filter=_tf(as_of=as_of_dt),
        as_of=as_of_dt,
    )

    assert len(captured_params) == 1
    # Time-travel filter should include tf_valid_from
    assert "tf_valid_from" in captured_params[0], "as_of filter must produce tf_valid_from param"
    assert captured_params[0]["tf_valid_from"] == as_of_dt


@pytest.mark.asyncio
async def test_current_truth_temporal_filter_passes_tf_now() -> None:
    """When as_of is None (current truth), the query receives tf_now param."""
    svc = _make_service()
    captured_params: list[dict[str, Any]] = []

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    await svc._traverse_cte(
        session=session,
        tenant_id=_TENANT_ID,
        root_entity_id=_A,
        direction="reverse",
        depth=3,
        edge_types=None,
        temporal_filter=_tf(as_of=None),
        as_of=_NOW,
    )

    assert len(captured_params) == 1
    assert "tf_now" in captured_params[0], "current-truth filter must produce tf_now param"


# ---------------------------------------------------------------------------
# Tenant ID is passed through correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_id_in_query_params() -> None:
    """The tenant_id argument must appear in the SQL params as 'tid'."""
    svc = _make_service()
    captured_params: list[dict[str, Any]] = []
    other_tenant = uuid.uuid4()

    async def _capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params is not None:
            captured_params.append(dict(params))
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result = MagicMock()
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _capture_execute

    await svc._traverse_cte(
        session=session,
        tenant_id=other_tenant,
        root_entity_id=_A,
        direction="forward",
        depth=2,
        edge_types=None,
        temporal_filter=_tf(),
        as_of=_NOW,
    )

    assert (
        captured_params[0]["tid"] == other_tenant
    ), f"expected tid={other_tenant}, got {captured_params[0].get('tid')}"
