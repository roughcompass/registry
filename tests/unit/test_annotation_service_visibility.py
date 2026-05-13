"""Unit tests: AnnotationService.list_annotations visibility enforcement.

Verifies that the visibility chokepoint (VisibilityService.assert_visible) is
called before any DB query in ``list_annotations``. Without this enforcement,
a caller could distinguish private-but-existing capabilities from missing
capabilities by the 200/empty-list vs 404 response gap — a cross-tenant
information leak.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.annotations import AnnotationService
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 5, 13, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_CALLER = uuid.uuid4()
_TENANT_OWNER = uuid.uuid4()
_CAPABILITY_ID = uuid.uuid4()


def _ctx(tenant: uuid.UUID = _TENANT_CALLER) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=uuid.uuid4(), roles=["consumer"])


def _make_session_recording() -> AsyncMock:
    """Build a session whose execute records every SQL string it sees."""
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        executed.append(" ".join(str(stmt).split()))
        result = MagicMock()
        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _execute
    session._executed = executed  # type: ignore[attr-defined]
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    factory = MagicMock()
    factory.return_value = cm
    return factory


def _audit_writer() -> AsyncMock:
    aw = AsyncMock()
    aw.emit = AsyncMock(return_value=None)
    return aw


def _pii_clean() -> MagicMock:
    scanner = MagicMock()
    scanner.scan = MagicMock()
    return scanner


def _visibility_denies() -> MagicMock:
    """Visibility mock whose assert_visible raises a 403-equivalent exception."""
    vis = MagicMock()
    vis.assert_visible = AsyncMock(side_effect=PermissionError("not visible"))
    return vis


def _visibility_allows() -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None)
    return vis


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_on_private_capability_returns_404_not_empty_list() -> None:
    """A caller without visibility hits assert_visible's exception before any DB query.

    Even if the capability row exists, the visibility check must fire first so
    private-but-existing is indistinguishable from missing.
    """
    session = _make_session_recording()
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility_denies(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )

    with pytest.raises(PermissionError):
        await svc.list_annotations(_ctx(), capability_id=_CAPABILITY_ID)

    # No DB query may have executed.
    assert session._executed == []


@pytest.mark.asyncio
async def test_list_annotations_calls_assert_visible_before_db_query() -> None:
    """assert_visible runs exactly once and strictly before any session.execute."""
    call_order: list[str] = []
    vis = MagicMock()

    async def _assert_visible(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("assert_visible")

    vis.assert_visible = AsyncMock(side_effect=_assert_visible)

    session = AsyncMock()

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        call_order.append("session.execute")
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "FROM entities" in sql and "entity_id = :eid" in sql:
            # Owner-tenant lookup must succeed so the service proceeds past the
            # 404 guard and we can verify the call order.
            row = MagicMock()
            row.tenant_id = _TENANT_OWNER
            result.first = MagicMock(return_value=row)
        else:
            result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session.execute = _execute

    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = cm

    svc = AnnotationService(
        session_factory=factory,
        visibility_svc=vis,
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )

    await svc.list_annotations(_ctx(), capability_id=_CAPABILITY_ID)

    assert vis.assert_visible.await_count == 1
    assert call_order, "expected at least one recorded call"
    assert call_order[0] == "assert_visible"
