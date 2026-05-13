"""SQL-safety tests for ClosureRefreshWorker._replace_and_delete.

The bulk INSERT into ``closure_cache`` builds a VALUES list as a single SQL
text — fast and avoids N round-trips — but any column whose value can vary
in shape must be bound out-of-band, not embedded as a SQL literal. The
``edge_path`` UUID array is safe to embed (UUID strings are hex-and-dash
only); ``edge_rels`` is a TEXT[] whose contents could one day include
single quotes or other SQL metacharacters. This test pins the
parameterization: ``edge_rels`` must reach the DB driver as a Python list,
never as a quoted literal inside the SQL string.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.types import FakeClock
from registry.workers.closure_refresh import ClosureRefreshWorker

_NOW = datetime.datetime(2026, 5, 13, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()


class _async_cm:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_args: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_replace_and_delete_uses_bound_parameter_for_edge_rels() -> None:
    """edge_rels values must be passed as a bound Python list, not interpolated.

    Even an edge_rel containing a single quote (e.g. an apostrophe in a
    misconfigured vocabulary value) must not appear as a SQL literal in the
    INSERT text. The test pins parameterization, not value sanitization:
    the safety property is that the DB driver receives the array out-of-band.
    """
    outbox_id = uuid.uuid4()
    root_id = uuid.uuid4()
    closure_rows = [
        {
            "tenant_id": _TENANT,
            "root_entity_id": root_id,
            "member_entity_id": uuid.uuid4(),
            "direction": "forward",
            "depth": 2,
            "edge_path": [uuid.uuid4()],
            "edge_rels": ["legit", "evil's-rel"],
        },
    ]
    recomputed_keys = {(root_id, "forward")}

    mock_execute = AsyncMock()
    mock_session = MagicMock()
    mock_session.execute = mock_execute
    mock_session.begin = MagicMock(return_value=_async_cm(None))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_sf = MagicMock(return_value=_async_cm(mock_session))

    worker = ClosureRefreshWorker(
        session_factory=mock_sf,  # type: ignore[arg-type]
        clock=FakeClock(_NOW),
    )

    await worker._replace_and_delete(_TENANT, closure_rows, recomputed_keys, outbox_id)

    insert_call = next(
        c for c in mock_execute.call_args_list
        if "INSERT INTO closure_cache" in str(c.args[0])
    )
    insert_sql = str(insert_call.args[0])
    bind_params: dict[str, Any] = insert_call.args[1] if len(insert_call.args) > 1 else (
        insert_call.kwargs.get("parameters") or {}
    )

    # Pin parameterization at the SQL level: the placeholder is present and
    # no quoted rel literal leaks into the SQL text.
    assert ":edge_rels_0" in insert_sql, (
        f"expected :edge_rels_0 placeholder in INSERT SQL; got:\n{insert_sql}"
    )
    assert "'evil's-rel'" not in insert_sql, (
        "a single-quoted rel value leaked into the SQL text — parameterization missing"
    )
    assert "'legit'" not in insert_sql, (
        "rel values must be bound out-of-band, not embedded as SQL literals"
    )

    # And at the bind layer: the DB driver receives the rels as a Python list.
    assert "edge_rels_0" in bind_params, f"bind dict missing edge_rels_0: {bind_params}"
    rels_bound = bind_params["edge_rels_0"]
    assert isinstance(rels_bound, list), (
        f"edge_rels must be a list bound out-of-band; got {type(rels_bound).__name__}"
    )
    assert rels_bound == ["legit", "evil's-rel"], (
        f"edge_rels list shape changed unexpectedly: {rels_bound}"
    )
