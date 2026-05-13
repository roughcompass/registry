"""Unit tests for registry.api.audit.emit.

Confirms emit() never re-raises (failed audit must not roll back the
service-layer mutation), and that the Prometheus failure counter
advances when the underlying session_factory raises.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.api.audit import AUDIT_WRITE_FAILURES, emit
from registry.types import FakeClock, TenantContext


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["producer"],
    )


def _ok_session_factory() -> MagicMock:
    """Async session_factory that walks the begin/commit happy path."""
    session = MagicMock()
    session.add = MagicMock(return_value=None)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=transaction)
    factory = MagicMock(return_value=session)
    return factory


def _broken_session_factory() -> MagicMock:
    factory = MagicMock(side_effect=RuntimeError("db unreachable"))
    return factory


@pytest.mark.asyncio
async def test_emit_writes_one_row_on_happy_path() -> None:
    factory = _ok_session_factory()
    ctx = _ctx()
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    await emit(
        factory,
        ctx,
        clock,
        action="create",
        target_type="entity",
        target_id=uuid.uuid4(),
        before=None,
        after={"name": "x"},
    )

    factory.return_value.add.assert_called_once()


@pytest.mark.asyncio
async def test_emit_swallows_exception_and_increments_counter() -> None:
    factory = _broken_session_factory()
    ctx = _ctx()
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    before = AUDIT_WRITE_FAILURES._value.get()
    # Must NOT raise.
    await emit(
        factory,
        ctx,
        clock,
        action="update",
        target_type="fact",
        target_id=uuid.uuid4(),
    )
    after = AUDIT_WRITE_FAILURES._value.get()

    assert after == before + 1
