"""Unit tests for ProgressionService in-process definition cache.

These tests exercise the caching layer added to ProgressionService:
  - cache_ttl_seconds > 0 → definition is fetched once and served from cache
    for subsequent calls within the TTL window.
  - cache_ttl_seconds = 0 → caching disabled; every call hits the loader.
  - Concurrent misses for the same key are coalesced (single-flight):
    exactly one DB query is issued regardless of how many coroutines raced.
  - None definitions (unmanaged entity type) are cached too.
  - Different (tenant_id, entity_type) keys cache independently.

All tests mock _load_active_definition_uncached directly on the service
instance so no real database or session factory is needed.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from time import monotonic
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.progression import ProgressionService
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime.datetime(2026, 5, 12, 10, 0, 0, tzinfo=datetime.UTC)


def _clock() -> FakeClock:
    return FakeClock(_FIXED_TS)


def _ctx(tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
    )


def _defn_row(entity_type: str = "initiative") -> MagicMock:
    row = MagicMock()
    row.progression_id = uuid.uuid4()
    row.entity_type = entity_type
    row.is_advisory = False
    row.definition = {
        "states": [{"id": "a", "name": "A", "gates": []}],
        "transitions": {"forward": "sequential"},
    }
    return row


def _make_service(ttl: int = 60) -> ProgressionService:
    """Construct a ProgressionService with a no-op session factory.

    Tests that exercise the cache bypass the session factory entirely by
    patching _load_active_definition_uncached directly on the instance.
    """
    factory = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = cm
    return ProgressionService(
        session_factory=factory,
        clock=_clock(),
        cache_ttl_seconds=ttl,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Cache hit returns same object within TTL
# ---------------------------------------------------------------------------


class TestCacheHit:
    """Second call within TTL returns the cached object without a DB query."""

    @pytest.mark.asyncio
    async def test_loader_called_once_both_calls_return_same_object(self) -> None:
        svc = _make_service(ttl=60)
        row = _defn_row()
        tenant_id = uuid.uuid4()
        entity_type = "initiative"

        # Patch the uncached loader on the instance.
        svc._load_active_definition_uncached = AsyncMock(return_value=row)  # type: ignore[method-assign]

        # Provide a real (but unused) session — the cache wrapper only needs
        # it to pass through to the uncached loader on a miss.
        session = AsyncMock()

        first = await svc._load_active_definition_cached(session, tenant_id, entity_type, _FIXED_TS)
        second = await svc._load_active_definition_cached(session, tenant_id, entity_type, _FIXED_TS)

        assert svc._load_active_definition_uncached.call_count == 1
        assert first is second  # same object identity, not just equality


# ---------------------------------------------------------------------------
# Scenario 2: TTL expiry triggers re-fetch
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    """After the TTL expires the loader is called again on the next access."""

    @pytest.mark.asyncio
    async def test_loader_called_twice_after_expiry(self) -> None:
        svc = _make_service(ttl=60)
        row = _defn_row()
        tenant_id = uuid.uuid4()
        entity_type = "initiative"
        session = AsyncMock()

        svc._load_active_definition_uncached = AsyncMock(return_value=row)  # type: ignore[method-assign]

        # First call populates the cache.
        await svc._load_active_definition_cached(session, tenant_id, entity_type, _FIXED_TS)
        assert svc._load_active_definition_uncached.call_count == 1

        # Simulate expiry by rewinding the entry's expires_at into the past.
        key = (str(tenant_id), entity_type)
        svc._cache[key].expires_at = monotonic() - 1.0

        # Second call should hit the loader again.
        await svc._load_active_definition_cached(session, tenant_id, entity_type, _FIXED_TS)
        assert svc._load_active_definition_uncached.call_count == 2


# ---------------------------------------------------------------------------
# Scenario 3: Concurrent misses → exactly one DB call (single-flight)
# ---------------------------------------------------------------------------


class TestSingleFlight:
    """Concurrent cold-cache callers for the same key issue exactly one DB query."""

    @pytest.mark.asyncio
    async def test_concurrent_misses_coalesced_to_one_db_call(self) -> None:
        svc = _make_service(ttl=60)
        row = _defn_row()
        tenant_id = uuid.uuid4()
        entity_type = "initiative"
        session = AsyncMock()

        call_count = 0

        async def _slow_loader(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # Yield control so the other coroutine advances past its fast-path
            # check and queues on the per-key lock before we return.
            await asyncio.sleep(0.05)
            return row

        svc._load_active_definition_uncached = _slow_loader  # type: ignore[method-assign]

        result_a, result_b = await asyncio.gather(
            svc._load_active_definition_cached(session, tenant_id, entity_type, _FIXED_TS),
            svc._load_active_definition_cached(session, tenant_id, entity_type, _FIXED_TS),
        )

        assert call_count == 1, f"expected exactly 1 DB call due to single-flight coalescing, got {call_count}"
        assert result_a is result_b
        assert result_a is row


# ---------------------------------------------------------------------------
# Bonus: TTL=0 disables cache entirely
# ---------------------------------------------------------------------------


class TestCacheDisabled:
    """cache_ttl_seconds=0 bypasses the cache; every call hits the loader."""

    @pytest.mark.asyncio
    async def test_every_call_hits_loader_when_ttl_zero(self) -> None:
        svc = _make_service(ttl=0)
        row = _defn_row()
        tenant_id = uuid.uuid4()
        entity_type = "initiative"
        session = AsyncMock()

        svc._load_active_definition_uncached = AsyncMock(return_value=row)  # type: ignore[method-assign]

        for _ in range(3):
            await svc._load_active_definition_cached(session, tenant_id, entity_type, _FIXED_TS)

        assert svc._load_active_definition_uncached.call_count == 3
        # The cache dict must remain empty — TTL=0 must not populate it.
        assert svc._cache == {}


# ---------------------------------------------------------------------------
# Bonus: Different keys cache independently
# ---------------------------------------------------------------------------


class TestIndependentKeys:
    """Two different (tenant_id, entity_type) pairs get independent cache slots."""

    @pytest.mark.asyncio
    async def test_different_keys_cache_independently(self) -> None:
        svc = _make_service(ttl=60)
        row_a = _defn_row("initiative")
        row_b = _defn_row("epic")
        tenant_id = uuid.uuid4()
        session = AsyncMock()

        call_log: list[str] = []

        async def _loader(
            _session: Any,
            _tenant_id: uuid.UUID,
            entity_type: str,
            _now: Any,
        ) -> MagicMock:
            call_log.append(entity_type)
            return row_a if entity_type == "initiative" else row_b

        svc._load_active_definition_uncached = _loader  # type: ignore[method-assign]

        r1 = await svc._load_active_definition_cached(session, tenant_id, "initiative", _FIXED_TS)
        r2 = await svc._load_active_definition_cached(session, tenant_id, "epic", _FIXED_TS)
        # Second hits for the same types — should come from cache.
        r3 = await svc._load_active_definition_cached(session, tenant_id, "initiative", _FIXED_TS)
        r4 = await svc._load_active_definition_cached(session, tenant_id, "epic", _FIXED_TS)

        assert call_log == [
            "initiative",
            "epic",
        ], "each type should be loaded exactly once; subsequent calls must use cache"
        assert r1 is row_a
        assert r2 is row_b
        assert r3 is row_a
        assert r4 is row_b


# ---------------------------------------------------------------------------
# Bonus: None (no active definition) is cached too
# ---------------------------------------------------------------------------


class TestNoneDefinitionCached:
    """A None result (no active definition) is cached; the DB is not re-queried."""

    @pytest.mark.asyncio
    async def test_none_definition_is_cached(self) -> None:
        svc = _make_service(ttl=60)
        tenant_id = uuid.uuid4()
        session = AsyncMock()

        svc._load_active_definition_uncached = AsyncMock(return_value=None)  # type: ignore[method-assign]

        first = await svc._load_active_definition_cached(session, tenant_id, "unmanaged_type", _FIXED_TS)
        second = await svc._load_active_definition_cached(session, tenant_id, "unmanaged_type", _FIXED_TS)

        assert first is None
        assert second is None
        assert (
            svc._load_active_definition_uncached.call_count == 1
        ), "None result must be cached so unmanaged entity types do not cause repeated DB hits"
