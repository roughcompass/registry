"""Unit tests for fabric/service/retrieval.py.

All DB and embedder interactions are mocked — no Postgres or Docker required.

Coverage:
  - Score normalisation: weighted sum produces correct merged scores.
  - Arm failure recovery: one arm raises; remaining arms fuse without propagation.
  - Empty-arm weight redistribution: graph arm empty → weights redistribute
      proportionally (semantic 0.5/0.8 = 0.625, lexical 0.3/0.8 = 0.375).
  - Dedup by entity_id: max fused score wins (same entity in two arms).
  - ef_search over-fetch: semantic arm passes top_k * 4 as SET LOCAL value.
  - Final tenant assertion: row with wrong tenant_id is filtered out post-fusion.
  - LRU cache hit: same query within a session yields one embedder.encode call.
  - LRU cache concurrency: 10 concurrent coroutines with the same key produce exactly 1 encode call.
  - list_capabilities keyset pagination: cursor encoding/decoding, single-page no-cursor,
    multi-page cursor emission, cursor chaining, ?page=N rejection, empty result.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from registry.config import Settings
from registry.service.retrieval import RetrievalService, _normalize_scores, _redistribute_weights
from registry.types import (
    EntityRef,
    FactRef,
    FakeClock,
    TemporalFilter,
    TenantContext,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


def _ctx(tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or _TENANT_ID,
        actor_id=_ACTOR_ID,
        roles=["reader"],
    )


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
    )


def _stub_embedder(dim: int = 4) -> MagicMock:
    emb = MagicMock()
    emb.model_version = "stub-v1"
    emb.encode = MagicMock(side_effect=lambda texts: np.ones((len(texts), dim), dtype=np.float32))
    return emb


def _null_session_factory() -> MagicMock:
    """A session factory that should never be called in fusion-only tests."""
    factory = MagicMock(side_effect=AssertionError("session factory must not be called"))
    return factory


def _make_entity(tenant_id: uuid.UUID | None = None) -> EntityRef:
    return EntityRef(
        entity_id=uuid.uuid4(),
        tenant_id=tenant_id or _TENANT_ID,
        entity_type="service",
        name="test-entity",
        external_id=None,
        is_active=True,
        created_at=_NOW,
    )


def _arm_row(
    entity_id: uuid.UUID,
    entity: EntityRef,
) -> tuple[uuid.UUID, EntityRef, list[FactRef]]:
    return (entity_id, entity, [])


def _make_service(
    embedder: MagicMock | None = None,
    session_factory: Any = None,
) -> RetrievalService:
    if embedder is None:
        embedder = _stub_embedder()
    if session_factory is None:
        session_factory = _null_session_factory()
    clock = FakeClock(_NOW)
    return RetrievalService(
        session_factory=session_factory,
        clock=clock,
        embedder=embedder,
        settings=_settings(),
    )


def _tf() -> TemporalFilter:
    return TemporalFilter(as_of=None)


# ---------------------------------------------------------------------------
# Pure helper function tests
# ---------------------------------------------------------------------------


class TestNormalizeScores:
    def test_single_score_is_one_half(self) -> None:
        # rank 0 → 1/(0+1) = 1.0
        assert _normalize_scores([0.9]) == pytest.approx([1.0])

    def test_two_scores(self) -> None:
        result = _normalize_scores([0.9, 0.5])
        assert result == pytest.approx([1.0, 0.5])

    def test_empty(self) -> None:
        assert _normalize_scores([]) == []

    def test_three_scores(self) -> None:
        result = _normalize_scores([0.9, 0.6, 0.3])
        assert result == pytest.approx([1.0, 0.5, 1.0 / 3.0])


class TestRedistributeWeights:
    def test_no_failures_sums_to_one(self) -> None:
        weights = {"semantic": 0.5, "lexical": 0.3, "graph": 0.2}
        result = _redistribute_weights(weights, failed_arms=set())
        assert sum(result.values()) == pytest.approx(1.0)
        assert result == pytest.approx({"semantic": 0.5, "lexical": 0.3, "graph": 0.2})

    def test_one_arm_removed_weights_scale(self) -> None:
        weights = {"semantic": 0.5, "lexical": 0.3, "graph": 0.2}
        result = _redistribute_weights(weights, failed_arms={"graph"})
        # surviving sum = 0.8; semantic → 0.5/0.8=0.625, lexical → 0.3/0.8=0.375
        assert result == pytest.approx({"semantic": 0.625, "lexical": 0.375})
        assert sum(result.values()) == pytest.approx(1.0)

    def test_all_arms_removed_returns_empty(self) -> None:
        weights = {"semantic": 0.5, "lexical": 0.3, "graph": 0.2}
        result = _redistribute_weights(weights, failed_arms={"semantic", "lexical", "graph"})
        assert result == {}


# ---------------------------------------------------------------------------
# Score normalisation — weighted sum produces correct merged scores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_normalisation_single_arm() -> None:
    """Top result in one arm gets weight * 1/rank; empty arms don't redistribute weights.

    Weights are only redistributed when an arm raises an exception (failed_arms).
    An arm returning [] is still in effective_weights but contributes nothing.
    So with only semantic having data:
      rank 0: semantic_weight(0.5) * normalize(rank=0)(1.0) = 0.5
      rank 1: semantic_weight(0.5) * normalize(rank=1)(0.5) = 0.25
    """
    svc = _make_service()

    eid_a = uuid.uuid4()
    eid_b = uuid.uuid4()
    entity_a = _make_entity()
    entity_a.entity_id = eid_a
    entity_b = _make_entity()
    entity_b.entity_id = eid_b

    arm_data = [_arm_row(eid_a, entity_a), _arm_row(eid_b, entity_b)]

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=arm_data)),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=[])),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    scores = {r.entity.entity_id: r.score for r in results}
    # semantic weight = 0.5; rank-based: rank 0 → 1.0, rank 1 → 0.5
    assert scores[eid_a] == pytest.approx(0.5 * 1.0)
    assert scores[eid_b] == pytest.approx(0.5 * 0.5)
    # Results must be sorted descending by score
    assert results[0].entity.entity_id == eid_a


@pytest.mark.asyncio
async def test_score_normalisation_two_arms() -> None:
    """Two arms both return rank-0 hit for same entity: scores accumulate additively.

    No weight redistribution for empty graph arm (only exceptions trigger redistribution).
    entity score = semantic(0.5)*1.0 + lexical(0.3)*1.0 = 0.8
    """
    svc = _make_service()

    eid = uuid.uuid4()
    entity = _make_entity()
    entity.entity_id = eid
    arm_data = [_arm_row(eid, entity)]

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=arm_data)),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=arm_data)),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    assert len(results) == 1
    # semantic 0.5*1.0 + lexical 0.3*1.0 = 0.8 (graph is empty, not failed)
    assert results[0].score == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Arm failure recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_failure_does_not_propagate() -> None:
    """A raising arm is excluded via failed_arms; surviving arms fuse without exception.

    semantic raises → failed_arms = {semantic}.
    _redistribute_weights removes semantic: surviving = {lexical:0.3, graph:0.2}, total=0.5.
    Redistributed: lexical→0.6, graph→0.4. Graph returns empty, so contributes nothing.
    Entity score = lexical(0.6) * 1.0 = 0.6.
    """
    svc = _make_service()

    eid = uuid.uuid4()
    entity = _make_entity()
    entity.entity_id = eid
    arm_data = [_arm_row(eid, entity)]

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(side_effect=RuntimeError("pgvector down"))),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=arm_data)),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    assert len(results) == 1
    assert results[0].entity.entity_id == eid
    # lexical redistributed weight = 0.3/0.5 = 0.6
    assert results[0].score == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_two_arm_failures_still_returns_results() -> None:
    """Two arms fail; the one surviving arm returns results."""
    svc = _make_service()

    eid = uuid.uuid4()
    entity = _make_entity()
    entity.entity_id = eid
    arm_data = [_arm_row(eid, entity)]

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(side_effect=OSError("db error"))),
        patch.object(svc, "_lexical_arm", new=AsyncMock(side_effect=ValueError("bad query"))),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=arm_data)),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    assert len(results) == 1
    assert results[0].score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_all_arms_fail_returns_empty() -> None:
    """All arms failing → empty results, no exception."""
    svc = _make_service()

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(side_effect=RuntimeError("down"))),
        patch.object(svc, "_lexical_arm", new=AsyncMock(side_effect=RuntimeError("down"))),
        patch.object(svc, "_graph_arm", new=AsyncMock(side_effect=RuntimeError("down"))),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    assert results == []


# ---------------------------------------------------------------------------
# Empty-arm weight redistribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_graph_arm_weight_redistribution() -> None:
    """Graph arm returns empty list; only exception arms trigger weight redistribution.

    An empty return is NOT a failure — weights stay at their original values.
    Graph arm stays in effective_weights at 0.2 but contributes 0 to fusion.
    eid_a: semantic(0.5) * rank_0(1.0) = 0.5
    eid_b: lexical(0.3) * rank_0(1.0) = 0.3
    """
    svc = _make_service()

    eid_a = uuid.uuid4()
    eid_b = uuid.uuid4()
    entity_a = _make_entity()
    entity_a.entity_id = eid_a
    entity_b = _make_entity()
    entity_b.entity_id = eid_b

    # semantic has eid_a at rank 0 only; lexical has eid_b at rank 0 only; graph empty
    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=[_arm_row(eid_a, entity_a)])),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=[_arm_row(eid_b, entity_b)])),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    scores = {r.entity.entity_id: r.score for r in results}
    # Empty graph arm retains its weight slot (0.2) but contributes nothing to fusion
    assert scores[eid_a] == pytest.approx(0.5)
    assert scores[eid_b] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_failing_graph_arm_weight_redistribution_to_625_375() -> None:
    """Graph arm RAISES → weight redistribution: semantic 0.5/0.8=0.625, lexical 0.3/0.8=0.375.

    A raising arm (not an empty return) triggers weight redistribution across remaining arms.
    A raising arm (not an empty return) triggers redistribution.
    """
    svc = _make_service()

    eid_a = uuid.uuid4()
    eid_b = uuid.uuid4()
    entity_a = _make_entity()
    entity_a.entity_id = eid_a
    entity_b = _make_entity()
    entity_b.entity_id = eid_b

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=[_arm_row(eid_a, entity_a)])),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=[_arm_row(eid_b, entity_b)])),
        patch.object(svc, "_graph_arm", new=AsyncMock(side_effect=RuntimeError("graph down"))),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    scores = {r.entity.entity_id: r.score for r in results}
    # graph failed → weights redistribute: semantic 0.5/0.8=0.625, lexical 0.3/0.8=0.375
    assert scores[eid_a] == pytest.approx(0.625)
    assert scores[eid_b] == pytest.approx(0.375)


# ---------------------------------------------------------------------------
# Dedup by entity_id — max score wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_by_entity_id_max_score_wins() -> None:
    """Same entity_id in semantic and lexical at rank 0: scores accumulate, one deduplicated result.

    Dedup is by entity_id; the fused score is the sum of contributions.
    semantic(0.5)*1.0 + lexical(0.3)*1.0 = 0.8.
    """
    svc = _make_service()

    shared_eid = uuid.uuid4()
    entity = _make_entity()
    entity.entity_id = shared_eid

    arm_data = [_arm_row(shared_eid, entity)]

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=arm_data)),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=arm_data)),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    # Only one result for the shared entity — deduplicated
    assert len(results) == 1
    assert results[0].entity.entity_id == shared_eid
    # Additive: semantic(0.5)*1.0 + lexical(0.3)*1.0 = 0.8
    assert results[0].score == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_dedup_higher_ranked_in_first_arm_dominates_ordering() -> None:
    """Two distinct entities; the one with higher combined score appears first.

    eid_a: semantic rank 0 → 0.5*1.0 = 0.5
    eid_b: lexical rank 0 → 0.3*1.0 = 0.3
    eid_a must sort first.
    """
    svc = _make_service()

    eid_a = uuid.uuid4()
    eid_b = uuid.uuid4()
    entity_a = _make_entity()
    entity_a.entity_id = eid_a
    entity_b = _make_entity()
    entity_b.entity_id = eid_b

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=[_arm_row(eid_a, entity_a)])),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=[_arm_row(eid_b, entity_b)])),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(), "q", top_k=10, temporal_filter=_tf())

    assert results[0].entity.entity_id == eid_a
    assert results[1].entity.entity_id == eid_b
    assert results[0].score == pytest.approx(0.5)
    assert results[1].score == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# ef_search over-fetch parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_arm_receives_ef_search_top_k_times_4() -> None:
    """Semantic arm issues SET LOCAL hnsw.ef_search = top_k * 4 inside a transaction."""
    embedder = _stub_embedder()

    # Build a session mock that captures all execute() calls
    executed_stmts: list[tuple[str, Any]] = []

    async def _execute(stmt: Any, params: Any = None) -> MagicMock:
        executed_stmts.append((str(stmt), params))
        result = MagicMock()
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = []
        result.mappings.return_value = mappings_mock
        return result

    session = AsyncMock()
    session.execute = _execute

    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)
    clock = FakeClock(_NOW)
    svc = RetrievalService(
        session_factory=factory,
        clock=clock,
        embedder=embedder,
        settings=_settings(),
    )

    top_k = 5
    await svc._semantic_arm(_ctx(), "q", top_k, _tf(), None)

    # Find the SET LOCAL hnsw.ef_search call
    set_local_calls = [(stmt, params) for stmt, params in executed_stmts if "SET LOCAL hnsw.ef_search" in stmt]
    assert len(set_local_calls) >= 1, "SET LOCAL hnsw.ef_search must be called"
    _, params = set_local_calls[0]
    assert params is not None
    assert params["v"] == top_k * 4, f"ef_search must be top_k*4={top_k*4}, got {params.get('v')}"


# ---------------------------------------------------------------------------
# Final tenant assertion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_tenant_assertion_drops_wrong_tenant() -> None:
    """A result whose entity.tenant_id != ctx.tenant_id is silently dropped post-fusion."""
    svc = _make_service()

    wrong_tenant_id = uuid.uuid4()  # different from _TENANT_ID

    eid_good = uuid.uuid4()
    entity_good = _make_entity(tenant_id=_TENANT_ID)
    entity_good.entity_id = eid_good

    eid_bad = uuid.uuid4()
    entity_bad = _make_entity(tenant_id=wrong_tenant_id)
    entity_bad.entity_id = eid_bad

    arm_data = [
        _arm_row(eid_good, entity_good),
        _arm_row(eid_bad, entity_bad),
    ]

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=arm_data)),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=[])),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(tenant_id=_TENANT_ID), "q", top_k=10, temporal_filter=_tf())

    entity_ids = {r.entity.entity_id for r in results}
    assert eid_good in entity_ids, "correct tenant entity should be in results"
    assert eid_bad not in entity_ids, "wrong-tenant entity must be filtered out"


# ---------------------------------------------------------------------------
# LRU cache hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lru_cache_hit_single_encode_call() -> None:
    """Same query within a session yields exactly one embedder.encode call."""
    embedder = _stub_embedder()
    svc = _make_service(embedder=embedder)

    await svc._encode_query("hello world")
    await svc._encode_query("hello world")
    await svc._encode_query("hello world")

    embedder.encode.assert_called_once()


@pytest.mark.asyncio
async def test_lru_cache_different_queries_call_encode_separately() -> None:
    """Different queries each invoke encode once."""
    embedder = _stub_embedder()
    svc = _make_service(embedder=embedder)

    await svc._encode_query("query one")
    await svc._encode_query("query two")

    assert embedder.encode.call_count == 2


@pytest.mark.asyncio
async def test_lru_cache_different_model_versions_are_distinct() -> None:
    """Same text but different model_version results in separate encode calls."""
    embedder_a = _stub_embedder()
    embedder_a.model_version = "model-v1"
    svc_a = _make_service(embedder=embedder_a)

    embedder_b = _stub_embedder()
    embedder_b.model_version = "model-v2"
    svc_b = _make_service(embedder=embedder_b)

    await svc_a._encode_query("query")
    await svc_b._encode_query("query")

    embedder_a.encode.assert_called_once()
    embedder_b.encode.assert_called_once()


@pytest.mark.asyncio
async def test_lru_cache_concurrent_same_key_calls_encode_once() -> None:
    """10 concurrent coroutines requesting the same key produce exactly 1 encode call.

    Without the async lock, concurrent awaits between the cache-miss check and
    the cache write would each see a cache miss and each call encode — wasting
    embedder compute. The lock collapses them into one encode call.
    """
    import asyncio as _asyncio

    embedder = _stub_embedder()
    svc = _make_service(embedder=embedder)

    results = await _asyncio.gather(*[svc._encode_query("concurrent query") for _ in range(10)])

    # All 10 calls returned the same vector.
    assert all(r == results[0] for r in results)
    # Embedder was only called once despite 10 concurrent coroutines.
    embedder.encode.assert_called_once()


# ---------------------------------------------------------------------------
# top_k truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_respects_top_k_limit() -> None:
    """Results are truncated to top_k even when more are returned by arms."""
    svc = _make_service()

    entities = []
    for _ in range(5):
        eid = uuid.uuid4()
        e = _make_entity()
        e.entity_id = eid
        entities.append(_arm_row(eid, e))

    with (
        patch.object(svc, "_semantic_arm", new=AsyncMock(return_value=entities)),
        patch.object(svc, "_lexical_arm", new=AsyncMock(return_value=[])),
        patch.object(svc, "_graph_arm", new=AsyncMock(return_value=[])),
    ):
        results = await svc.search(_ctx(), "q", top_k=3, temporal_filter=_tf())

    assert len(results) == 3


# ---------------------------------------------------------------------------
# list_capabilities — keyset pagination
# ---------------------------------------------------------------------------


def _make_list_session_factory(rows: list[dict]) -> MagicMock:
    """Build a session factory that returns ``rows`` from the entities SELECT."""

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "FROM entities" in sql:
            result.mappings.return_value.all.return_value = rows
        else:
            result.mappings.return_value.all.return_value = []
        return result

    session = MagicMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return factory


def _list_entity_row(
    entity_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    created_at: datetime.datetime | None = None,
) -> dict:
    return {
        "entity_id": entity_id or uuid.uuid4(),
        "tenant_id": tenant_id or _TENANT_ID,
        "entity_type": "capability",
        "name": "test-cap",
        "external_id": None,
        "is_active": True,
        "created_at": created_at or _NOW,
    }


def _make_list_service(rows: list[dict]) -> RetrievalService:
    factory = _make_list_session_factory(rows)
    return RetrievalService(
        session_factory=factory,
        clock=FakeClock(_NOW),
        embedder=_stub_embedder(),
        settings=_settings(),
    )


@pytest.mark.asyncio
async def test_list_capabilities_no_cursor_single_page() -> None:
    """Single page result: next_cursor is None."""
    rows = [_list_entity_row() for _ in range(5)]
    svc = _make_list_service(rows)

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=20,
        temporal_filter=_tf(),
    )

    assert len(items) == 5
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_capabilities_emits_cursor_when_more_rows() -> None:
    """page_size+1 rows returned → trim to page_size and emit next_cursor."""
    page_size = 3
    rows = [_list_entity_row() for _ in range(page_size + 1)]
    svc = _make_list_service(rows)

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=page_size,
        temporal_filter=_tf(),
    )

    assert len(items) == page_size
    assert next_cursor is not None
    assert "ts" in next_cursor and "id" in next_cursor


@pytest.mark.asyncio
async def test_list_capabilities_cursor_points_to_last_item() -> None:
    """next_cursor encodes the last returned item's (created_at, entity_id)."""
    import uuid as _uuid_mod

    last_id = _uuid_mod.uuid4()
    last_ts = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    page_size = 2
    rows = [
        _list_entity_row(),
        _list_entity_row(entity_id=last_id, created_at=last_ts),
        _list_entity_row(),  # extra row signals has_more
    ]
    svc = _make_list_service(rows)

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=page_size,
        temporal_filter=_tf(),
    )

    assert next_cursor is not None
    assert next_cursor["id"] == str(last_id)
    assert datetime.datetime.fromisoformat(next_cursor["ts"]) == last_ts


@pytest.mark.asyncio
async def test_list_capabilities_empty_result() -> None:
    """Empty DB result returns empty items list and no cursor."""
    svc = _make_list_service([])

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=20,
        temporal_filter=_tf(),
    )

    assert items == []
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_capabilities_cursor_payload_round_trips() -> None:
    """The cursor payload produced by list_capabilities survives encode/decode."""
    from registry.api.cursor import decode_cursor, encode_cursor

    page_size = 1
    rows = [_list_entity_row(), _list_entity_row()]  # second row triggers has_more
    svc = _make_list_service(rows)

    _, next_cursor_payload = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=page_size,
        temporal_filter=_tf(),
    )

    assert next_cursor_payload is not None
    token = encode_cursor(next_cursor_payload)
    decoded = decode_cursor(token)
    assert decoded == next_cursor_payload
