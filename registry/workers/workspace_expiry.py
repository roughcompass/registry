"""WorkspaceExpiryWorker — batched soft-invalidation of expired workspace entries.

Workspace entries may carry an ``expires_at`` timestamp for ephemeral content
(time-bounded context windows, short-lived scratchpads, etc.).  Once that
timestamp passes the entry is semantically expired but physically retained so
the audit trail, RTBF path, and any downstream references remain intact.

This worker sets ``t_invalidated_at = now()`` on expired entries in batches
of 1000, looping until no eligible rows remain.  The ``t_invalidated_at IS NULL``
filter makes every pass idempotent: rows already soft-invalidated are excluded
automatically, so re-running after a partial failure is safe.

Audit note
----------
The worker runs across all tenants in a single pass.  Because there is no
per-request TenantContext here, audit rows are written with a synthetic system
actor (nil UUIDs) so every invalidation is traceable without attributing it to
a real actor.  The ``after`` payload records the batch count and timestamp,
giving operators a clear record of how many entries were expired per run.

Physical deletion is explicitly out of scope for this worker.  Entries remain
in the table until RTBF (the physical-purge path in WorkspaceService) or an
admin hard-purge explicitly targets them.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api import audit as audit_emit
from registry.audit import actions
from registry.types import Clock, SystemClock, TenantContext

_log = logging.getLogger(__name__)

# Rows processed per UPDATE pass.  1000 balances throughput against lock hold
# time; a single long-running UPDATE on a large table can block concurrent reads.
_BATCH_SIZE: int = 1000

# Synthetic system actor used when writing audit rows from background workers.
# A nil UUID is the conventional "system" marker — it cannot collide with a real
# actor created via the REST surface (which uses gen_random_uuid()).
_SYSTEM_TENANT_ID = uuid.UUID(int=0)
_SYSTEM_ACTOR_ID = uuid.UUID(int=0)

_SYSTEM_CTX = TenantContext(
    tenant_id=_SYSTEM_TENANT_ID,
    actor_id=_SYSTEM_ACTOR_ID,
    roles=["system"],
)


@dataclass(frozen=True)
class ExpiryResult:
    """Outcome of a full expiry worker run.

    Attributes
    ----------
    expired_count:
        Total number of entries soft-invalidated across all batches.
    batch_ts:
        The UTC timestamp used as ``t_invalidated_at`` for this run.
        All entries invalidated in one ``run()`` call share the same
        timestamp so the audit trail is easy to correlate.
    """

    expired_count: int
    batch_ts: datetime.datetime


class WorkspaceExpiryWorker:
    """Daily worker that soft-invalidates workspace entries past their expires_at.

    Constructor parameters
    ----------------------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock for deterministic testing.  Defaults to the real
        UTC wall-clock when ``None``.
    batch_size:
        Rows processed per UPDATE pass.  Kept small to avoid holding an
        exclusive lock on a large portion of ``workspace_entries``.

    Run pattern
    -----------
    Call ``await worker.run()`` from the maintenance scheduler (daily or
    hourly — see main.py scheduler registration).  Returns an
    :class:`ExpiryResult` with the total invalidated count and the
    timestamp used for ``t_invalidated_at``.

    The query pattern is::

        UPDATE workspace_entries
        SET    t_invalidated_at = :now
        WHERE  expires_at < :now
          AND  t_invalidated_at IS NULL
        LIMIT  :batch_size
        RETURNING entry_id

    Looped until the RETURNING result is empty (0 rows → all eligible
    entries have been processed).

    Scheduler registration
    ----------------------
    Register in the existing scheduler in ``registry/registry/main.py``
    alongside the webhook drain and audit partition check jobs::

        from registry.workers.workspace_expiry import WorkspaceExpiryWorker

        expiry_worker = WorkspaceExpiryWorker(
            session_factory=session_factory,
            clock=clock,
        )

        async def _expire_workspace_entries() -> None:
            try:
                result = await expiry_worker.run()
                _log.info(
                    "workspace_expiry.run: expired=%d batch_ts=%s",
                    result.expired_count,
                    result.batch_ts,
                )
            except Exception as exc:
                _log.warning("workspace_expiry_run: %s", exc)

        scheduler.add_job(
            _expire_workspace_entries,
            trigger="interval",
            hours=1,
            max_instances=1,
            coalesce=True,
            id="workspace_expiry",
            replace_existing=True,
        )
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._batch_size = batch_size

    async def run(self) -> ExpiryResult:
        """Soft-invalidate all expired workspace entries.

        Processes rows in batches of ``self._batch_size`` to avoid long-held
        locks.  Each batch is committed independently; a crash mid-run leaves
        already-processed rows invalidated and picks up the remainder on the
        next scheduled run (idempotent by construction — ``t_invalidated_at IS
        NULL`` excludes already-processed rows).

        An audit row is written per batch so large invalidation events are
        traceable in the audit log without a single record per expired entry.

        Returns
        -------
        ExpiryResult
            Total count of entries invalidated and the UTC timestamp shared
            across all batches in this run.
        """
        now = self._clock.now()
        total_expired = 0

        while True:
            batch_count = await self._invalidate_batch(now)
            if batch_count == 0:
                break

            total_expired += batch_count
            _log.info(
                "workspace_expiry: batch expired=%d running_total=%d ts=%s",
                batch_count,
                total_expired,
                now,
            )
            await self._emit_audit(now=now, count=batch_count)

        _log.info(
            "workspace_expiry.run: complete expired=%d batch_ts=%s",
            total_expired,
            now,
        )
        return ExpiryResult(expired_count=total_expired, batch_ts=now)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _invalidate_batch(self, now: datetime.datetime) -> int:
        """Run one UPDATE batch and return the number of rows affected.

        Uses a CTE-based UPDATE with LIMIT so Postgres processes at most
        ``batch_size`` rows per statement.  The ``t_invalidated_at IS NULL``
        predicate ensures idempotency across retries.
        """
        sql = text(
            """
            WITH candidates AS (
                SELECT entry_id
                FROM   workspace_entries
                WHERE  expires_at < :now
                  AND  t_invalidated_at IS NULL
                LIMIT  :batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE workspace_entries we
            SET    t_invalidated_at = :now
            FROM   candidates
            WHERE  we.entry_id = candidates.entry_id
            RETURNING we.entry_id
            """
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                sql,
                {"now": now, "batch_size": self._batch_size},
            )
            rows = result.fetchall()
        return len(rows)

    async def _emit_audit(self, *, now: datetime.datetime, count: int) -> None:
        """Write one audit row for a completed invalidation batch.

        Failure is swallowed by ``api.audit.emit`` — the metric counter
        ``catalog_audit_write_failures_total`` increments if the write fails,
        so monitoring catches drift without blocking the expiry run.
        """
        await audit_emit.emit(
            self._session_factory,
            _SYSTEM_CTX,
            self._clock,
            action=actions.WORKSPACE_ENTRY_EXPIRED,
            target_type="workspace_entries",
            target_id=_SYSTEM_TENANT_ID,
            after={"count": count, "batch_ts": now.isoformat()},
        )


__all__ = ["ExpiryResult", "WorkspaceExpiryWorker"]
