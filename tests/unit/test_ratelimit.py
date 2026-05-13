"""Unit tests for per-tenant rate limiting (Postgres advisory locks).

Covers:
- Tenant default row lookup (actor_id IS NULL fallback).
- Actor-specific row lookup takes priority.
- Advisory lock acquired path: request passes through.
- Advisory lock contention path: HTTP 429 returned with correct shape.
- Zero-budget (explicit block) path: 429 without touching the lock.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from registry.api.middleware.ratelimit import _lookup_rate_limit, _try_advisory_lock, check_rate_limit
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tenant_id: uuid.UUID | None = None, actor_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=actor_id or uuid.uuid4(),
        roles=["consumer"],
    )


def _request(method: str = "GET") -> Any:
    req = MagicMock()
    req.method = method
    return req


def _mock_session(rows: list[tuple[int, int] | None]) -> AsyncMock:
    """Build an AsyncSession mock that returns *rows* on successive execute() calls.

    ``rows`` items: ``None`` means no row found (one_or_none returns None),
    otherwise ``(reads_ps, writes_ps)``.
    """
    session = AsyncMock()
    results = []
    for row in rows:
        result_mock = MagicMock()
        if row is None:
            result_mock.one_or_none.return_value = None
        else:
            result_mock.one_or_none.return_value = row
        results.append(result_mock)
    session.execute = AsyncMock(side_effect=results)
    return session


# ---------------------------------------------------------------------------
# _lookup_rate_limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_returns_actor_specific_row_when_present() -> None:
    """Actor-specific row is found on first query — no fallback needed."""
    session = _mock_session([(50, 5)])
    reads, writes = await _lookup_rate_limit(session, uuid.uuid4(), uuid.uuid4())
    assert reads == 50
    assert writes == 5
    # Only one DB query should have been issued.
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_lookup_falls_back_to_tenant_default_when_no_actor_row() -> None:
    """No actor row — falls back to tenant default (actor_id IS NULL)."""
    session = _mock_session([None, (100, 10)])
    reads, writes = await _lookup_rate_limit(session, uuid.uuid4(), uuid.uuid4())
    assert reads == 100
    assert writes == 10
    assert session.execute.call_count == 2


@pytest.mark.asyncio
async def test_lookup_returns_permissive_defaults_when_no_row_at_all() -> None:
    """No rows at all — permissive defaults returned to avoid hard outage."""
    session = _mock_session([None, None])
    reads, writes = await _lookup_rate_limit(session, uuid.uuid4(), uuid.uuid4())
    assert reads == 1000
    assert writes == 100


# ---------------------------------------------------------------------------
# _try_advisory_lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_advisory_lock_returns_true_when_acquired() -> None:
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    acquired = await _try_advisory_lock(session, uuid.uuid4())
    assert acquired is True


@pytest.mark.asyncio
async def test_try_advisory_lock_returns_false_when_contended() -> None:
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=result_mock)

    acquired = await _try_advisory_lock(session, uuid.uuid4())
    assert acquired is False


# ---------------------------------------------------------------------------
# check_rate_limit (integration of lookup + lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_rate_limit_passes_when_lock_acquired() -> None:
    """Happy path: actor row found, lock acquired, no exception raised."""
    ctx = _ctx()
    request = _request("GET")
    session = AsyncMock()

    with (
        patch(
            "registry.api.middleware.ratelimit._lookup_rate_limit",
            AsyncMock(return_value=(100, 10)),
        ),
        patch(
            "registry.api.middleware.ratelimit._try_advisory_lock",
            AsyncMock(return_value=True),
        ),
    ):
        # Should complete without raising.
        await check_rate_limit(request, ctx, session)


@pytest.mark.asyncio
async def test_check_rate_limit_raises_429_on_lock_contention() -> None:
    """Lock not acquired → HTTP 429 with correct body shape."""
    ctx = _ctx()
    request = _request("POST")
    session = AsyncMock()

    with (
        patch(
            "registry.api.middleware.ratelimit._lookup_rate_limit",
            AsyncMock(return_value=(100, 10)),
        ),
        patch(
            "registry.api.middleware.ratelimit._try_advisory_lock",
            AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await check_rate_limit(request, ctx, session)

    assert exc_info.value.status_code == 429
    detail = exc_info.value.detail
    assert detail["error"] == "rate_limit_exceeded"
    assert detail["retry_after_s"] == 1


@pytest.mark.asyncio
async def test_check_rate_limit_raises_429_on_zero_budget_without_lock() -> None:
    """Zero budget short-circuits before attempting the advisory lock."""
    ctx = _ctx()
    request = _request("POST")
    session = AsyncMock()

    lock_mock = AsyncMock(return_value=True)

    with (
        patch(
            "registry.api.middleware.ratelimit._lookup_rate_limit",
            AsyncMock(return_value=(100, 0)),  # writes_per_second = 0
        ),
        patch(
            "registry.api.middleware.ratelimit._try_advisory_lock",
            lock_mock,
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await check_rate_limit(request, ctx, session)

    # Lock must NOT have been called.
    lock_mock.assert_not_called()
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["error"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_check_rate_limit_uses_reads_budget_for_get() -> None:
    """GET → reads budget path; write budget doesn't matter."""
    ctx = _ctx()
    request = _request("GET")
    session = AsyncMock()

    # reads=0, writes=10: GET should be throttled; POST would not.
    with (
        patch(
            "registry.api.middleware.ratelimit._lookup_rate_limit",
            AsyncMock(return_value=(0, 10)),
        ),
        patch(
            "registry.api.middleware.ratelimit._try_advisory_lock",
            AsyncMock(return_value=True),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await check_rate_limit(request, ctx, session)

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_check_rate_limit_uses_writes_budget_for_post() -> None:
    """POST → writes budget path."""
    ctx = _ctx()
    request = _request("POST")
    session = AsyncMock()

    # reads=0, writes=10: POST should pass because writes budget > 0.
    with (
        patch(
            "registry.api.middleware.ratelimit._lookup_rate_limit",
            AsyncMock(return_value=(0, 10)),
        ),
        patch(
            "registry.api.middleware.ratelimit._try_advisory_lock",
            AsyncMock(return_value=True),
        ),
    ):
        # Should NOT raise.
        await check_rate_limit(request, ctx, session)
