"""Unit tests for ClosureRefreshWorker.

All DB interactions are mocked at the session.execute boundary — no Docker
or real Postgres is required.

Coverage:
- run_once with concurrent processing: 10 outbox rows complete concurrently
  (measured by observing that overlapping sleep tasks finish faster than serial
  execution would permit).
- A single row that raises an exception does not cancel the rest of the batch;
  the other rows still succeed (existing retry semantics: the failing row
  remains in the outbox).
- _replace_and_delete issues exactly one INSERT statement for N closure rows
  (not N separate INSERT statements).
- Empty closure_rows: no INSERT issued, only deletes and outbox cleanup.
"""

from __future__ import annotations

import asyncio
import datetime
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.types import FakeClock
from registry.workers.closure_refresh import ClosureRefreshWorker

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()


def _make_worker(concurrency: int = 8) -> ClosureRefreshWorker:
    """Return a worker with a noop session factory."""
    sf = MagicMock()
    return ClosureRefreshWorker(
        session_factory=sf,
        clock=FakeClock(_NOW),
        concurrency=concurrency,
    )


def _outbox_row(
    outbox_id: uuid.UUID | None = None,
    edge_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "outbox_id": outbox_id or uuid.uuid4(),
        "tenant_id": tenant_id or _TENANT,
        "edge_id": edge_id or uuid.uuid4(),
        "attempts": 0,
        "enqueued_at": _NOW,
    }


# ---------------------------------------------------------------------------
# Concurrent batch processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_processes_rows_concurrently() -> None:
    """10 outbox rows with a 20ms mock delay should complete in well under 200ms
    when processed concurrently (serial would require ≥200ms).

    Each call to _process_row sleeps 20ms to give concurrency room to
    express itself, then returns True.
    """
    worker = _make_worker(concurrency=10)
    rows = [_outbox_row() for _ in range(10)]

    call_starts: list[float] = []

    async def _slow_process(row: dict[str, Any]) -> bool:
        call_starts.append(time.monotonic())
        await asyncio.sleep(0.02)  # 20ms artificial delay
        return True

    worker._claim_batch = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    worker._process_row = _slow_process  # type: ignore[method-assign]

    start = time.monotonic()
    result = await worker.run_once()
    elapsed = time.monotonic() - start

    assert result == 10
    # Serial 10×20ms = 200ms minimum; concurrent should be much less.
    # Allow a generous 150ms ceiling (CI overhead) while confirming we beat serial.
    assert elapsed < 0.15, f"expected concurrent completion (<150ms); got {elapsed:.3f}s"
    # All 10 rows started (none skipped due to semaphore starvation).
    assert len(call_starts) == 10


@pytest.mark.asyncio
async def test_run_once_concurrency_cap_respected() -> None:
    """With concurrency=2 and 6 rows, at most 2 rows run at the same time."""
    worker = _make_worker(concurrency=2)
    rows = [_outbox_row() for _ in range(6)]

    active: list[int] = []
    peak: list[int] = []
    _current = [0]

    async def _track_process(row: dict[str, Any]) -> bool:
        _current[0] += 1
        active.append(_current[0])
        peak.append(max(active))
        await asyncio.sleep(0.01)
        _current[0] -= 1
        return True

    worker._claim_batch = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    worker._process_row = _track_process  # type: ignore[method-assign]

    await worker.run_once()

    assert max(peak) <= 2, f"peak concurrency exceeded cap: {max(peak)}"


# ---------------------------------------------------------------------------
# Per-row exception isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_row_exception_does_not_fail_batch() -> None:
    """A row whose _process_row raises does not prevent other rows from completing."""
    worker = _make_worker(concurrency=5)
    rows = [_outbox_row() for _ in range(5)]
    failing_id = rows[2]["outbox_id"]

    async def _maybe_fail(row: dict[str, Any]) -> bool:
        if row["outbox_id"] == failing_id:
            raise RuntimeError("simulated DB error")
        return True

    worker._claim_batch = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    worker._process_row = _maybe_fail  # type: ignore[method-assign]

    # Should not raise; the exception is caught by gather(return_exceptions=True).
    result = await worker.run_once()

    # 4 rows succeeded; 1 raised — it counts as not processed.
    assert result == 4


@pytest.mark.asyncio
async def test_process_row_returns_false_on_failure_leaves_others_unaffected() -> None:
    """A row whose _process_row returns False (error recorded internally) is not
    counted in the processed total; other rows are unaffected."""
    worker = _make_worker(concurrency=5)
    rows = [_outbox_row() for _ in range(4)]
    failing_id = rows[1]["outbox_id"]

    async def _maybe_fail(row: dict[str, Any]) -> bool:
        return row["outbox_id"] != failing_id

    worker._claim_batch = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    worker._process_row = _maybe_fail  # type: ignore[method-assign]

    result = await worker.run_once()
    assert result == 3


