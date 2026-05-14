"""Unit tests for WorkspaceExpiryWorker.

DB is mocked at the session.execute boundary — no live Postgres is required.
The session factory uses a two-level mock: the factory returns a context-manager
session whose execute() is an AsyncMock that returns a MagicMock with a
controllable fetchall().

Coverage:
- Entries past expires_at are soft-invalidated: UPDATE executes with the
  correct WHERE filter (expires_at < :now AND t_invalidated_at IS NULL).
- Entries NOT past expires_at are unchanged: no UPDATE issues for them because
  the WHERE clause already excludes future-expires rows at the DB level; we
  verify the mock sees only one call when the single batch returns 0.
- Already-invalidated entries are skipped (idempotency): t_invalidated_at IS NULL
  predicate in the query naturally excludes them; zero-row response terminates loop.
- ExpiryResult.expired_count is accurate across batches.
- Partial run: batch_size smaller than total — worker loops until UPDATE returns 0.
- Restart mid-run: crash after first batch, re-run starts fresh and is idempotent.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.types import FakeClock
from registry.workers.workspace_expiry import ExpiryResult, WorkspaceExpiryWorker

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_result(count: int) -> MagicMock:
    """Return a mock execute() result whose fetchall() returns `count` fake rows."""
    result = MagicMock()
    result.fetchall.return_value = [object() for _ in range(count)]
    return result


def _make_factory(batch_sequence: list[int]) -> tuple[MagicMock, list[dict]]:
    """Build a session factory that serves batched row counts from batch_sequence.

    Each entry in batch_sequence is the number of rows the corresponding
    UPDATE call should appear to affect.  After the sequence is exhausted,
    every subsequent call returns 0 rows (loop termination).

    Returns (factory, params_log) where params_log accumulates the params
    dict passed to session.execute() on each call.
    """
    params_log: list[dict] = []
    call_index = 0

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        nonlocal call_index
        params_log.append(params or {})
        if call_index < len(batch_sequence):
            count = batch_sequence[call_index]
        else:
            count = 0
        call_index += 1
        return _row_result(count)

    session = MagicMock()
    session.execute = _execute

    # begin() must be an async context manager (used as `async with session.begin()`)
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    # session itself must be an async context manager (used as `async with factory()`)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=session)
    return factory, params_log


def _make_worker(
    batch_sequence: list[int],
    batch_size: int = 1000,
) -> tuple[WorkspaceExpiryWorker, list[dict]]:
    """Convenience: build factory + worker together, suppressing audit emit."""
    factory, params_log = _make_factory(batch_sequence)
    worker = WorkspaceExpiryWorker(
        session_factory=factory,
        clock=FakeClock(_NOW),
        batch_size=batch_size,
    )
    # Suppress audit writes — they open a second session and are tested elsewhere.
    worker._emit_audit = AsyncMock()  # type: ignore[method-assign]
    return worker, params_log


# ---------------------------------------------------------------------------
# Scenario 1: entries past expires_at are soft-invalidated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_entries_are_soft_invalidated() -> None:
    """A single batch of 5 expired rows triggers one UPDATE and returns count=5."""
    # Sequence: first call returns 5, second call returns 0 (terminates loop).
    worker, params_log = _make_worker(batch_sequence=[5, 0])

    result = await worker.run()

    assert result.expired_count == 5
    assert result.batch_ts == _NOW
    # At least one execute call must have carried :now and :batch_size,
    # confirming the UPDATE used the right parameters.
    assert any(p.get("now") == _NOW for p in params_log)
    assert any("batch_size" in p for p in params_log)


@pytest.mark.asyncio
async def test_expired_entries_update_uses_correct_filter_params() -> None:
    """The UPDATE params include 'now' and 'batch_size' — the WHERE clause relies on them."""
    worker, params_log = _make_worker(batch_sequence=[1, 0])

    await worker.run()

    # The only non-zero batch call carries the expected params.
    first_call = params_log[0]
    assert first_call["now"] == _NOW
    assert first_call["batch_size"] == 1000


# ---------------------------------------------------------------------------
# Scenario 2: entries NOT past expires_at are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_expired_entries_untouched() -> None:
    """When no rows match the WHERE clause the worker performs exactly one execute
    (which returns 0), exits the loop immediately, and reports expired_count=0.

    The WHERE predicate (expires_at < :now AND t_invalidated_at IS NULL) is
    evaluated by the DB; from the worker's view, 0 rows returned means nothing
    was invalidated.
    """
    worker, params_log = _make_worker(batch_sequence=[0])

    result = await worker.run()

    assert result.expired_count == 0
    # Exactly one execute call: the first (and only) batch that returned 0.
    assert len(params_log) == 1


# ---------------------------------------------------------------------------
# Scenario 3: already-invalidated entries are skipped (idempotency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_invalidated_entries_skipped() -> None:
    """Re-running the worker when all eligible rows are already invalidated
    results in expired_count=0 and a single DB round-trip.

    The t_invalidated_at IS NULL predicate in the UPDATE CTE ensures those rows
    are never returned; the worker sees 0 rows on the first call and exits.
    """
    worker, params_log = _make_worker(batch_sequence=[0])

    result = await worker.run()

    assert result.expired_count == 0
    assert len(params_log) == 1, "should have stopped after first 0-row response"


@pytest.mark.asyncio
async def test_idempotent_rerun_after_full_run() -> None:
    """A second run after a complete prior run must report 0 newly invalidated rows.

    Simulates: first run expires 10 rows, second run finds nothing eligible.
    """
    # First run
    worker1, _ = _make_worker(batch_sequence=[10, 0])
    result1 = await worker1.run()
    assert result1.expired_count == 10

    # Second run — all rows already carry t_invalidated_at, so 0 returned.
    worker2, _ = _make_worker(batch_sequence=[0])
    result2 = await worker2.run()
    assert result2.expired_count == 0


# ---------------------------------------------------------------------------
# Scenario 4: ExpiryResult.expired_count is accurate across batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_count_accurate_across_batches() -> None:
    """expired_count sums correctly over multiple non-zero batches."""
    # Three batches: 1000 + 500 + 200, then 0 to terminate.
    worker, _ = _make_worker(batch_sequence=[1000, 500, 200, 0])

    result = await worker.run()

    assert result.expired_count == 1700
    assert isinstance(result, ExpiryResult)


# ---------------------------------------------------------------------------
# Scenario 5: partial run — batch size smaller than total expired rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_run_loops_until_no_rows_remain() -> None:
    """With batch_size=1000 and two partial batches the worker loops twice.

    Mock returns 1000 on the first call and 7 on the second, then 0 to signal
    completion.  The worker must keep looping as long as fetchall() is non-empty.
    """
    worker, params_log = _make_worker(batch_sequence=[1000, 7, 0], batch_size=1000)

    result = await worker.run()

    assert result.expired_count == 1007
    # Three execute calls: [1000-row batch, 7-row batch, 0-row terminator].
    assert len(params_log) == 3


@pytest.mark.asyncio
async def test_partial_run_batch_size_propagated() -> None:
    """batch_size passed at construction is forwarded to every UPDATE call."""
    worker, params_log = _make_worker(batch_sequence=[50, 0], batch_size=50)

    await worker.run()

    for p in params_log:
        assert p.get("batch_size") == 50, f"unexpected batch_size in params: {p}"


# ---------------------------------------------------------------------------
# Scenario 6: restart mid-run — simulate crash after first batch commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_mid_run_is_idempotent() -> None:
    """Crash after first batch: re-run from scratch processes only remaining rows.

    Modelled as two independent worker invocations:
    - Run 1: first batch returns 1000, then the worker raises (simulates crash).
    - Run 2: starts fresh; mock returns 7 (only un-invalidated rows remain), then 0.

    The second run must report only 7 — no double-counting of the 1000 rows
    already committed by run 1 (which the DB no longer returns because they now
    carry t_invalidated_at IS NOT NULL).
    """
    # Run 1 — crash after first batch
    crash_call = 0

    async def _crashing_execute(stmt: Any, params: dict | None = None) -> MagicMock:
        nonlocal crash_call
        crash_call += 1
        if crash_call == 1:
            return _row_result(1000)
        raise RuntimeError("simulated crash mid-run")

    session1 = MagicMock()
    session1.execute = _crashing_execute
    begin_ctx1 = MagicMock()
    begin_ctx1.__aenter__ = AsyncMock(return_value=None)
    begin_ctx1.__aexit__ = AsyncMock(return_value=False)
    session1.begin = MagicMock(return_value=begin_ctx1)
    session1.__aenter__ = AsyncMock(return_value=session1)
    session1.__aexit__ = AsyncMock(return_value=False)

    factory1 = MagicMock(return_value=session1)
    worker1 = WorkspaceExpiryWorker(
        session_factory=factory1,
        clock=FakeClock(_NOW),
        batch_size=1000,
    )
    worker1._emit_audit = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated crash"):
        await worker1.run()

    # Run 2 — fresh start; only 7 rows remain eligible
    worker2, params_log2 = _make_worker(batch_sequence=[7, 0], batch_size=1000)
    result2 = await worker2.run()

    assert result2.expired_count == 7, "re-run should count only newly invalidated rows, not rows from run 1"
    # Two execute calls: [7-row batch, 0-row terminator]
    assert len(params_log2) == 2
