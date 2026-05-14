"""Unit tests for the RSAM grant cache in RsamClaimSource — 6 scenarios.

All tests inject `fetch_authorities` at construction time (AsyncMock or lambda).
The session factory is mocked; no DB is required. Time is controlled by
monkeypatching `time.monotonic` in `registry.auth.rsam.claim_source`.

Monotonic call map (per resolve() invocation):
  Cold fill (cache miss + success):
    [0] fast-path TTL check
    [1] re-check inside per-key lock
    [2] _fetch_and_resolve: t0 (before fetch)
    [3] _fetch_and_resolve: t1 (after fetch, latency_ms)
    [4] cache write (entry.cached_at = ...)
  Cache hit (within TTL):
    [5] fast-path TTL check → returns immediately
  Cache miss + fetch failure + stale-serve:
    [5] fast-path TTL check → expired
    [6] re-check inside lock → still expired
    [7] _fetch_and_resolve: t0 → fetch raises
    [8] _handle_fetch_failure: now (stale age calc)
  Cache miss + fetch failure + serve_stale=False:
    [5] fast-path TTL check → expired
    [6] re-check inside lock → still expired
    [7] _fetch_and_resolve: t0 → fetch raises
         _handle_fetch_failure raises immediately (no extra monotonic call)
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.auth.rsam.claim_source import RsamClaimSource
from registry.config import Settings

# ---------------------------------------------------------------------------
# Helpers


def _settings(
    ttl: int = 300,
    stale_ceiling: int = 86400,
    serve_stale: bool = False,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test/test",
        pgbouncer_url="postgresql+asyncpg://test/test",
        scheduler_jobstore_url="postgresql+asyncpg://test/test",
        auth_mode="rsam",
        auth_claim_source_url="https://rsam.example.com",
        auth_claim_cache_ttl_seconds=ttl,
        auth_stale_ceiling_seconds=stale_ceiling,
        auth_serve_stale_on_failure=serve_stale,
    )


def _make_session_factory(
    actor_display_name: str | None = "Alice",
    actor_email: str | None = None,
) -> MagicMock:
    """Mock session_factory compatible with all async-with usage patterns in claim_source."""
    session = AsyncMock()

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    actor_row = (actor_display_name, actor_email)
    execute_result = MagicMock()
    execute_result.first = MagicMock(return_value=actor_row)
    session.execute = AsyncMock(return_value=execute_result)

    outer_cm = AsyncMock()
    outer_cm.__aenter__ = AsyncMock(return_value=session)
    outer_cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=outer_cm)
    return factory


def _claims(subject: str = "user-abc") -> dict:
    return {"sub": subject}


def _make_source(
    fetch_authorities: AsyncMock,
    *,
    ttl: int = 300,
    stale_ceiling: int = 86400,
    serve_stale: bool = False,
    session_factory: MagicMock | None = None,
) -> RsamClaimSource:
    if session_factory is None:
        session_factory = _make_session_factory()
    return RsamClaimSource(
        _settings(ttl=ttl, stale_ceiling=stale_ceiling, serve_stale=serve_stale),
        session_factory,
        fetch_authorities=fetch_authorities,
    )


AUTHORITY = "112025_DP_CHANNEL_Owner"
TENANT_UUID = uuid.uuid4()

# Monotonic values for a single cold-fill resolve() call.
# cached_at is stored as value index 4 → 4.0
_COLD_FILL_MONO = [0.0, 1.0, 2.0, 3.0, 4.0]

# Monotonic values for a second resolve() call that is a cache HIT (age < ttl).
# Only 1 call: the fast-path check returns a value less than cached_at + ttl.
_CACHE_HIT_MONO = [5.0]  # age = 5 - 4 = 1 < 300 → hit

# Monotonic values for a second resolve() call that is a cache MISS (age > ttl).
# 5 calls: fast-path, re-check, t0, t1, cache-write.
_TTL_EXPIRED_SECOND_MONO = [500.0, 500.0, 500.0, 500.001, 500.001]

# Monotonic values for a second resolve() call where fetch raises and stale IS served.
# 4 calls: fast-path, re-check, t0 (before fetch raises), _handle_fetch_failure now.
_STALE_SERVE_MONO = [500.0, 500.0, 500.0, 500.0]

# Monotonic values for a second resolve() call where fetch raises and stale is DISABLED.
# 3 calls: fast-path, re-check, t0 (before fetch raises). _handle_fetch_failure
# raises immediately without calling time.monotonic.
_STALE_DISABLED_MONO = [500.0, 500.0, 500.0]


# ---------------------------------------------------------------------------
# Scenario 1: TTL hit — second call returns cached result without a second fetch


@pytest.mark.asyncio
async def test_ttl_hit_single_fetch() -> None:
    """Two resolve() calls within the TTL window issue exactly one fetch_authorities call."""
    fetch = AsyncMock(return_value=[AUTHORITY])

    mono_seq = _COLD_FILL_MONO + _CACHE_HIT_MONO

    with (
        patch("registry.auth.rsam.claim_source.upsert_rsam_tenant", AsyncMock(return_value=TENANT_UUID)),
        patch("registry.auth.rsam.claim_source.upsert_rsam_actor", AsyncMock()),
        patch("registry.auth.rsam.claim_source.time.monotonic", side_effect=mono_seq),
    ):
        source = _make_source(fetch)
        result1 = await source.resolve(_claims())
        result2 = await source.resolve(_claims())

    assert fetch.await_count == 1
    assert len(result1.tenant_grants) == 1
    assert result2.tenant_grants == result1.tenant_grants


# ---------------------------------------------------------------------------
# Scenario 2: TTL expiry — second call after TTL triggers a re-fetch


@pytest.mark.asyncio
async def test_ttl_expiry_triggers_refetch() -> None:
    """After the TTL window expires, resolve() issues a second fetch_authorities call."""
    fetch = AsyncMock(return_value=[AUTHORITY])

    mono_seq = _COLD_FILL_MONO + _TTL_EXPIRED_SECOND_MONO

    with (
        patch("registry.auth.rsam.claim_source.upsert_rsam_tenant", AsyncMock(return_value=TENANT_UUID)),
        patch("registry.auth.rsam.claim_source.upsert_rsam_actor", AsyncMock()),
        patch("registry.auth.rsam.claim_source.time.monotonic", side_effect=mono_seq),
    ):
        source = _make_source(fetch, ttl=300)
        await source.resolve(_claims())
        await source.resolve(_claims())

    assert fetch.await_count == 2


# ---------------------------------------------------------------------------
# Scenario 3: Single-flight — concurrent misses issue exactly one fetch


@pytest.mark.asyncio
async def test_single_flight_concurrent_misses() -> None:
    """Two concurrent resolve() calls on a cold cache issue exactly ONE fetch_authorities."""
    call_count = 0

    async def slow_fetch(subject: str) -> list[str]:
        nonlocal call_count
        call_count += 1
        # Small delay to ensure both coroutines enter resolve() before either completes.
        await asyncio.sleep(0.05)
        return [AUTHORITY]

    # No monotonic patching: real time is fine for a concurrency test since we are
    # testing call count, not TTL arithmetic.
    with (
        patch("registry.auth.rsam.claim_source.upsert_rsam_tenant", AsyncMock(return_value=TENANT_UUID)),
        patch("registry.auth.rsam.claim_source.upsert_rsam_actor", AsyncMock()),
    ):
        source = _make_source(AsyncMock(side_effect=slow_fetch))
        results = await asyncio.gather(
            source.resolve(_claims()),
            source.resolve(_claims()),
        )

    assert call_count == 1
    for r in results:
        assert len(r.tenant_grants) == 1


# ---------------------------------------------------------------------------
# Scenario 4: Stale-serve fires when enabled and within ceiling


@pytest.mark.asyncio
async def test_stale_serve_fires_when_enabled() -> None:
    """After a successful resolve(), if fetch_authorities raises and stale-on-failure
    is enabled, the cached result is returned and auth.stale_cache.served is emitted.
    """
    fetch = AsyncMock(return_value=[AUTHORITY])
    session_factory = _make_session_factory()

    # Capture audit events by intercepting execute on the shared session object.
    # _make_session_factory stores the session as outer_cm.__aenter__'s return value;
    # we fish it out directly so we intercept the same object the source will call.
    shared_session = session_factory.return_value.__aenter__.return_value
    audit_actions: list[str] = []
    original_execute = shared_session.execute

    async def capturing_execute(stmt, params=None, **kw):
        # The stale_cache event carries "stale_age_seconds" in its after_jsonb payload.
        # Identify it by the combination of that key and absence of "subject" (which
        # the claim_source.invoked event carries instead).
        if params and "after_jsonb" in params:
            after = params["after_jsonb"]
            if "stale_age_seconds" in after and "subject" not in after:
                audit_actions.append("auth.stale_cache.served")
        return await original_execute(stmt, params, **kw)

    shared_session.execute = capturing_execute

    mono_seq = _COLD_FILL_MONO + _STALE_SERVE_MONO

    with (
        patch("registry.auth.rsam.claim_source.upsert_rsam_tenant", AsyncMock(return_value=TENANT_UUID)),
        patch("registry.auth.rsam.claim_source.upsert_rsam_actor", AsyncMock()),
        patch("registry.auth.rsam.claim_source.time.monotonic", side_effect=mono_seq),
    ):
        source = _make_source(
            fetch,
            ttl=300,
            stale_ceiling=86400,
            serve_stale=True,
            session_factory=session_factory,
        )

        # First call: warm the cache.
        result1 = await source.resolve(_claims())
        assert len(result1.tenant_grants) == 1

        # Make the next fetch fail.
        fetch.side_effect = RuntimeError("upstream down")

        # Second call: TTL expired, fetch fails — should serve stale.
        result2 = await source.resolve(_claims())

    assert result2.tenant_grants == result1.tenant_grants
    assert "auth.stale_cache.served" in audit_actions


# ---------------------------------------------------------------------------
# Scenario 5: Stale-serve disabled — exception propagates, no audit event


@pytest.mark.asyncio
async def test_stale_serve_disabled_propagates_exception() -> None:
    """When auth_serve_stale_on_failure=False, a fetch failure propagates after TTL."""
    fetch = AsyncMock(return_value=[AUTHORITY])
    session_factory = _make_session_factory()

    shared_session = session_factory.return_value.__aenter__.return_value
    audit_actions: list[str] = []
    original_execute = shared_session.execute

    async def capturing_execute(stmt, params=None, **kw):
        if params and "after_jsonb" in params:
            after = params["after_jsonb"]
            if "stale_age_seconds" in after and "subject" not in after:
                audit_actions.append("auth.stale_cache.served")
        return await original_execute(stmt, params, **kw)

    shared_session.execute = capturing_execute

    mono_seq = _COLD_FILL_MONO + _STALE_DISABLED_MONO

    with (
        patch("registry.auth.rsam.claim_source.upsert_rsam_tenant", AsyncMock(return_value=TENANT_UUID)),
        patch("registry.auth.rsam.claim_source.upsert_rsam_actor", AsyncMock()),
        patch("registry.auth.rsam.claim_source.time.monotonic", side_effect=mono_seq),
    ):
        source = _make_source(
            fetch,
            ttl=300,
            stale_ceiling=86400,
            serve_stale=False,
            session_factory=session_factory,
        )

        await source.resolve(_claims())

        fetch.side_effect = RuntimeError("upstream down")

        with pytest.raises(RuntimeError, match="upstream down"):
            await source.resolve(_claims())

    assert "auth.stale_cache.served" not in audit_actions


# ---------------------------------------------------------------------------
# Scenario 6: Stale ceiling enforced — no stale-serve beyond ceiling


@pytest.mark.asyncio
async def test_stale_ceiling_enforced() -> None:
    """When the stale entry exceeds the ceiling, resolve() raises 503 and does not emit stale event."""
    from fastapi import HTTPException

    fetch = AsyncMock(return_value=[AUTHORITY])

    # Ceiling is 3600s. cached_at=4.0. Second call simulates t=3605.0.
    # age = 3605 - 4 = 3601 > 3600 (ceiling) → fail-closed with 503.
    _ceiling_stale_mono = [3605.0, 3605.0, 3605.0, 3605.0]

    mono_seq = _COLD_FILL_MONO + _ceiling_stale_mono

    with (
        patch("registry.auth.rsam.claim_source.upsert_rsam_tenant", AsyncMock(return_value=TENANT_UUID)),
        patch("registry.auth.rsam.claim_source.upsert_rsam_actor", AsyncMock()),
        patch("registry.auth.rsam.claim_source.time.monotonic", side_effect=mono_seq),
    ):
        source = _make_source(fetch, ttl=300, stale_ceiling=3600, serve_stale=True)

        await source.resolve(_claims())

        fetch.side_effect = RuntimeError("upstream down")

        with pytest.raises(HTTPException) as exc_info:
            await source.resolve(_claims())

    assert exc_info.value.status_code == 503
    assert "Retry-After" in exc_info.value.headers