# ---------------------------------------------------------------------------
# Bulk UPSERT statement count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_and_delete_issues_one_insert_for_n_rows() -> None:
    """_replace_and_delete must issue exactly one INSERT statement for N closure
    rows, not N separate INSERTs.

    We count session.execute calls: each (root_id, direction) pair issues one
    DELETE, then there is one bulk INSERT, then one DELETE from closure_outbox.
    """
    outbox_id = uuid.uuid4()
    tenant_id = _TENANT
    root_a = uuid.uuid4()
    root_b = uuid.uuid4()

    closure_rows = [
        {
            "tenant_id": tenant_id,
            "root_entity_id": root_a,
            "member_entity_id": uuid.uuid4(),
            "direction": "forward",
            "depth": 1,
            "edge_path": [],
            "edge_rels": [],
        },
        {
            "tenant_id": tenant_id,
            "root_entity_id": root_a,
            "member_entity_id": uuid.uuid4(),
            "direction": "forward",
            "depth": 2,
            "edge_path": [uuid.uuid4()],
            "edge_rels": ["relates_to"],
        },
        {
            "tenant_id": tenant_id,
            "root_entity_id": root_b,
            "member_entity_id": uuid.uuid4(),
            "direction": "reverse",
            "depth": 1,
            "edge_path": [],
            "edge_rels": [],
        },
    ]
    recomputed_keys = {(root_a, "forward"), (root_b, "reverse")}

    # Build a mock session that records calls.
    mock_execute = AsyncMock()
    mock_session = MagicMock()
    mock_session.execute = mock_execute
    mock_session.begin = MagicMock(return_value=_async_cm(None))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_sf = MagicMock()
    mock_sf.return_value = _async_cm(mock_session)

    worker = ClosureRefreshWorker(
        session_factory=mock_sf,  # type: ignore[arg-type]
        clock=FakeClock(_NOW),
    )

    await worker._replace_and_delete(tenant_id, closure_rows, recomputed_keys, outbox_id)

    calls = mock_execute.call_args_list
    sql_texts = [str(c.args[0]) for c in calls]

    delete_cache_calls = [s for s in sql_texts if "DELETE FROM closure_cache" in s]
    insert_calls = [s for s in sql_texts if "INSERT INTO closure_cache" in s]
    delete_outbox_calls = [s for s in sql_texts if "DELETE FROM closure_outbox" in s]

    # One DELETE per (root_id, direction) pair.
    assert len(delete_cache_calls) == 2, f"expected 2 cache DELETEs, got {len(delete_cache_calls)}"
    # One bulk INSERT regardless of closure row count.
    assert len(insert_calls) == 1, f"expected 1 INSERT, got {len(insert_calls)}: {insert_calls}"
    # One outbox cleanup DELETE.
    assert len(delete_outbox_calls) == 1

    # Confirm the single INSERT contains VALUES for all 3 closure rows.
    insert_sql = insert_calls[0]
    # Each row generates one gen_random_uuid() call in the VALUES list.
    assert insert_sql.count("gen_random_uuid()") == 3, f"expected 3 value tuples in bulk INSERT; got:\n{insert_sql}"


@pytest.mark.asyncio
async def test_replace_and_delete_no_insert_when_no_closure_rows() -> None:
    """When closure_rows is empty, no INSERT is issued — only DELETEs."""
    outbox_id = uuid.uuid4()
    tenant_id = _TENANT
    root_id = uuid.uuid4()
    recomputed_keys = {(root_id, "forward")}

    mock_execute = AsyncMock()
    mock_session = MagicMock()
    mock_session.execute = mock_execute
    mock_session.begin = MagicMock(return_value=_async_cm(None))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_sf = MagicMock()
    mock_sf.return_value = _async_cm(mock_session)

    worker = ClosureRefreshWorker(
        session_factory=mock_sf,  # type: ignore[arg-type]
        clock=FakeClock(_NOW),
    )

    await worker._replace_and_delete(tenant_id, [], recomputed_keys, outbox_id)

    calls = mock_execute.call_args_list
    sql_texts = [str(c.args[0]) for c in calls]

    insert_calls = [s for s in sql_texts if "INSERT INTO closure_cache" in s]
    delete_outbox_calls = [s for s in sql_texts if "DELETE FROM closure_outbox" in s]

    assert len(insert_calls) == 0, "no INSERT expected when closure_rows is empty"
    assert len(delete_outbox_calls) == 1


# ---------------------------------------------------------------------------
# run_once with empty outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_empty_outbox_returns_zero() -> None:
    worker = _make_worker()
    worker._claim_batch = AsyncMock(return_value=[])  # type: ignore[method-assign]
    result = await worker.run_once()
    assert result == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _async_cm:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False
