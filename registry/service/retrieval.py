"""RetrievalService — three-arm hybrid search (semantic + lexical + graph).

Architecture:
- Three arms run concurrently via asyncio.gather(return_exceptions=True).
- A failing arm is logged at WARN and excluded from fusion; the call never
  raises to the caller.
- Fusion uses rank-based normalisation (1/rank) within each arm, then
  linearly combines with weights 0.5 semantic + 0.3 lexical + 0.2 graph.
  If an arm is absent (empty results or exception) its weight is redistributed
  proportionally across surviving arms.
- Dedup by entity_id: max fused score per entity wins.
- Final tenant assertion: any row whose tenant_id != ctx.tenant_id is
  silently dropped post-fusion (defense-in-depth on top of query filters).

Semantic arm:
  - Embedding is LRU-cached by sha256(query_text + model_version).
  - Must run inside an explicit transaction so SET LOCAL hnsw.ef_search has
    effect (SET LOCAL is a no-op outside a transaction).
  - Over-fetches top_k * 4 rows and returns the nearest top_k after SET LOCAL.

Lexical arm:
  - tsvector @@ plainto_tsquery on facts.ts_vector (GIN index).
  - Ranked via ts_rank_cd.

Graph arm (search helper):
  - Recursive CTE on edges, depth <= 2 (hardcoded for search), edge types
    depends_on | integrates_with | event_source.
  - Service-layer cap: depth <= 5 regardless of caller input.

get_dependencies:
  - Recursive CTE depth capped at min(requested, 5).
  - depth_counter column counts inclusive hops (1-based).

list_capabilities:
  - Paginated entity list; keyset (cursor) pagination on (created_at DESC, entity_id DESC).

_traverse_cte:
  - Shared recursive CTE primitive for forward and reverse traversal.
  - direction='forward': follows src→dst (who does root depend on?).
  - direction='reverse': follows dst→src (who depends on root?).
  - Depth capped at _MAX_DEPTH (5). Default edge types exclude concept_of,
    operation_of, instance_of (structural-typing edges, not dependencies).
  - Returns list[dict] with member_entity_id, depth, edge_path, edge_rels.
  - Visibility filtering is the caller's responsibility; this method returns
    all reachable members without cross-tenant filtering.
  - Callers provide an open AsyncSession; this method does not manage sessions.

Version predicate evaluation:
  - _evaluate_edge_predicates: for each hydrated EdgeRef, checks the
    properties.version predicate against the target entity's resolved version
    (looked up from attributes table key='version').
  - A missing predicate means the edge is always satisfied (True).
  - A malformed predicate or missing entity version returns False.
  - When as_of_version is set, _filter_cte_rows_by_version removes CTE rows
    whose terminal edge predicate is not satisfied; the rest of the path is
    still returned but those edges are excluded from traversal following.
  - Both get_reverse_traversal and get_blast_radius wire this through.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import uuid
from typing import Any

from cachetools import LRUCache  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.config import Settings
from registry.service.temporal import build_as_of_filter
from registry.service.version_predicates import evaluate_version_predicate
from registry.service.visibility import VisibilityService
from registry.types import (
    Clock,
    EdgeRef,
    Embedder,
    EntityRef,
    FactRef,
    SearchResult,
    TemporalFilter,
    TenantContext,
    TraversalResult,
)

_log = logging.getLogger(__name__)

# Graph arm: permitted edge relationship types for search traversal.
_GRAPH_EDGE_TYPES = ("depends_on", "integrates_with", "event_source")

# Maximum recursion depth for any CTE (depth > 5 risks performance on large graphs).
_MAX_DEPTH = 5

# Search-time graph hop limit (separate from get_dependencies cap).
_SEARCH_GRAPH_DEPTH = 2

# Cache horizon: as_of values older than this many days bypass the embedding cache.
_CACHE_HORIZON_DAYS: int = 90

# Edge rel values excluded from the default traversal set.
# These are structural / typing edges, not dependency relationships.
_TRAVERSAL_EXCLUDED_RELS: frozenset[str] = frozenset({"concept_of", "operation_of", "instance_of"})

# All vocab edge_rel values known to the system.
# Updated when new edge_rel vocab rows are added by migration.
_ALL_VOCAB_RELS: tuple[str, ...] = (
    "depends_on",
    "integrates_with",
    "event_source",
    "replaced_by",
    "requires",
    "conflicts_with",
    "composes",
    "provides_to",
    "concept_of",
    "operation_of",
    "instance_of",
)

# Default edge types for reverse/blast-radius traversal: all vocab minus excluded set.
_DEFAULT_TRAVERSAL_EDGE_TYPES: tuple[str, ...] = tuple(r for r in _ALL_VOCAB_RELS if r not in _TRAVERSAL_EXCLUDED_RELS)


def _version_edge_satisfied(
    edge: EdgeRef | None,
    as_of_version: str,
    entity_versions: dict[uuid.UUID, str | None],
) -> bool:
    """Return True if ``edge`` is satisfied by ``as_of_version``.

    - If ``edge`` is None (not hydrated): treated as satisfied.
    - If ``edge.properties`` has no ``version`` key: always satisfied.
    - If the entity version is unknown: unsatisfied (False).
    - Otherwise: delegates to ``evaluate_version_predicate``.

    This is the per-edge predicate check used when ``as_of_version`` is set
    to decide whether to include an edge in the traversal result.
    """
    if edge is None:
        return True
    predicate: str | None = None
    if edge.properties and isinstance(edge.properties, dict):
        predicate = edge.properties.get("version")
    if not predicate:
        return True
    target_version = entity_versions.get(edge.dst_entity_id)
    if target_version is None:
        return False
    return evaluate_version_predicate(target_version, predicate)


def _cache_key(query_text: str, model_version: str) -> str:
    """SHA-256 digest of query_text + model_version used as LRU cache key."""
    payload = (query_text + model_version).encode()
    return hashlib.sha256(payload).hexdigest()


def _normalize_scores(scores: list[float]) -> list[float]:
    """Rank-based normalisation: score for rank r (1-based) = 1/r.

    Input list is ordered best-first; output preserves the same order.
    Returns empty list for empty input.
    """
    return [1.0 / (rank + 1) for rank, _ in enumerate(scores)]


def _redistribute_weights(
    weights: dict[str, float],
    failed_arms: set[str],
) -> dict[str, float]:
    """Return new weights with failed arms removed and remaining scaled to sum=1."""
    surviving = {arm: w for arm, w in weights.items() if arm not in failed_arms}
    total = sum(surviving.values())
    if total == 0.0:
        return {}
    return {arm: w / total for arm, w in surviving.items()}


class RetrievalService:
    """Consumer read surface — hybrid search, dependency traversal, listing."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        embedder: Embedder,
        settings: Settings | None = None,
        visibility: VisibilityService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._embedder = embedder
        # VisibilityService is the cross-tenant chokepoint. When wired,
        # `_apply_visibility` delegates to it for private/tenant-shared/public
        # enforcement. When None (unit-test paths that don't inject it),
        # `_apply_visibility` falls back to same-tenant filtering at fetch
        # time — a strict subset of cross-tenant filtering, so still secure.
        self._visibility = visibility
        _maxsize = settings.embedding_cache_maxsize if settings is not None else 1024
        self._embed_cache: LRUCache[str, list[float]] = LRUCache(maxsize=_maxsize)
        # Guards the cache-miss check + encode + write sequence so concurrent
        # coroutines on the same key don't call the embedder more than once.
        # Cache hits release the lock immediately; the contention cost is
        # negligible compared to a single encode call.
        self._embed_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Visibility chokepoint
    # ------------------------------------------------------------------

    async def _apply_visibility(
        self,
        ctx: TenantContext,
        entity_ids: list[uuid.UUID] | set[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Filter *entity_ids* through VisibilityService when available.

        Returns the subset of *entity_ids* visible to ``ctx.tenant_id``
        (private / tenant-shared / public). When no
        VisibilityService is injected, returns the full set unchanged —
        the caller's downstream ``_fetch_entity_refs`` then applies a
        same-tenant SQL filter, which is a strict subset of cross-tenant
        filtering and remains secure.
        """
        if not entity_ids:
            return set()
        ids_list = list(entity_ids)
        if self._visibility is not None:
            visible = await self._visibility.filter_entities(ctx, ids_list)
            return set(visible)
        return set(ids_list)

    # ------------------------------------------------------------------
    # Embedding helper (cached)
    # ------------------------------------------------------------------

    async def _encode_query(self, query_text: str) -> list[float]:
        """Return embedding vector for query_text, using LRU cache.

        The lock ensures that concurrent coroutines waiting on the same key
        only call the embedder once — the second caller finds the result
        already written when it acquires the lock.
        """
        key = _cache_key(query_text, self._embedder.model_version)
        async with self._embed_lock:
            cached = self._embed_cache.get(key)
            if cached is not None:
                return cached  # type: ignore[no-any-return]
            vec = self._embedder.encode([query_text])
            result: list[float] = vec[0].tolist()
            self._embed_cache[key] = result
            return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None = None,
        lifecycle: str | None = None,
    ) -> list[SearchResult]:
        """Three-arm hybrid search.

        Arms run concurrently; a failing arm is excluded without raising.
        Weights: 0.5 semantic + 0.3 lexical + 0.2 graph.
        Dedup by entity_id (max fused score wins). Final tenant assertion applied.
        """
        base_weights: dict[str, float] = {
            "semantic": 0.5,
            "lexical": 0.3,
            "graph": 0.2,
        }

        semantic_task = self._semantic_arm(ctx, q, top_k, temporal_filter, entity_type)
        lexical_task = self._lexical_arm(ctx, q, top_k, temporal_filter, entity_type)
        graph_task = self._graph_arm(ctx, q, top_k, temporal_filter, entity_type)

        raw_results = await asyncio.gather(
            semantic_task,
            lexical_task,
            graph_task,
            return_exceptions=True,
        )

        arm_names = ("semantic", "lexical", "graph")
        arm_results: dict[str, list[tuple[uuid.UUID, EntityRef, list[FactRef]]]] = {}
        failed_arms: set[str] = set()

        for name, result in zip(arm_names, raw_results, strict=True):
            if isinstance(result, BaseException):
                _log.warning(
                    "retrieval arm failed — excluding from fusion",
                    extra={"arm": name, "error": str(result)},
                )
                failed_arms.add(name)
            else:
                arm_results[name] = result

        effective_weights = _redistribute_weights(base_weights, failed_arms)

        # Fuse: entity_id → (score, EntityRef, matching_facts, arm_scores)
        fused: dict[uuid.UUID, dict[str, Any]] = {}

        for arm_name, weight in effective_weights.items():
            rows = arm_results.get(arm_name, [])
            if not rows:
                continue
            rank_scores = _normalize_scores([1.0] * len(rows))  # rank-based
            for rank, (entity_id, entity_ref, facts) in enumerate(rows):
                contribution = weight * rank_scores[rank]
                if entity_id not in fused:
                    fused[entity_id] = {
                        "score": 0.0,
                        "entity": entity_ref,
                        "facts": facts,
                        "arms": {},
                    }
                fused[entity_id]["score"] += contribution
                fused[entity_id]["arms"][arm_name] = fused[entity_id]["arms"].get(arm_name, 0.0) + contribution

        # Cross-tenant chokepoint: filter fused entity IDs through VisibilityService.
        # When no VisibilityService is wired (unit-test paths), fall back to the
        # strict same-tenant defense-in-depth assertion.
        results: list[SearchResult] = []
        if self._visibility is not None:
            visible_ids = await self._apply_visibility(ctx, list(fused.keys()))
            for entity_id, data in fused.items():
                if entity_id not in visible_ids:
                    continue
                results.append(
                    SearchResult(
                        entity=data["entity"],
                        matching_facts=data["facts"],
                        score=data["score"],
                        retrieval_arms=data["arms"],
                    )
                )
        else:
            for entity_id, data in fused.items():
                if data["entity"].tenant_id != ctx.tenant_id:
                    _log.warning(
                        "post-fusion tenant assertion failed — dropping row",
                        extra={
                            "entity_id": str(entity_id),
                            "tenant_id": str(data["entity"].tenant_id),
                        },
                    )
                    continue
                results.append(
                    SearchResult(
                        entity=data["entity"],
                        matching_facts=data["facts"],
                        score=data["score"],
                        retrieval_arms=data["arms"],
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def get_dependencies(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        depth: int,
        temporal_filter: TemporalFilter,
    ) -> list[EdgeRef]:
        """Recursive CTE on edges, depth capped at _MAX_DEPTH (G4 binding).

        depth_counter counts inclusive hops (1-based): the root entity is hop 0;
        its direct neighbours are hop 1.  Caller receives all edges up to depth.
        """
        capped_depth = min(depth, _MAX_DEPTH)
        now = self._clock.now()

        # Anchor branch: plain `FROM edges` — no join ambiguity, no alias needed.
        tf_sql_anchor, tf_params = self._temporal_sql_fragments(temporal_filter, now)
        # Recursive branch: `FROM edges e JOIN dep_cte …` — bare column names are
        # ambiguous because dep_cte also exposes the same temporal columns.  Use
        # the "e." prefix so PostgreSQL resolves references unambiguously.
        tf_sql_rec, _ = self._temporal_sql_fragments(temporal_filter, now, table_alias="e")
        # Both fragments share param names with identical values; merge is safe.

        sql = text(
            """
            WITH RECURSIVE dep_cte AS (
                SELECT
                    edge_id,
                    tenant_id,
                    src_entity_id,
                    rel,
                    dst_entity_id,
                    properties,
                    is_authoritative,
                    sync_run_id,
                    t_valid_from,
                    t_valid_to,
                    t_ingested_at,
                    t_invalidated_at,
                    1 AS depth_counter
                FROM edges
                WHERE src_entity_id = :root_id
                  AND tenant_id = :tid
                  AND rel = ANY(:edge_types)
                  AND """
            + tf_sql_anchor
            + """

                UNION ALL

                SELECT
                    e.edge_id,
                    e.tenant_id,
                    e.src_entity_id,
                    e.rel,
                    e.dst_entity_id,
                    e.properties,
                    e.is_authoritative,
                    e.sync_run_id,
                    e.t_valid_from,
                    e.t_valid_to,
                    e.t_ingested_at,
                    e.t_invalidated_at,
                    dep_cte.depth_counter + 1
                FROM edges e
                JOIN dep_cte ON e.src_entity_id = dep_cte.dst_entity_id
                WHERE e.tenant_id = :tid
                  AND e.rel = ANY(:edge_types)
                  AND dep_cte.depth_counter < :max_depth
                  AND """
            + tf_sql_rec
            + """
            )
            SELECT * FROM dep_cte ORDER BY depth_counter, edge_id
            """
        )

        params: dict[str, Any] = {
            "root_id": entity_id,
            "tid": ctx.tenant_id,
            "edge_types": list(_GRAPH_EDGE_TYPES),
            "max_depth": capped_depth,
            **tf_params,
        }

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return [
            EdgeRef(
                edge_id=row["edge_id"],
                tenant_id=row["tenant_id"],
                src_entity_id=row["src_entity_id"],
                rel=row["rel"],
                dst_entity_id=row["dst_entity_id"],
                properties=row["properties"],
                t_valid_from=row["t_valid_from"],
                t_valid_to=row["t_valid_to"],
                t_ingested_at=row["t_ingested_at"],
                t_invalidated_at=row["t_invalidated_at"],
            )
            for row in rows
            if row["tenant_id"] == ctx.tenant_id  # tenant assertion
        ]

    async def list_capabilities(
        self,
        ctx: TenantContext,
        lifecycle: str | None,
        entity_type: str | None,
        cursor: dict[str, Any],
        page_size: int,
        temporal_filter: TemporalFilter,
    ) -> tuple[list[EntityRef], dict[str, Any] | None]:
        """Paginated entity list filtered by tenant (and optionally lifecycle/entity_type).

        Uses keyset pagination on (created_at DESC, entity_id DESC) so performance
        is constant at any depth — no OFFSET scan. Pass the opaque ``cursor`` dict
        decoded from ``api/cursor.py``; an empty dict starts from the first page.

        Returns a (items, next_cursor_payload) tuple. ``next_cursor_payload`` is
        None when no further pages exist; otherwise it is a dict ready to pass to
        ``encode_cursor``.

        Entities do not have bi-temporal columns (they use `is_active` as their
        lifecycle flag). Temporal filtering is applied only to the attributes
        sub-query used for the lifecycle filter, not to the entity row itself.
        """
        now = self._clock.now()

        filters = ["e.tenant_id = :tid AND e.is_active = TRUE"]
        params: dict[str, Any] = {"tid": ctx.tenant_id}

        if entity_type is not None:
            filters.append("e.entity_type = :entity_type")
            params["entity_type"] = entity_type

        if lifecycle is not None:
            # lifecycle is stored as an attribute with key='lifecycle'
            filters.append(
                """EXISTS (
                    SELECT 1 FROM attributes a
                    WHERE a.entity_id = e.entity_id
                      AND a.tenant_id = :tid
                      AND a.key = 'lifecycle'
                      AND a.value = to_jsonb(:lifecycle::text)
                      AND a.t_invalidated_at IS NULL
                      AND (a.t_valid_to IS NULL OR a.t_valid_to > :lc_now)
                )"""
            )
            params["lifecycle"] = lifecycle
            params["lc_now"] = now

        # Keyset predicate: skip rows at-or-after the cursor position.
        # The sort is (created_at DESC, entity_id DESC) so "before in the cursor
        # order" means a strictly smaller (ts, id) tuple.
        if cursor:
            filters.append("(e.created_at, e.entity_id) < (:cursor_ts, :cursor_id)")
            import datetime as _dt  # noqa: PLC0415

            params["cursor_ts"] = _dt.datetime.fromisoformat(cursor["ts"])
            params["cursor_id"] = cursor["id"]

        where_clause = " AND ".join(filters)

        sql = text(
            f"""
            SELECT e.entity_id, e.tenant_id, e.entity_type, e.name,
                   e.external_id, e.is_active, e.created_at
            FROM entities e
            WHERE {where_clause}
            ORDER BY e.created_at DESC, e.entity_id DESC
            LIMIT :limit
            """
        )
        # Fetch one extra row to detect whether a next page exists.
        params["limit"] = page_size + 1

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]

        items = [
            EntityRef(
                entity_id=row["entity_id"],
                tenant_id=row["tenant_id"],
                entity_type=row["entity_type"],
                name=row["name"],
                external_id=row["external_id"],
                is_active=row["is_active"],
                created_at=row["created_at"],
            )
            for row in page_rows
            if row["tenant_id"] == ctx.tenant_id
        ]

        next_cursor_payload: dict[str, Any] | None = None
        if has_more and items:
            last = items[-1]
            next_cursor_payload = {
                "ts": last.created_at.isoformat(),
                "id": str(last.entity_id),
            }

        return items, next_cursor_payload

    # ------------------------------------------------------------------
    # Public graph traversal
    # ------------------------------------------------------------------

    async def get_reverse_traversal(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        depth: int = 2,
        edge_types: list[str] | None = None,
        as_of: Any | None = None,
        as_of_version: str | None = None,
        clock: Clock | None = None,
    ) -> TraversalResult:
        """Reverse traversal: who depends ON entity_id?

        Symmetric to ``get_dependencies`` (forward).  Uses ``_traverse_cte``
        with ``direction='reverse'``.

        Parameters
        ----------
        ctx:
            Tenant + actor context.  Ownership checks are applied here.
        entity_id:
            Root entity for the traversal (the node being depended upon).
        depth:
            Maximum hop count (1–5).  Capped at 5 by the service layer
            regardless of caller input (G4 binding).
        edge_types:
            Restrict traversal to these ``rel`` values.  ``None`` → all vocab
            minus structural-typing edges (``concept_of``, ``operation_of``,
            ``instance_of``).
        as_of:
            Optional ISO-8601 UTC datetime for time-travel queries.  ``None``
            → current-truth filter (``t_invalidated_at IS NULL``).
        as_of_version:
            Optional semver string.  When set, traversal only follows edges
            whose ``properties.version`` predicate is satisfied by this version.
            Edges with no predicate are always included.
            Unsatisfied predicates are flagged in ``version_satisfied`` but do
            not prune paths unless ``as_of_version`` is supplied.
        clock:
            Injectable clock.  Defaults to the service's own clock when
            ``None``.

        Returns
        -------
        TraversalResult
            ``direction='reverse'``, ``cache_hit=False`` (cache is T06).
            ``version_satisfied[edge_id]`` reflects predicate evaluation.
            When ``as_of_version`` is set, only edges whose predicate is
            satisfied are included in the result; other edges are omitted.
            Visibility filter applied after traversal: only nodes visible to
            ``ctx.tenant_id`` are included in the returned ``nodes`` set.
        """
        effective_clock = clock if clock is not None else self._clock
        now = effective_clock.now()

        capped_depth = min(depth, _MAX_DEPTH)

        # Build temporal filter from the caller-supplied as_of datetime.
        temporal_filter = TemporalFilter(as_of=as_of)

        async with self._session_factory() as session:
            cte_rows = await self._traverse_cte(
                session=session,
                tenant_id=ctx.tenant_id,
                root_entity_id=entity_id,
                direction="reverse",
                depth=capped_depth,
                edge_types=edge_types,
                temporal_filter=temporal_filter,
                as_of=now,
            )

        # Collect all edge IDs from all CTE paths for batch hydration.
        all_edge_ids: set[uuid.UUID] = set()
        for row in cte_rows:
            for eid in row["edge_path"]:
                all_edge_ids.add(eid)

        # Batch-fetch real edge rows (with properties) so we can evaluate
        # version predicates.  This replaces the prior stub approach.
        hydrated_edges_map: dict[uuid.UUID, EdgeRef] = {}
        if all_edge_ids:
            fetched = await self._fetch_edge_refs(
                ctx=ctx,
                edge_ids=list(all_edge_ids),
                now=now,
            )
            hydrated_edges_map = {e.edge_id: e for e in fetched}

        # Evaluate version predicates for all hydrated edges.
        # member_entity_id is the SOURCE in a reverse traversal (the node that
        # depends on root); the edge dst is what the predicate guards.
        # We resolve the target entity's version from the attributes table.
        # For reverse traversal, the edge goes src→dst where dst is the target
        # from the predicate's point of view (the entity being required).
        # We collect dst_entity_ids from fetched edges to resolve versions.
        dst_entity_ids: set[uuid.UUID] = {e.dst_entity_id for e in hydrated_edges_map.values()}
        entity_versions: dict[uuid.UUID, str | None] = {}
        if dst_entity_ids:
            entity_versions = await self._resolve_entity_versions(
                tenant_id=ctx.tenant_id,
                entity_ids=list(dst_entity_ids),
                as_of=as_of,
                now=now,
            )

        # Compute version_satisfied per edge.
        version_satisfied: dict[uuid.UUID, bool] = self._evaluate_edge_predicates(
            edges=list(hydrated_edges_map.values()),
            entity_versions=entity_versions,
        )

        # When as_of_version is set, filter CTE rows to only those whose paths
        # consist entirely of predicate-satisfied edges.  Rows where any edge
        # on the path is unsatisfied are excluded from traversal results.
        if as_of_version is not None:
            cte_rows = self._filter_cte_rows_by_version(
                cte_rows=cte_rows,
                edge_predicates_satisfied={
                    eid: _version_edge_satisfied(hydrated_edges_map.get(eid), as_of_version, entity_versions)
                    for eid in all_edge_ids
                },
            )

        # Collect unique member entity IDs reached by the (filtered) traversal.
        member_entity_ids: list[uuid.UUID] = []
        seen_members: set[uuid.UUID] = set()
        for row in cte_rows:
            mid = row["member_entity_id"]
            if mid not in seen_members:
                seen_members.add(mid)
                member_entity_ids.append(mid)

        # Cross-tenant chokepoint: filter members through VisibilityService.
        # Falls back to same-tenant when no visibility is wired (test paths).
        visible_member_ids: set[uuid.UUID] = await self._apply_visibility(ctx, member_entity_ids)

        # Collect unique edge IDs from the (filtered) traversal paths.
        edges: list[EdgeRef] = []
        seen_edge_ids: set[uuid.UUID] = set()
        for row in cte_rows:
            if row["member_entity_id"] not in visible_member_ids:
                continue
            for eid in row["edge_path"]:
                if eid not in seen_edge_ids:
                    seen_edge_ids.add(eid)
                    edge_obj = hydrated_edges_map.get(eid)
                    if edge_obj is not None:
                        edges.append(edge_obj)
                    else:
                        # Edge was not returned from DB (invalidated or missing);
                        # emit a minimal stub so the path is still traceable.
                        edges.append(
                            EdgeRef(
                                edge_id=eid,
                                tenant_id=ctx.tenant_id,
                                src_entity_id=uuid.UUID(int=0),
                                rel=row["edge_rels"][row["edge_path"].index(eid)],
                                dst_entity_id=uuid.UUID(int=0),
                                properties=None,
                                t_valid_from=now,
                                t_valid_to=None,
                                t_ingested_at=now,
                                t_invalidated_at=None,
                            )
                        )

        # Trim version_satisfied to only the edges present in the (filtered) result.
        version_satisfied = {eid: version_satisfied.get(eid, True) for eid in seen_edge_ids}

        # Hydrate EntityRef stubs for visible members.
        # A secondary SELECT fetches entity metadata for all member IDs in one
        # round-trip (avoids N+1). Post-visibility IDs may span tenants, so
        # the SQL filter relaxes to entity_id only (VisibilityService has already vetted).
        nodes: list[EntityRef] = []
        if visible_member_ids:
            nodes = await self._fetch_entity_refs(
                ctx=ctx,
                entity_ids=list(visible_member_ids),
                enforce_same_tenant=self._visibility is None,
            )

        _log.debug(
            "reverse_traversal completed",
            extra={
                "root_entity_id": str(entity_id),
                "depth": capped_depth,
                "nodes": len(nodes),
                "edges": len(edges),
                "as_of_version": as_of_version,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        return TraversalResult(
            root_entity_id=entity_id,
            depth=capped_depth,
            direction="reverse",
            as_of=as_of,
            nodes=nodes,
            edges=edges,
            version_satisfied=version_satisfied,
            cache_hit=False,
        )

    async def _fetch_entity_refs(
        self,
        ctx: TenantContext,
        entity_ids: list[uuid.UUID],
        enforce_same_tenant: bool = True,
    ) -> list[EntityRef]:
        """Batch-fetch EntityRef objects for a list of entity IDs.

        Two modes:

        * ``enforce_same_tenant=True`` (default): filters at SQL by
          ``ctx.tenant_id``, with a defense-in-depth assertion when rows come
          back. Callers without a VisibilityService in play stay on this path.

        * ``enforce_same_tenant=False`` (post-visibility path): fetches by
          entity_id alone. Callers MUST have already gated ``entity_ids``
          through :py:meth:`_apply_visibility` — the cross-tenant chokepoint
          — otherwise this leaks cross-tenant rows. The defense-in-depth
          assertion is intentionally dropped because visible entities
          legitimately belong to other tenants.

        Returns only active entities found in the DB; missing IDs are
        silently omitted (deleted/purged entities).
        """
        if not entity_ids:
            return []

        if enforce_same_tenant:
            sql = text(
                """
                SELECT entity_id, tenant_id, entity_type, name,
                       external_id, is_active, created_at
                FROM entities
                WHERE tenant_id = :tid
                  AND entity_id = ANY(:ids)
                  AND is_active = TRUE
                ORDER BY created_at DESC, entity_id
                """
            )
            params: dict[str, Any] = {"tid": ctx.tenant_id, "ids": entity_ids}
        else:
            sql = text(
                """
                SELECT entity_id, tenant_id, entity_type, name,
                       external_id, is_active, created_at
                FROM entities
                WHERE entity_id = ANY(:ids)
                  AND is_active = TRUE
                ORDER BY created_at DESC, entity_id
                """
            )
            params = {"ids": entity_ids}

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return [
            EntityRef(
                entity_id=row["entity_id"],
                tenant_id=row["tenant_id"],
                entity_type=row["entity_type"],
                name=row["name"],
                external_id=row["external_id"],
                is_active=row["is_active"],
                created_at=row["created_at"],
            )
            for row in rows
            if (not enforce_same_tenant) or row["tenant_id"] == ctx.tenant_id
        ]

    # ------------------------------------------------------------------
    # Version predicate helpers
    # ------------------------------------------------------------------

    async def _resolve_entity_versions(
        self,
        tenant_id: uuid.UUID,
        entity_ids: list[uuid.UUID],
        as_of: Any | None,
        now: Any,
    ) -> dict[uuid.UUID, str | None]:
        """Batch-fetch the ``version`` attribute for a list of entity IDs.

        Version is stored in the ``attributes`` table as key='version' with
        a JSONB string value (e.g. ``"2.4.0"``).  Returns a mapping of
        entity_id → version string (or ``None`` if not found).

        When ``as_of`` is set, fetches the version valid at that point in time;
        otherwise fetches the current-truth version (``t_invalidated_at IS NULL``).
        """
        if not entity_ids:
            return {}

        temporal_filter = TemporalFilter(as_of=as_of)
        tf_sql, tf_params = self._temporal_sql_fragments(temporal_filter, now, table_alias="a")

        sql = text(
            f"""
            SELECT DISTINCT ON (a.entity_id)
                a.entity_id,
                a.value
            FROM attributes a
            WHERE a.tenant_id = :tid
              AND a.entity_id = ANY(:ids)
              AND a.key = 'version'
              AND {tf_sql}
            ORDER BY a.entity_id, a.t_valid_from DESC
            """
        )

        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql,
                    {"tid": tenant_id, "ids": entity_ids, **tf_params},
                )
                rows = result.mappings().all()
        except Exception:
            _log.warning(
                "version_predicate: entity version lookup failed; treating all as no-version",
                extra={"tenant_id": str(tenant_id)},
                exc_info=True,
            )
            return {eid: None for eid in entity_ids}

        result_map: dict[uuid.UUID, str | None] = {eid: None for eid in entity_ids}
        for row in rows:
            # JSONB string value is returned as Python str with surrounding quotes
            # by psycopg2/asyncpg when the JSONB type is a JSON string.
            # asyncpg returns the deserialized Python value directly.
            raw = row["value"]
            if isinstance(raw, str):
                ver = raw.strip('"')
            elif isinstance(raw, dict):
                # Unexpected; skip
                ver = None
            else:
                ver = str(raw) if raw is not None else None
            result_map[row["entity_id"]] = ver

        return result_map

    @staticmethod
    def _evaluate_edge_predicates(
        edges: list[EdgeRef],
        entity_versions: dict[uuid.UUID, str | None],
    ) -> dict[uuid.UUID, bool]:
        """Evaluate ``properties.version`` predicates for each edge.

        For each edge:
        - If ``properties`` is None or has no ``version`` key: ``True``
          (no constraint = always satisfied).
        - If the predicate string is empty: ``True``.
        - Otherwise: evaluate via ``evaluate_version_predicate`` against the
          target entity's resolved version.  If the entity version is unknown
          or the predicate is malformed: ``False``.

        Returns a dict mapping edge_id → bool.
        """
        result: dict[uuid.UUID, bool] = {}
        for edge in edges:
            predicate: str | None = None
            if edge.properties and isinstance(edge.properties, dict):
                predicate = edge.properties.get("version")

            if not predicate:
                # No predicate → always satisfied.
                result[edge.edge_id] = True
                continue

            target_version = entity_versions.get(edge.dst_entity_id)
            if target_version is None:
                # Cannot resolve target version → predicate is unsatisfied.
                result[edge.edge_id] = False
                continue

            result[edge.edge_id] = evaluate_version_predicate(target_version, predicate)

        return result

    @staticmethod
    def _filter_cte_rows_by_version(
        cte_rows: list[dict[str, Any]],
        edge_predicates_satisfied: dict[uuid.UUID, bool],
    ) -> list[dict[str, Any]]:
        """Filter CTE rows to only those whose entire edge_path is version-satisfied.

        When ``as_of_version`` is set, traversal must only follow edges whose
        predicate is satisfied.  A row is included only if every edge on its
        path satisfies its predicate (or has no predicate).

        Edges not present in ``edge_predicates_satisfied`` (e.g. invalidated
        edges not returned from the DB) are treated as satisfied (True) to
        avoid incorrectly pruning paths with missing edge metadata.
        """
        filtered: list[dict[str, Any]] = []
        for row in cte_rows:
            if all(edge_predicates_satisfied.get(eid, True) for eid in row["edge_path"]):
                filtered.append(row)
        return filtered

    # ------------------------------------------------------------------
    # Public graph traversal — blast-radius
    # ------------------------------------------------------------------

    async def get_blast_radius(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        direction: str = "reverse",
        depth: int = 5,
        edge_types: list[str] | None = None,
        as_of: Any | None = None,
        as_of_version: str | None = None,
        clock: Clock | None = None,
    ) -> TraversalResult:
        """Transitive closure from entity_id with cache-first read path.

        Read path:
        1. If ``as_of < now() - 90 days``: CTE fallback (beyond cache horizon).
        2. Else: query ``closure_cache``.  Empty result → CTE fallback.
        3. Apply visibility filter after traversal.
        4. Evaluate version predicates; apply ``as_of_version`` filter if set.
        5. Return ``TraversalResult(cache_hit=True|False, ...)``.

        Parameters
        ----------
        ctx:
            Tenant + actor context.
        entity_id:
            Root entity for the closure.
        direction:
            ``'forward'`` or ``'reverse'``.  Defaults to ``'reverse'``.
        depth:
            Maximum hop count (1–5).  Capped at 5 by the service layer.
            Only applied on the CTE fallback path; the cache stores the full
            depth-5 closure and the depth parameter is used for post-filtering.
        edge_types:
            Restrict traversal/cache results to these ``rel`` values.
            ``None`` → all vocab minus structural-typing edges.
        as_of:
            Optional ISO-8601 UTC datetime.  When set and before the cache
            horizon (90 days), the CTE fallback is forced.
        as_of_version:
            Optional semver string.  When set, traversal only follows edges
            whose ``properties.version`` predicate is satisfied by this version.
            Edges with no predicate are always included.
            Unsatisfied predicates are flagged in ``version_satisfied``.
        clock:
            Injectable clock.  Defaults to the service's own clock when None.

        Returns
        -------
        TraversalResult
            ``cache_hit=True`` when served from ``closure_cache``.
            ``version_satisfied[edge_id]`` reflects predicate evaluation.
            Nodes are filtered through VisibilityService (or same-tenant
            when VisibilityService is not wired).
        """
        if direction not in ("forward", "reverse"):
            raise ValueError(f"direction must be 'forward' or 'reverse', got {direction!r}")

        effective_clock = clock if clock is not None else self._clock
        now = effective_clock.now()

        capped_depth = min(depth, _MAX_DEPTH)

        # --- Cache horizon check ---
        _cache_horizon = datetime.timedelta(days=_CACHE_HORIZON_DAYS)
        use_cte = False
        if as_of is not None and as_of < (now - _cache_horizon):
            # as_of is before the 90-day cache horizon → must use CTE fallback
            use_cte = True

        temporal_filter = TemporalFilter(as_of=as_of)

        if not use_cte:
            # --- Primary path: closure_cache lookup ---
            cache_rows = await self._query_closure_cache(
                tenant_id=ctx.tenant_id,
                root_entity_id=entity_id,
                direction=direction,
            )
            if cache_rows:
                return await self._build_result_from_cache(
                    ctx=ctx,
                    entity_id=entity_id,
                    direction=direction,
                    depth=capped_depth,
                    edge_types=edge_types,
                    as_of=as_of,
                    as_of_version=as_of_version,
                    now=now,
                    cache_rows=cache_rows,
                )
            # Cache miss → fall through to CTE

        # --- CTE fallback path ---
        async with self._session_factory() as session:
            cte_rows = await self._traverse_cte(
                session=session,
                tenant_id=ctx.tenant_id,
                root_entity_id=entity_id,
                direction=direction,
                depth=capped_depth,
                edge_types=edge_types,
                temporal_filter=temporal_filter,
                as_of=now,
            )

        return await self._build_result_from_cte(
            ctx=ctx,
            entity_id=entity_id,
            direction=direction,
            depth=capped_depth,
            as_of=as_of,
            as_of_version=as_of_version,
            now=now,
            cte_rows=cte_rows,
        )

    async def _query_closure_cache(
        self,
        tenant_id: uuid.UUID,
        root_entity_id: uuid.UUID,
        direction: str,
    ) -> list[dict[str, Any]]:
        """Query closure_cache for the given root + direction.

        Returns a list of dicts with keys:
        ``{member_entity_id, depth, edge_path, edge_rels}``.
        Returns an empty list on cache miss (no rows) or any DB error (logs warning).
        """
        sql = text(
            """
            SELECT member_entity_id, depth, edge_path, edge_rels
            FROM   closure_cache
            WHERE  tenant_id        = :tid
              AND  root_entity_id   = :root_id
              AND  direction        = :direction
            ORDER  BY depth ASC, member_entity_id
            """
        )
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql,
                    {"tid": tenant_id, "root_id": root_entity_id, "direction": direction},
                )
                rows = result.mappings().all()
        except Exception:
            _log.warning(
                "blast_radius: closure_cache query failed — falling back to CTE",
                extra={"root_entity_id": str(root_entity_id), "direction": direction},
                exc_info=True,
            )
            return []

        return [
            {
                "member_entity_id": row["member_entity_id"],
                "depth": row["depth"],
                "edge_path": list(row["edge_path"]),
                "edge_rels": list(row["edge_rels"]),
            }
            for row in rows
        ]

    async def _build_result_from_cache(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        as_of: Any | None,
        as_of_version: str | None,
        now: Any,
        cache_rows: list[dict[str, Any]],
    ) -> TraversalResult:
        """Hydrate a TraversalResult from closure_cache rows.

        Applies depth and edge_types filters post-fetch, then batch-fetches
        entity metadata and edge metadata for visible members.  Evaluates
        version predicates and applies ``as_of_version`` filter when set.
        """
        resolved_edge_types: frozenset[str] | None = frozenset(edge_types) if edge_types is not None else None

        # Filter to depth cap and (optionally) edge_types.
        filtered_rows: list[dict[str, Any]] = []
        for row in cache_rows:
            if row["depth"] > depth:
                continue
            if resolved_edge_types is not None:
                # Only include rows where all edge_rels on the path satisfy
                # the filter (the path's rels are a slice of the full path).
                # We include the row if ANY rel on the path matches the filter;
                # the final edge traversed is the rel that determined reachability.
                row_rels = set(row["edge_rels"])
                if not row_rels.intersection(resolved_edge_types):
                    continue
            filtered_rows.append(row)

        # Collect all edge IDs from all paths for batch hydration.
        all_edge_ids: set[uuid.UUID] = set()
        for row in filtered_rows:
            for eid in row["edge_path"]:
                all_edge_ids.add(eid)

        # Batch-hydrate edges from DB for full edge metadata (including properties).
        fetched_edges: list[EdgeRef] = []
        if all_edge_ids:
            fetched_edges = await self._fetch_edge_refs(
                ctx=ctx,
                edge_ids=list(all_edge_ids),
                now=now,
            )
        hydrated_edges_map: dict[uuid.UUID, EdgeRef] = {e.edge_id: e for e in fetched_edges}

        # Resolve target entity versions for predicate evaluation.
        dst_entity_ids: set[uuid.UUID] = {e.dst_entity_id for e in hydrated_edges_map.values()}
        entity_versions: dict[uuid.UUID, str | None] = {}
        if dst_entity_ids:
            entity_versions = await self._resolve_entity_versions(
                tenant_id=ctx.tenant_id,
                entity_ids=list(dst_entity_ids),
                as_of=as_of,
                now=now,
            )

        # Compute version_satisfied per edge.
        version_satisfied: dict[uuid.UUID, bool] = self._evaluate_edge_predicates(
            edges=fetched_edges,
            entity_versions=entity_versions,
        )

        # When as_of_version is set, filter rows to only predicate-satisfied paths.
        if as_of_version is not None:
            edge_satisfied_for_filter: dict[uuid.UUID, bool] = {
                eid: _version_edge_satisfied(hydrated_edges_map.get(eid), as_of_version, entity_versions)
                for eid in all_edge_ids
            }
            filtered_rows = self._filter_cte_rows_by_version(
                cte_rows=filtered_rows,
                edge_predicates_satisfied=edge_satisfied_for_filter,
            )

        # Collect unique member entity IDs.
        member_entity_ids: list[uuid.UUID] = []
        seen_members: set[uuid.UUID] = set()
        for row in filtered_rows:
            mid = row["member_entity_id"]
            if mid not in seen_members:
                seen_members.add(mid)
                member_entity_ids.append(mid)

        # Cross-tenant visibility filter (cache hit path).
        visible_member_ids: set[uuid.UUID] = await self._apply_visibility(ctx, member_entity_ids)

        # Collect edge objects from the final filtered paths.
        edges: list[EdgeRef] = []
        seen_edge_ids: set[uuid.UUID] = set()
        for row in filtered_rows:
            if row["member_entity_id"] not in visible_member_ids:
                continue
            for eid in row["edge_path"]:
                if eid not in seen_edge_ids:
                    seen_edge_ids.add(eid)
                    edge_obj = hydrated_edges_map.get(eid)
                    if edge_obj is not None:
                        edges.append(edge_obj)

        # Trim version_satisfied to edges in result.
        version_satisfied = {eid: version_satisfied.get(eid, True) for eid in seen_edge_ids}

        # Batch-hydrate entity metadata (cache hit path). See note in
        # get_reverse_traversal: cross-tenant fetch when visibility is wired.
        nodes: list[EntityRef] = []
        if visible_member_ids:
            nodes = await self._fetch_entity_refs(
                ctx=ctx,
                entity_ids=list(visible_member_ids),
                enforce_same_tenant=self._visibility is None,
            )

        _log.debug(
            "blast_radius completed (cache hit)",
            extra={
                "root_entity_id": str(entity_id),
                "direction": direction,
                "depth": depth,
                "nodes": len(nodes),
                "edges": len(edges),
                "as_of_version": as_of_version,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        return TraversalResult(
            root_entity_id=entity_id,
            depth=depth,
            direction=direction,  # type: ignore[arg-type]
            as_of=as_of,
            nodes=nodes,
            edges=edges,
            version_satisfied=version_satisfied,
            cache_hit=True,
        )

    async def _build_result_from_cte(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        direction: str,
        depth: int,
        as_of: Any | None,
        as_of_version: str | None,
        now: Any,
        cte_rows: list[dict[str, Any]],
    ) -> TraversalResult:
        """Hydrate a TraversalResult from CTE rows.

        Batch-fetches real edge rows (with properties) to support version
        predicate evaluation.  Applies ``as_of_version`` filter when set.
        """
        # Collect all edge IDs for batch hydration.
        all_edge_ids: set[uuid.UUID] = set()
        for row in cte_rows:
            for eid in row["edge_path"]:
                all_edge_ids.add(eid)

        # Batch-fetch real edge rows (with properties).
        fetched_edges: list[EdgeRef] = []
        if all_edge_ids:
            fetched_edges = await self._fetch_edge_refs(
                ctx=ctx,
                edge_ids=list(all_edge_ids),
                now=now,
            )
        hydrated_edges_map: dict[uuid.UUID, EdgeRef] = {e.edge_id: e for e in fetched_edges}

        # Resolve target entity versions for predicate evaluation.
        dst_entity_ids: set[uuid.UUID] = {e.dst_entity_id for e in hydrated_edges_map.values()}
        entity_versions: dict[uuid.UUID, str | None] = {}
        if dst_entity_ids:
            entity_versions = await self._resolve_entity_versions(
                tenant_id=ctx.tenant_id,
                entity_ids=list(dst_entity_ids),
                as_of=as_of,
                now=now,
            )

        # Compute version_satisfied per edge.
        version_satisfied: dict[uuid.UUID, bool] = self._evaluate_edge_predicates(
            edges=fetched_edges,
            entity_versions=entity_versions,
        )

        # When as_of_version is set, filter CTE rows to predicate-satisfied paths.
        if as_of_version is not None:
            edge_satisfied_for_filter: dict[uuid.UUID, bool] = {
                eid: _version_edge_satisfied(hydrated_edges_map.get(eid), as_of_version, entity_versions)
                for eid in all_edge_ids
            }
            cte_rows = self._filter_cte_rows_by_version(
                cte_rows=cte_rows,
                edge_predicates_satisfied=edge_satisfied_for_filter,
            )

        member_entity_ids: list[uuid.UUID] = []
        seen_members: set[uuid.UUID] = set()
        for row in cte_rows:
            mid = row["member_entity_id"]
            if mid not in seen_members:
                seen_members.add(mid)
                member_entity_ids.append(mid)

        # Cross-tenant visibility filter (CTE fallback path).
        visible_member_ids: set[uuid.UUID] = await self._apply_visibility(ctx, member_entity_ids)

        edges: list[EdgeRef] = []
        seen_edge_ids: set[uuid.UUID] = set()
        for row in cte_rows:
            if row["member_entity_id"] not in visible_member_ids:
                continue
            for eid in row["edge_path"]:
                if eid not in seen_edge_ids:
                    seen_edge_ids.add(eid)
                    edge_obj = hydrated_edges_map.get(eid)
                    if edge_obj is not None:
                        edges.append(edge_obj)
                    else:
                        # Edge not returned from DB (invalidated/purged); emit stub.
                        edges.append(
                            EdgeRef(
                                edge_id=eid,
                                tenant_id=ctx.tenant_id,
                                src_entity_id=uuid.UUID(int=0),
                                rel=row["edge_rels"][row["edge_path"].index(eid)],
                                dst_entity_id=uuid.UUID(int=0),
                                properties=None,
                                t_valid_from=now,
                                t_valid_to=None,
                                t_ingested_at=now,
                                t_invalidated_at=None,
                            )
                        )

        # Trim version_satisfied to edges in result.
        version_satisfied = {eid: version_satisfied.get(eid, True) for eid in seen_edge_ids}

        # CTE fallback path: visibility-vetted IDs may span tenants.
        nodes: list[EntityRef] = []
        if visible_member_ids:
            nodes = await self._fetch_entity_refs(
                ctx=ctx,
                entity_ids=list(visible_member_ids),
                enforce_same_tenant=self._visibility is None,
            )

        _log.debug(
            "blast_radius completed (CTE fallback)",
            extra={
                "root_entity_id": str(entity_id),
                "direction": direction,
                "depth": depth,
                "nodes": len(nodes),
                "edges": len(edges),
                "as_of_version": as_of_version,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        return TraversalResult(
            root_entity_id=entity_id,
            depth=depth,
            direction=direction,  # type: ignore[arg-type]
            as_of=as_of,
            nodes=nodes,
            edges=edges,
            version_satisfied=version_satisfied,
            cache_hit=False,
        )

    async def _fetch_edge_refs(
        self,
        ctx: TenantContext,
        edge_ids: list[uuid.UUID],
        now: Any,
    ) -> list[EdgeRef]:
        """Batch-fetch EdgeRef objects for a list of edge IDs.

        Filters to ctx.tenant_id.  Missing or invalidated edges are silently
        omitted.  Returns edges active at current truth (t_invalidated_at IS NULL).
        Falls back to returning stub EdgeRef objects on DB error.
        """
        if not edge_ids:
            return []

        sql = text(
            """
            SELECT edge_id, tenant_id, src_entity_id, rel, dst_entity_id,
                   properties, t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at
            FROM   edges
            WHERE  tenant_id = :tid
              AND  edge_id   = ANY(:ids)
              AND  t_invalidated_at IS NULL
            """
        )

        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql,
                    {"tid": ctx.tenant_id, "ids": edge_ids},
                )
                rows = result.mappings().all()
        except Exception:
            _log.warning(
                "blast_radius: edge batch-fetch failed; returning stub EdgeRefs",
                extra={"tenant_id": str(ctx.tenant_id)},
                exc_info=True,
            )
            return []

        return [
            EdgeRef(
                edge_id=row["edge_id"],
                tenant_id=row["tenant_id"],
                src_entity_id=row["src_entity_id"],
                rel=row["rel"],
                dst_entity_id=row["dst_entity_id"],
                properties=row["properties"],
                t_valid_from=row["t_valid_from"],
                t_valid_to=row["t_valid_to"],
                t_ingested_at=row["t_ingested_at"],
                t_invalidated_at=row["t_invalidated_at"],
            )
            for row in rows
            if row["tenant_id"] == ctx.tenant_id  # defense-in-depth
        ]

    # ------------------------------------------------------------------
    # Graph traversal primitive — shared by reverse-traversal and blast-radius
    # ------------------------------------------------------------------

    async def _traverse_cte(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        root_entity_id: uuid.UUID,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        temporal_filter: TemporalFilter,
        as_of: Any,
    ) -> list[dict[str, Any]]:
        """Recursive CTE traversal primitive.  No version predicates; no visibility filter.

        Parameters
        ----------
        session:
            Active async session provided by the caller.  Callers are responsible
            for opening and closing the session; this method does not manage it.
        tenant_id:
            Tenant scope for all edge lookups.
        root_entity_id:
            Starting entity for the traversal.
        direction:
            ``'forward'`` — follows edges where ``src_entity_id = root`` outward
            through ``dst_entity_id`` (who does root depend on?).
            ``'reverse'`` — follows edges where ``dst_entity_id = root`` inward
            through ``src_entity_id`` (who depends on root?).
        depth:
            Maximum hop count from root.  Internally capped at ``_MAX_DEPTH`` (5).
        edge_types:
            Restrict traversal to these ``rel`` values.  ``None`` → all vocab
            edge_rel values minus the structural-typing exclusion set
            (``concept_of``, ``operation_of``, ``instance_of``).
        temporal_filter:
            Bi-temporal filter applied at every CTE hop.
        as_of:
            Pre-resolved ``now()`` value passed by the caller.  Used for the
            ``tf_now`` parameter in current-truth fragments.

        Returns
        -------
        list[dict]
            One dict per row: ``{member_entity_id, depth, edge_path, edge_rels}``.
            ``member_entity_id`` is the non-root end of each traversal path.
            ``depth`` is the hop count from root (1-based: immediate neighbours = 1).
            ``edge_path`` is an ordered list of edge UUIDs on the shortest path.
            ``edge_rels`` is a parallel list of rel values for each edge in the path.
            Rows are ordered by depth ascending, then member_entity_id.
        """
        if direction not in ("forward", "reverse"):
            raise ValueError(f"direction must be 'forward' or 'reverse', got {direction!r}")

        capped_depth = min(depth, _MAX_DEPTH)

        resolved_edge_types: list[str] = (
            list(edge_types) if edge_types is not None else list(_DEFAULT_TRAVERSAL_EDGE_TYPES)
        )

        # Build bi-temporal SQL fragments for anchor and recursive branches.
        # The anchor branch uses bare column names; the recursive branch uses the
        # "e." alias to disambiguate from the CTE's own columns.
        tf_anchor, tf_params = self._temporal_sql_fragments(temporal_filter, as_of)
        tf_rec, _ = self._temporal_sql_fragments(temporal_filter, as_of, table_alias="e")
        # Both fragments share param names with identical values — safe to merge once.

        if direction == "forward":
            # Seed: root is the source; we follow outward to destinations.
            seed_where = "src_entity_id = :root_id"
            # Recursive join: the previously visited destination is the next source.
            rec_join = "e.src_entity_id = cte.member_entity_id"
            rec_member = "e.dst_entity_id"
        else:
            # Seed: root is the destination; we follow inward to sources.
            seed_where = "dst_entity_id = :root_id"
            # Recursive join: the previously visited source is the next destination.
            rec_join = "e.dst_entity_id = cte.member_entity_id"
            rec_member = "e.src_entity_id"

        sql = text(
            f"""
            WITH RECURSIVE cte AS (
                -- Anchor: immediate neighbours of root
                SELECT
                    edge_id,
                    tenant_id,
                    rel,
                    src_entity_id,
                    dst_entity_id,
                    CASE
                        WHEN '{direction}' = 'forward' THEN dst_entity_id
                        ELSE src_entity_id
                    END                         AS member_entity_id,
                    1                           AS depth,
                    ARRAY[edge_id]              AS edge_path,
                    ARRAY[rel]                  AS edge_rels
                FROM edges
                WHERE {seed_where}
                  AND tenant_id = :tid
                  AND rel = ANY(:edge_types)
                  AND {tf_anchor}

                UNION ALL

                -- Recursive: extend path by one more hop
                SELECT
                    e.edge_id,
                    e.tenant_id,
                    e.rel,
                    e.src_entity_id,
                    e.dst_entity_id,
                    {rec_member}                AS member_entity_id,
                    cte.depth + 1               AS depth,
                    cte.edge_path || e.edge_id  AS edge_path,
                    cte.edge_rels || e.rel      AS edge_rels
                FROM edges e
                JOIN cte ON {rec_join}
                WHERE e.tenant_id = :tid
                  AND e.rel = ANY(:edge_types)
                  AND cte.depth < :max_depth
                  AND {tf_rec}
            )
            SELECT DISTINCT ON (member_entity_id)
                member_entity_id,
                depth,
                edge_path,
                edge_rels
            FROM cte
            WHERE member_entity_id != :root_id
            ORDER BY member_entity_id, depth ASC
            LIMIT 10000
            """
        )

        params: dict[str, Any] = {
            "root_id": root_entity_id,
            "tid": tenant_id,
            "edge_types": resolved_edge_types,
            "max_depth": capped_depth,
            **tf_params,
        }

        result = await session.execute(sql, params)
        raw_rows = result.mappings().all()

        return [
            {
                "member_entity_id": row["member_entity_id"],
                "depth": row["depth"],
                "edge_path": list(row["edge_path"]),
                "edge_rels": list(row["edge_rels"]),
            }
            for row in raw_rows
            if row["member_entity_id"] != root_entity_id  # defense-in-depth
        ]

    async def traverse_for_closure_refresh(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        root_entity_id: uuid.UUID,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        temporal_filter: TemporalFilter,
        as_of: Any,
    ) -> list[dict[str, Any]]:
        """Public traversal entry point for background workers.

        This is the only surface the closure-refresh worker (and any other
        background job) should call when it needs raw CTE rows.  It delegates
        directly to ``_traverse_cte``; the private method remains the shared
        internal implementation used by the service's own read paths.

        Keeping this boundary explicit means that renaming, signature changes,
        or caching added to ``_traverse_cte`` will be caught at the call site
        rather than silently broken by a private-method refactor.

        Parameters and return value are identical to ``_traverse_cte``; see
        that method's docstring for full parameter descriptions.
        """
        return await self._traverse_cte(
            session=session,
            tenant_id=tenant_id,
            root_entity_id=root_entity_id,
            direction=direction,
            depth=depth,
            edge_types=edge_types,
            temporal_filter=temporal_filter,
            as_of=as_of,
        )

    # ------------------------------------------------------------------
    # Private — retrieval arms
    # ------------------------------------------------------------------

    async def _semantic_arm(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """ANN search via pgvector HNSW index.

        SET LOCAL hnsw.ef_search must run inside the same transaction as the
        SELECT (SET LOCAL is a no-op outside a transaction).
        """
        query_vec = await self._encode_query(q)
        ef_search = top_k * 4
        fetch_k = top_k * 4  # over-fetch before dedup

        now = self._clock.now()
        tf_sql, tf_params = self._temporal_sql_fragments(temporal_filter, now, table_alias="f")

        entity_filter = ""
        params: dict[str, Any] = {
            "tid": ctx.tenant_id,
            "vec": query_vec,
            "fetch_k": fetch_k,
            "ef_search": ef_search,
            **tf_params,
        }
        if entity_type is not None:
            entity_filter = "AND ent.entity_type = :entity_type"
            params["entity_type"] = entity_type

        sql = text(
            f"""
            SELECT
                emb.embedding_id,
                emb.claim_id AS fact_id,
                emb.tenant_id AS emb_tenant_id,
                f.entity_id,
                f.tenant_id AS fact_tenant_id,
                f.category,
                f.body,
                f.is_authoritative,
                f.is_authoritative_superseded,
                f.sync_run_id,
                f.t_valid_from,
                f.t_valid_to,
                f.t_ingested_at,
                f.t_invalidated_at,
                ent.entity_id AS ent_entity_id,
                ent.tenant_id AS ent_tenant_id,
                ent.entity_type,
                ent.name,
                ent.external_id,
                ent.is_active,
                ent.created_at,
                (emb.vector <=> CAST(:vec AS vector)) AS distance
            FROM embeddings emb
            JOIN facts f ON f.fact_id = emb.claim_id
            JOIN entities ent ON ent.entity_id = f.entity_id
            WHERE emb.tenant_id = :tid
              AND f.tenant_id = :tid
              AND ent.tenant_id = :tid
              AND ent.is_active = TRUE
              {entity_filter}
              AND {tf_sql}
            ORDER BY emb.vector <=> CAST(:vec AS vector)
            LIMIT :fetch_k
            """
        )

        # Must run inside an explicit transaction so SET LOCAL takes effect.
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SET LOCAL hnsw.ef_search = :v"),
                    {"v": ef_search},
                )
                result = await session.execute(sql, params)
                rows = result.mappings().all()

        return self._group_rows_by_entity(rows, top_k)

    async def _lexical_arm(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """Full-text search via tsvector @@ plainto_tsquery, ranked by ts_rank_cd."""
        now = self._clock.now()
        tf_sql, tf_params = self._temporal_sql_fragments(temporal_filter, now, table_alias="f")

        entity_filter = ""
        params: dict[str, Any] = {
            "tid": ctx.tenant_id,
            "query": q,
            "limit": top_k,
            **tf_params,
        }
        if entity_type is not None:
            entity_filter = "AND ent.entity_type = :entity_type"
            params["entity_type"] = entity_type

        sql = text(
            f"""
            SELECT
                f.fact_id,
                f.entity_id,
                f.tenant_id AS fact_tenant_id,
                f.category,
                f.body,
                f.is_authoritative,
                f.is_authoritative_superseded,
                f.sync_run_id,
                f.t_valid_from,
                f.t_valid_to,
                f.t_ingested_at,
                f.t_invalidated_at,
                ent.entity_id AS ent_entity_id,
                ent.tenant_id AS ent_tenant_id,
                ent.entity_type,
                ent.name,
                ent.external_id,
                ent.is_active,
                ent.created_at,
                ts_rank_cd(f.ts_vector, plainto_tsquery('english', :query)) AS rank
            FROM facts f
            JOIN entities ent ON ent.entity_id = f.entity_id
            WHERE f.tenant_id = :tid
              AND ent.tenant_id = :tid
              AND ent.is_active = TRUE
              AND f.ts_vector @@ plainto_tsquery('english', :query)
              {entity_filter}
              AND {tf_sql}
            ORDER BY rank DESC
            LIMIT :limit
            """
        )

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return self._group_rows_by_entity(rows, top_k)

    async def _graph_arm(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """Graph-neighbour expansion via recursive CTE.

        Starting from entities whose names match the query text (lexical match),
        expand outward via graph edges up to _SEARCH_GRAPH_DEPTH hops.
        Returns entity-level rows for the neighbour entities.
        """
        now = self._clock.now()
        tf_fact_sql, tf_fact_params = self._temporal_sql_fragments(temporal_filter, now, table_alias="f")
        tf_edge_sql, tf_edge_params = self._temporal_sql_fragments(temporal_filter, now, table_alias="e")

        # De-duplicate param keys from two temporal fragments by renaming one set.
        tf_edge_params_renamed = {f"edge_{k}": v for k, v in tf_edge_params.items()}
        tf_edge_sql_renamed = tf_edge_sql
        for k in tf_edge_params:
            tf_edge_sql_renamed = tf_edge_sql_renamed.replace(f":{k}", f":edge_{k}")

        entity_filter = ""
        params: dict[str, Any] = {
            "tid": ctx.tenant_id,
            "query": f"%{q}%",
            "edge_types": list(_GRAPH_EDGE_TYPES),
            "limit": top_k,
            **tf_fact_params,
            **tf_edge_params_renamed,
        }
        if entity_type is not None:
            entity_filter = "AND ent.entity_type = :entity_type"
            params["entity_type"] = entity_type

        sql = text(
            f"""
            WITH RECURSIVE graph_cte AS (
                -- Seed: entities matching query text
                SELECT
                    ent.entity_id,
                    ent.tenant_id,
                    ent.entity_type,
                    ent.name,
                    ent.external_id,
                    ent.is_active,
                    ent.created_at,
                    0 AS depth_counter
                FROM entities ent
                WHERE ent.tenant_id = :tid
                  AND ent.is_active = TRUE
                  AND ent.name ILIKE :query
                  {entity_filter}

                UNION

                SELECT
                    ent2.entity_id,
                    ent2.tenant_id,
                    ent2.entity_type,
                    ent2.name,
                    ent2.external_id,
                    ent2.is_active,
                    ent2.created_at,
                    graph_cte.depth_counter + 1
                FROM graph_cte
                JOIN edges e ON e.src_entity_id = graph_cte.entity_id
                JOIN entities ent2 ON ent2.entity_id = e.dst_entity_id
                WHERE e.tenant_id = :tid
                  AND ent2.tenant_id = :tid
                  AND ent2.is_active = TRUE
                  AND e.rel = ANY(:edge_types)
                  AND graph_cte.depth_counter < :search_depth
                  AND {tf_edge_sql_renamed}
            )
            SELECT DISTINCT ON (g.entity_id)
                g.entity_id,
                g.tenant_id AS ent_tenant_id,
                g.entity_type,
                g.name,
                g.external_id,
                g.is_active,
                g.created_at,
                f.fact_id,
                f.entity_id AS f_entity_id,
                f.tenant_id AS fact_tenant_id,
                f.category,
                f.body,
                f.is_authoritative,
                f.is_authoritative_superseded,
                f.sync_run_id,
                f.t_valid_from,
                f.t_valid_to,
                f.t_ingested_at,
                f.t_invalidated_at
            FROM graph_cte g
            LEFT JOIN facts f ON f.entity_id = g.entity_id
              AND f.tenant_id = :tid
              AND {tf_fact_sql}
            ORDER BY g.entity_id, g.depth_counter
            LIMIT :limit
            """
        )
        params["search_depth"] = _SEARCH_GRAPH_DEPTH

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return self._group_rows_by_entity(rows, top_k)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _temporal_sql_fragments(
        temporal_filter: TemporalFilter,
        now: Any,
        table_alias: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Build SQL WHERE fragment + params for bi-temporal filter.

        Fragments do not start with AND; callers add connectives.
        Columns are prefixed with table_alias if provided.
        """
        prefix = f"{table_alias}." if table_alias else ""
        params: dict[str, Any] = {}
        clauses: list[str] = []

        if temporal_filter.as_of is not None:
            as_of = temporal_filter.as_of
            spec = build_as_of_filter(as_of)
            clauses.append(f"{prefix}t_valid_from <= :tf_valid_from")
            params["tf_valid_from"] = spec["t_valid_from"][1]
            clauses.append(f"({prefix}t_valid_to IS NULL OR {prefix}t_valid_to > :tf_valid_to)")
            params["tf_valid_to"] = spec["t_valid_to"][1]
            clauses.append(f"({prefix}t_invalidated_at IS NULL OR {prefix}t_invalidated_at > :tf_invalidated_at)")
            params["tf_invalidated_at"] = spec["t_invalidated_at"][1]
        else:
            # t_invalidated_at IS NULL
            clauses.append(f"{prefix}t_invalidated_at IS NULL")
            # t_valid_to IS NULL OR t_valid_to > now
            clauses.append(f"({prefix}t_valid_to IS NULL OR {prefix}t_valid_to > :tf_now)")
            params["tf_now"] = now

        return " AND ".join(clauses), params

    @staticmethod
    def _group_rows_by_entity(
        rows: Any,
        top_k: int,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """Group flat result rows into (entity_id, EntityRef, [FactRef]) tuples.

        Preserves original row order for ranking; deduplicates entity_id.
        """
        seen: dict[uuid.UUID, tuple[EntityRef, list[FactRef]]] = {}
        order: list[uuid.UUID] = []

        for row in rows:
            eid = row["entity_id"]
            if eid not in seen:
                entity_ref = EntityRef(
                    entity_id=eid,
                    tenant_id=row["ent_tenant_id"],
                    entity_type=row["entity_type"],
                    name=row["name"],
                    external_id=row["external_id"],
                    is_active=row["is_active"],
                    created_at=row["created_at"],
                )
                seen[eid] = (entity_ref, [])
                order.append(eid)

            # Attach fact if present (LEFT JOIN may return NULLs).
            if row.get("fact_id") is not None:
                fact_ref = FactRef(
                    fact_id=row["fact_id"],
                    tenant_id=row["fact_tenant_id"],
                    entity_id=eid,
                    category=row["category"],
                    body=row["body"],
                    is_authoritative=row["is_authoritative"],
                    is_authoritative_superseded=row["is_authoritative_superseded"],
                    sync_run_id=row["sync_run_id"],
                    t_valid_from=row["t_valid_from"],
                    t_valid_to=row["t_valid_to"],
                    t_ingested_at=row["t_ingested_at"],
                    t_invalidated_at=row["t_invalidated_at"],
                )
                seen[eid][1].append(fact_ref)

        return [(eid, seen[eid][0], seen[eid][1]) for eid in order[:top_k]]


__all__ = [
    "RetrievalService",
    "_DEFAULT_TRAVERSAL_EDGE_TYPES",
    "_TRAVERSAL_EXCLUDED_RELS",
    "_ALL_VOCAB_RELS",
    "_MAX_DEPTH",
    "_CACHE_HORIZON_DAYS",
]
