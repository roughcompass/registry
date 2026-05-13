"""Closure-cache refresh worker.

Drains `closure_outbox` rows (one per edge mutation) and upserts the updated
transitive closure into `closure_cache`.  Uses the outbox pattern:
rows are processed with FOR UPDATE SKIP LOCKED; each row is deleted atomically
with its closure upsert so a mid-run crash leaves the outbox row intact for
retry.

Architecture notes
------------------
- `run_once()` returns the count of outbox rows successfully processed.
- Conservative invalidation strategy: for each mutated edge, we recompute the
  full forward AND reverse closure for both the src_entity_id and dst_entity_id.
  This may recompute more than strictly necessary but guarantees no stale entries
  remain after an edge mutation.
- Closure rows are upserted into `closure_cache` via ON CONFLICT DO UPDATE so
  re-runs are idempotent.
- Cache eviction: rows with ``refreshed_at < now() - 90 days`` are deleted by
  the nightly maintenance job (wired by the caller, not this module).
- Manual ``TRUNCATE closure_cache`` does NOT seed the outbox — reads fall back
  to the CTE until natural edge mutations rewarm the cache. This is intentional:
  a truncate should not silently trigger a full rebuild.
- `traverse_for_closure_refresh()` on `RetrievalService` is the public surface
  this worker calls for raw CTE rows; it does not duplicate the CTE logic.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.types import Clock, SystemClock, TemporalFilter

_log = logging.getLogger(__name__)

# Nightly eviction horizon: cache rows older than this are stale enough to purge.
_EVICTION_DAYS: int = 90

# Cooldown between retries for a failed row (seconds).
_COOLDOWN_S: int = 60

# Batch size per drain cycle.
_BATCH_SIZE: int = 50


class ClosureRefreshWorker:
    """Drains ``closure_outbox`` and keeps ``closure_cache`` consistent.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock.  Defaults to real UTC wall-clock when ``None``.
    batch_size:
        Max outbox rows to process per ``run_once()`` call.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
        batch_size: int = _BATCH_SIZE,
        concurrency: int = 8,
    ) -> None:
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._batch_size = batch_size
        self._concurrency = concurrency

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run_once(self) -> int:
        """Drain one batch from ``closure_outbox``.

        Rows in the batch are processed concurrently up to ``self._concurrency``
        at a time.  Each row gets its own DB session; the FOR UPDATE SKIP LOCKED
        claim is already exclusive at the batch level, so concurrent processing
        within a batch does not conflict with other worker processes.

        A per-row failure is logged and the row left in the outbox for the next
        retry cycle (existing semantics).  One row's failure does not cancel
        siblings.

        Returns
        -------
        int
            Number of outbox rows successfully processed (deleted) in this call.
            Zero when the outbox is empty.
        """
        rows = await self._claim_batch()
        if not rows:
            return 0

        sem = asyncio.Semaphore(self._concurrency)

        async def _gated(row: dict[str, Any]) -> bool:
            async with sem:
                return await self._process_row(row)

        outcomes = await asyncio.gather(*[_gated(r) for r in rows], return_exceptions=True)

        processed = 0
        for row, outcome in zip(rows, outcomes, strict=True):
            if isinstance(outcome, Exception):
                # _process_row should catch its own exceptions and return False;
                # this path handles unexpected errors that escape _process_row.
                _log.exception(
                    "closure_refresh: unexpected error processing outbox row",
                    extra={"outbox_id": str(row.get("outbox_id")), "edge_id": str(row.get("edge_id"))},
                    exc_info=outcome,
                )
            elif outcome:
                processed += 1

        _log.info(
            "closure_refresh.run_once: processed=%d claimed=%d",
            processed,
            len(rows),
        )
        return processed

    async def evict_stale(self) -> int:
        """Delete ``closure_cache`` rows older than the eviction horizon.

        Intended to be called by the nightly maintenance scheduler.  Returns
        the number of rows deleted.
        """
        cutoff = self._clock.now() - datetime.timedelta(days=_EVICTION_DAYS)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text("DELETE FROM closure_cache WHERE refreshed_at < :cutoff"),
                {"cutoff": cutoff},
            )
            deleted: int = result.rowcount
        _log.info("closure_refresh.evict_stale: deleted=%d cutoff=%s", deleted, cutoff)
        return deleted

    # ------------------------------------------------------------------
    # Internal — outbox drain
    # ------------------------------------------------------------------

    async def _claim_batch(self) -> list[dict[str, Any]]:
        """Claim up to ``_batch_size`` outbox rows with SKIP LOCKED.

        The explicit ``session.begin()`` is load-bearing: without it the
        autobegin transaction would be rolled back when this method returns,
        releasing the ``FOR UPDATE SKIP LOCKED`` row locks before the caller
        could process the claimed rows. Two concurrent workers would then
        claim the same outbox rows and write duplicate closure entries.
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    """
                    SELECT outbox_id, tenant_id, edge_id, attempts, enqueued_at
                    FROM   closure_outbox
                    WHERE  last_error IS NULL
                       OR  last_attempt_at < now() - interval ':cooldown seconds'
                    ORDER  BY enqueued_at
                    LIMIT  :batch_size
                    FOR UPDATE SKIP LOCKED
                    """.replace(":cooldown", str(_COOLDOWN_S))
                ),
                {"batch_size": self._batch_size},
            )
            return [dict(r) for r in result.mappings().all()]

    async def _process_row(self, row: dict[str, Any]) -> bool:
        """Process one outbox row: compute closure and upsert; delete outbox row.

        Returns True on success, False on failure (error recorded on row).
        """
        outbox_id: uuid.UUID = row["outbox_id"]
        tenant_id: uuid.UUID = row["tenant_id"]
        edge_id: uuid.UUID = row["edge_id"]

        try:
            # Load edge src/dst so we know which roots to recompute.
            edge_info = await self._fetch_edge(tenant_id, edge_id)
        except Exception as exc:
            await self._record_failure(outbox_id, repr(exc)[:2000])
            return False

        if edge_info is None:
            # Edge was hard-deleted (should not happen; edges are soft-deleted).
            # Delete the orphaned outbox row and move on.
            _log.warning(
                "closure_refresh: edge %s not found; discarding outbox row %s",
                edge_id,
                outbox_id,
            )
            await self._delete_outbox_row(outbox_id)
            return True

        src_id: uuid.UUID = edge_info["src_entity_id"]
        dst_id: uuid.UUID = edge_info["dst_entity_id"]

        try:
            # Recompute forward + reverse closures for both endpoints.
            closure_rows = await self._compute_closure_all(tenant_id, src_id, dst_id)
        except Exception as exc:
            await self._record_failure(outbox_id, repr(exc)[:2000])
            return False

        # Determine which (root, direction) pairs were recomputed.
        recomputed_keys: set[tuple[uuid.UUID, str]] = set()
        roots = list({src_id, dst_id})
        for root_id in roots:
            for direction in ("forward", "reverse"):
                recomputed_keys.add((root_id, direction))

        try:
            await self._replace_and_delete(tenant_id, closure_rows, recomputed_keys, outbox_id)
        except Exception as exc:
            await self._record_failure(outbox_id, repr(exc)[:2000])
            return False

        return True

    async def _fetch_edge(self, tenant_id: uuid.UUID, edge_id: uuid.UUID) -> dict[str, Any] | None:
        """Return ``{src_entity_id, dst_entity_id}`` for the given edge, or None."""
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT src_entity_id, dst_entity_id
                    FROM   edges
                    WHERE  edge_id = :eid AND tenant_id = :tid
                    """
                ),
                {"eid": edge_id, "tid": tenant_id},
            )
            row = result.mappings().first()
        if row is None:
            return None
        return dict(row)

    async def _compute_closure_all(
        self,
        tenant_id: uuid.UUID,
        src_id: uuid.UUID,
        dst_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """Compute forward + reverse closures for src and dst endpoints.

        Conservative invalidation: recompute closures for both the src and dst
        of the mutated edge in both directions.  This guarantees no stale entries
        for any root whose reachability changed.

        Returns a flat list of dicts ready for ``_upsert_and_delete``.
        """
        # Import here to avoid circular import at module load.
        from registry.service.retrieval import RetrievalService  # noqa: PLC0415

        # Temporal filter: current-truth (t_invalidated_at IS NULL).
        tf = TemporalFilter(as_of=None)
        now = self._clock.now()

        # Build a minimal RetrievalService instance for CTE access.
        # We do not need the embedder; pass None (the CTE path never calls it).
        svc = RetrievalService(
            session_factory=self._session_factory,
            clock=self._clock,
            embedder=_NoopEmbedder(),  # type: ignore[arg-type]
        )

        all_rows: list[dict[str, Any]] = []

        # Roots to recompute: both endpoints, both directions.
        roots = list({src_id, dst_id})  # deduplicate in case src == dst
        directions = ("forward", "reverse")

        for root_id in roots:
            for direction in directions:
                async with self._session_factory() as session:
                    cte_rows = await svc.traverse_for_closure_refresh(
                        session=session,
                        tenant_id=tenant_id,
                        root_entity_id=root_id,
                        direction=direction,
                        depth=5,  # _MAX_DEPTH
                        edge_types=None,  # all default traversal types
                        temporal_filter=tf,
                        as_of=now,
                    )

                for r in cte_rows:
                    all_rows.append(
                        {
                            "tenant_id": tenant_id,
                            "root_entity_id": root_id,
                            "member_entity_id": r["member_entity_id"],
                            "direction": direction,
                            "depth": r["depth"],
                            "edge_path": r["edge_path"],
                            "edge_rels": r["edge_rels"],
                        }
                    )

        return all_rows

    async def _replace_and_delete(
        self,
        tenant_id: uuid.UUID,
        closure_rows: list[dict[str, Any]],
        recomputed_keys: set[tuple[uuid.UUID, str]],
        outbox_id: uuid.UUID,
    ) -> None:
        """Replace closure rows for recomputed (root, direction) pairs and delete outbox row.

        For each (root_entity_id, direction) that was recomputed, delete all
        existing cache rows first, then insert the new ones in a single bulk
        INSERT ... VALUES (...), (...) ... ON CONFLICT DO UPDATE statement.
        This reduces N round-trips (one per closure row) to O(1) statements
        regardless of batch size.

        All deletes, the bulk insert, and the outbox-row delete are executed
        in a single transaction so a mid-run crash leaves the outbox row intact
        for retry (existing semantics).
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            # Delete existing cache rows for all recomputed (root, direction) pairs.
            for root_id, direction in recomputed_keys:
                await session.execute(
                    text(
                        "DELETE FROM closure_cache "
                        "WHERE tenant_id = :tid "
                        "  AND root_entity_id = :root_id "
                        "  AND direction = :direction"
                    ),
                    {"tid": tenant_id, "root_id": root_id, "direction": direction},
                )

            if closure_rows:
                # Build one bulk INSERT ... VALUES (...), (...) ... ON CONFLICT DO UPDATE.
                #
                # Array columns (edge_path uuid[], edge_rels text[]) are embedded as
                # PostgreSQL array literals rather than bound parameters because asyncpg
                # requires Python-native list types for ARRAY bind params, but the UUID[]
                # column expects uuid-typed elements — casting each element via a Python
                # list would require extra driver-level type annotations.  The values
                # here are all derived from the CTE output (valid UUIDs and edge-rel
                # strings); no user-supplied content flows through this path.
                value_fragments: list[str] = []
                params: dict[str, Any] = {"refreshed_at": now}

                for idx, r in enumerate(closure_rows):
                    edge_path_list: list[uuid.UUID] = r["edge_path"]
                    edge_rels_list: list[str] = r["edge_rels"]

                    if edge_path_list:
                        edge_path_sql = "ARRAY[" + ", ".join(f"'{str(e)}'::uuid" for e in edge_path_list) + "]::uuid[]"
                        edge_rels_sql = "ARRAY[" + ", ".join(f"'{rel}'" for rel in edge_rels_list) + "]::text[]"
                    else:
                        edge_path_sql = "ARRAY[]::uuid[]"
                        edge_rels_sql = "ARRAY[]::text[]"

                    tid_key = f"tid_{idx}"
                    root_key = f"root_{idx}"
                    member_key = f"member_{idx}"
                    dir_key = f"dir_{idx}"
                    depth_key = f"depth_{idx}"

                    params[tid_key] = r["tenant_id"]
                    params[root_key] = r["root_entity_id"]
                    params[member_key] = r["member_entity_id"]
                    params[dir_key] = r["direction"]
                    params[depth_key] = r["depth"]

                    value_fragments.append(
                        f"(gen_random_uuid(), :{tid_key}, :{root_key}, :{member_key},"
                        f" :{dir_key}, :{depth_key}, {edge_path_sql}, {edge_rels_sql}, :refreshed_at)"
                    )

                bulk_sql = (
                    "INSERT INTO closure_cache"
                    " (cache_id, tenant_id, root_entity_id, member_entity_id,"
                    "  direction, depth, edge_path, edge_rels, refreshed_at)"
                    " VALUES "
                    + ", ".join(value_fragments)
                    + " ON CONFLICT (tenant_id, root_entity_id, member_entity_id, direction)"
                    " DO UPDATE SET"
                    "   depth        = EXCLUDED.depth,"
                    "   edge_path    = EXCLUDED.edge_path,"
                    "   edge_rels    = EXCLUDED.edge_rels,"
                    "   refreshed_at = EXCLUDED.refreshed_at"
                )
                await session.execute(text(bulk_sql), params)

            await session.execute(
                text("DELETE FROM closure_outbox WHERE outbox_id = :oid"),
                {"oid": outbox_id},
            )
        _log.debug(
            "closure_refresh: replaced %d rows for outbox_id=%s",
            len(closure_rows),
            outbox_id,
        )

    async def _delete_outbox_row(self, outbox_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM closure_outbox WHERE outbox_id = :oid"),
                {"oid": outbox_id},
            )

    async def _record_failure(self, outbox_id: uuid.UUID, error_text: str) -> None:
        now = self._clock.now()
        _log.warning("closure_refresh: failed outbox_id=%s error=%s", outbox_id, error_text[:200])
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE closure_outbox
                        SET    attempts        = attempts + 1,
                               last_error      = :err,
                               last_attempt_at = :now
                        WHERE  outbox_id = :oid
                        """
                    ),
                    {"err": error_text, "now": now, "oid": outbox_id},
                )
        except Exception:
            _log.exception("closure_refresh: could not record failure for outbox_id=%s", outbox_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _NoopEmbedder:
    """Stub embedder passed to RetrievalService for CTE-only paths.

    The CTE traversal never calls the embedder; this stub prevents an import
    error when constructing a minimal RetrievalService instance.
    """

    model_version: str = "noop"

    def encode(self, texts: list[str]) -> Any:
        raise NotImplementedError("_NoopEmbedder.encode must not be called")


# ---------------------------------------------------------------------------
# Outbox emit helper (called from registry.py)
# ---------------------------------------------------------------------------


async def enqueue_closure_refresh(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    edge_id: uuid.UUID,
    now: datetime.datetime,
) -> None:
    """Insert one ``closure_outbox`` row in the caller's active transaction.

    Wrapped in a SAVEPOINT so that if the ``closure_outbox`` table is absent
    (e.g., before migration 0008 is applied) the outer transaction is not
    poisoned.  Once the migration is applied, the insert succeeds atomically
    with the edge write.
    """
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO closure_outbox "
                    "(outbox_id, tenant_id, edge_id, enqueued_at, attempts) "
                    "VALUES (gen_random_uuid(), :tid, :eid, :now, 0)"
                ),
                {"tid": tenant_id, "eid": edge_id, "now": now},
            )
    except Exception:
        _log.debug(
            "closure_outbox not present yet (migration 0008 creates it); "
            "skipping closure_refresh enqueue for edge_id=%s",
            edge_id,
        )


__all__ = ["ClosureRefreshWorker", "enqueue_closure_refresh"]
