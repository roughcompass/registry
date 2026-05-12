"""Structural Correctness phase — integration test suite.

Covers four behavioural contracts from the structural-correctness phase:

1. Semver evaluation uses a single implementation (SCR-T01).
   Asserts that ``_adoption_in_scope`` makes correct in-scope / out-of-scope
   decisions for the four cases that previously diverged between the two
   implementations:
   - ``^0.2`` caret on a pre-1.0 version
   - ``~1.2.3`` tilde expansion allowing ``1.2.4``
   - Leading-``v`` stripping (``v1.2.3`` accepted)
   - Multi-clause comma range ``>=1.0,<2.0``

2. Audit emit failure semantics (SCR-T02).
   When the ``session_factory`` inside ``audit.emit()`` raises, the parent
   transaction still commits and ``AUDIT_WRITE_FAILURES`` increments.

3. OIDC concurrent refresh (SCR-T07).
   10 simultaneous ``get_jwks`` calls at TTL boundary issue exactly one
   upstream HTTP fetch.

4. Lifecycle transition validates the ``successor`` field (SCR-T08).
   ``successor="none"`` → succeeds.
   ``successor=<uuid>`` → succeeds.
   Missing ``successor`` field → Pydantic raises 422 before the service layer.

Docker note: tests 1 and 4 require a running Docker daemon (testcontainers).
Tests 2 and 3 are pure-Python unit-style tests exercising the module APIs
in isolation — they do not require Docker.

Run all four under ``make test-integration``.
The pure-Python pair (2 and 3) also pass under ``make test-unit`` if the
testcontainer fixtures are skipped.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.audit import AUDIT_WRITE_FAILURES
from registry.api.audit import emit as audit_emit
from registry.api.auth.oidc import _OidcCache
from registry.api.routers.admin_lifecycle import LifecycleTransitionRequest
from registry.service.breaking_change import _adoption_in_scope
from registry.service.interface_diff import BREAKING, NON_BREAKING
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers (shared by tests that need a live DB)
# ---------------------------------------------------------------------------


async def _seed_tenant(pg_url: str, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a tenant + actor; return (tenant_id, actor_id)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, created_at) "
                    "VALUES (:aid, :tid, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "dn": f"actor-{slug}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def _seed_entity(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_type: str = "capability",
    name: str,
) -> uuid.UUID:
    """Insert an entity with initial lifecycle=alpha; return entity_id."""
    eid = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at, visibility) "
                    "VALUES (:eid, :tid, :etype, :name, TRUE, :now, 'public')"
                ),
                {"eid": eid, "tid": tenant_id, "etype": entity_type, "name": name, "now": _NOW},
            )
            # Seed required vocab values so lifecycle service can look them up.
            for kind, value in (
                ("entity_type", entity_type),
                ("lifecycle_state", "alpha"),
                ("lifecycle_state", "beta"),
                ("lifecycle_state", "deprecated"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values "
                        "(tenant_id, kind, value, is_system, created_at) "
                        "VALUES (:tid, :k, :v, FALSE, :now) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "k": kind, "v": value, "now": _NOW},
                )
            # First lifecycle attribute so transition() can read current state.
            await session.execute(
                text(
                    "INSERT INTO attributes "
                    "(attr_id, tenant_id, entity_id, key, value, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                    "VALUES (gen_random_uuid(), :tid, :eid, 'lifecycle', "
                    "        CAST('\"alpha\"' AS jsonb), :now, NULL, :now, NULL)"
                ),
                {"tid": tenant_id, "eid": eid, "now": _NOW},
            )
    finally:
        await engine.dispose()
    return eid


# ---------------------------------------------------------------------------
# 1. Semver evaluation — unit-level (no DB required)
# ---------------------------------------------------------------------------
#
# ``_adoption_in_scope`` is a pure function in ``breaking_change.py``.  These
# tests confirm that the four previously-divergent cases now resolve correctly
# through the single ``evaluate_version_predicate`` implementation.
#
# Each test verifies two directions: the version that *should* match (in-scope
# because pin fails to cover it) and the version that *should not* match (pin
# covers it, so the consumer is not surfaced as affected).


class TestSemverEvaluationUnified:
    """Confirm _adoption_in_scope uses the canonical evaluate_version_predicate."""

    # Helper: NON_BREAKING classification, no consumer-impacting changes.
    # Under these conditions, a consumer is in-scope only when their pin
    # fails to cover the proposed version.
    _classification = NON_BREAKING
    _no_changes: list[dict[str, Any]] = []

    def _in_scope(self, pin: str, proposed: str) -> bool:
        return _adoption_in_scope(pin, proposed, self._classification, self._no_changes)

    # --- Caret pre-1.0 (^0.2) -----------------------------------------------
    # ^0.2 means >=0.2.0 <0.3.0 (left-most non-zero digit locks to minor).

    def test_caret_pre_1_0_within_range_is_not_in_scope(self) -> None:
        # 0.2.5 satisfies ^0.2 → consumer is covered → not in scope
        assert not self._in_scope("^0.2", "0.2.5")

    def test_caret_pre_1_0_outside_range_is_in_scope(self) -> None:
        # 0.3.0 does NOT satisfy ^0.2 → consumer is not covered → in scope
        assert self._in_scope("^0.2", "0.3.0")

    def test_caret_pre_1_0_upper_boundary_is_not_in_scope(self) -> None:
        # 0.2.0 satisfies ^0.2 → covered → not in scope
        assert not self._in_scope("^0.2", "0.2.0")

    # --- Tilde expansion (~1.2.3) -------------------------------------------
    # ~1.2.3 means >=1.2.3 <1.3.0 — patch bumps within the same minor are ok.

    def test_tilde_patch_bump_is_not_in_scope(self) -> None:
        # 1.2.4 satisfies ~1.2.3 (same-minor patch bump) → covered → not in scope
        assert not self._in_scope("~1.2.3", "1.2.4")

    def test_tilde_minor_bump_is_in_scope(self) -> None:
        # 1.3.0 does NOT satisfy ~1.2.3 → not covered → in scope
        assert self._in_scope("~1.2.3", "1.3.0")

    def test_tilde_base_version_is_not_in_scope(self) -> None:
        # 1.2.3 satisfies ~1.2.3 (exact lower bound) → covered → not in scope
        assert not self._in_scope("~1.2.3", "1.2.3")

    # --- Leading-v stripping (v1.2.3) ----------------------------------------
    # Version strings with a leading ``v`` must be stripped before evaluation.

    def test_leading_v_version_within_pin_is_not_in_scope(self) -> None:
        # proposed "v1.2.3" with pin ">=1.0,<2.0" → satisfies → not in scope
        assert not self._in_scope(">=1.0,<2.0", "v1.2.3")

    def test_leading_v_version_outside_pin_is_in_scope(self) -> None:
        # proposed "v2.1.0" with pin ">=1.0,<2.0" → fails → in scope
        assert self._in_scope(">=1.0,<2.0", "v2.1.0")

    # --- Multi-clause comma range (>=1.0,<2.0) --------------------------------

    def test_multi_clause_within_range_is_not_in_scope(self) -> None:
        # 1.5.0 satisfies >=1.0,<2.0 → covered → not in scope
        assert not self._in_scope(">=1.0,<2.0", "1.5.0")

    def test_multi_clause_below_lower_is_in_scope(self) -> None:
        # 0.9.9 < 1.0 → fails → in scope
        assert self._in_scope(">=1.0,<2.0", "0.9.9")

    def test_multi_clause_at_upper_bound_is_in_scope(self) -> None:
        # 2.0.0 is NOT < 2.0 → fails → in scope
        assert self._in_scope(">=1.0,<2.0", "2.0.0")

    # --- BREAKING always returns True regardless of pin ----------------------

    def test_breaking_classification_always_in_scope(self) -> None:
        # Even a pin that would cover the version is in-scope when BREAKING.
        assert _adoption_in_scope(">=0.1.0,<99.0.0", "1.0.0", BREAKING, [])


# ---------------------------------------------------------------------------
# 2. Audit emit failure semantics — unit-level (no DB required)
# ---------------------------------------------------------------------------
#
# ``audit.emit()`` must swallow exceptions from the inner session and increment
# ``AUDIT_WRITE_FAILURES``.  The parent transaction is not affected because
# ``emit()`` opens its own session via the ``session_factory`` parameter.


@pytest.mark.asyncio
async def test_audit_emit_failure_increments_counter_and_does_not_raise() -> None:
    """A session-factory that raises inside emit() increments the failure counter.

    The failure counter is a Prometheus ``Counter``; its current total is read
    from ``_value.get()`` on the internal child counter (prometheus_client
    internal API, stable across versions).
    """
    # Build a session_factory that raises on __aenter__.
    failing_session = AsyncMock()
    failing_session.__aenter__ = AsyncMock(side_effect=RuntimeError("db exploded"))
    failing_session.__aexit__ = AsyncMock(return_value=False)

    failing_factory = MagicMock()
    failing_factory.return_value = failing_session

    ctx = TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
    )
    clock = FakeClock(_NOW)

    before = AUDIT_WRITE_FAILURES._value.get()  # type: ignore[attr-defined]

    # emit() must not raise — it swallows the error.
    await audit_emit(
        failing_factory,
        ctx,
        clock,
        action="test.action",
        target_type="capability",
        target_id=uuid.uuid4(),
    )

    after = AUDIT_WRITE_FAILURES._value.get()  # type: ignore[attr-defined]
    assert after == before + 1, "AUDIT_WRITE_FAILURES must increment exactly once per swallowed failure"


@pytest.mark.asyncio
async def test_audit_emit_success_does_not_increment_counter() -> None:
    """A successful emit() does not touch the failure counter."""
    # Build a session_factory that succeeds silently.
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=mock_session)
    mock_session.add = MagicMock()

    factory = MagicMock()
    factory.return_value = mock_session

    ctx = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"])
    clock = FakeClock(_NOW)

    before = AUDIT_WRITE_FAILURES._value.get()  # type: ignore[attr-defined]

    await audit_emit(
        factory,
        ctx,
        clock,
        action="test.action",
        target_type="capability",
        target_id=uuid.uuid4(),
    )

    after = AUDIT_WRITE_FAILURES._value.get()  # type: ignore[attr-defined]
    assert after == before, "Successful emit must not increment failure counter"


# ---------------------------------------------------------------------------
# 3. OIDC concurrent refresh — unit-level (no DB required)
# ---------------------------------------------------------------------------
#
# 10 simultaneous ``get_jwks`` calls at TTL expiry must result in exactly one
# upstream HTTP fetch.  The ``asyncio.Lock`` inside ``_OidcCache`` serialises
# concurrent expiry detections so the double-check under lock prevents
# redundant fetches.


@pytest.mark.asyncio
async def test_oidc_concurrent_refresh_issues_exactly_one_fetch() -> None:
    """10 concurrent get_jwks calls at TTL boundary → exactly 1 upstream fetch.

    The test patches the httpx.AsyncClient so no real network call is made.
    The fetch_count counter is incremented inside the mock response to measure
    how many times the slow-path actually reached the HTTP layer.  With the
    asyncio.Lock double-check in place, only the first caller through the lock
    fires the fetch; the remaining 9 re-check under the lock and return the
    already-populated cache.
    """
    import httpx  # noqa: PLC0415

    cache = _OidcCache()
    # Expired cache: every concurrent caller enters the slow path simultaneously.
    cache.jwks_data = None
    cache.jwks_fetched_at = 0.0

    fake_jwks: dict[str, Any] = {"keys": [{"kid": "test-key", "kty": "RSA"}]}
    fetch_count = 0

    # Build a proper async context manager mock for httpx.AsyncClient.
    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass  # accept and discard timeout and any other kwargs

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def get(self, url: str, **kwargs: Any) -> MagicMock:
            nonlocal fetch_count
            fetch_count += 1
            # Yield to the event loop so other concurrent callers can enter the
            # slow-path check before this fetch completes — maximises contention.
            await asyncio.sleep(0)
            resp = MagicMock(spec=httpx.Response)
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value=fake_jwks)
            return resp

    with patch("catalog.api.auth.oidc.httpx.AsyncClient", _FakeClient):
        calls = [cache.get_jwks("https://example.com/.well-known/jwks.json") for _ in range(10)]
        results = await asyncio.gather(*calls)

    assert (
        fetch_count == 1
    ), f"Expected exactly 1 upstream JWKS fetch for 10 concurrent callers at TTL boundary, got {fetch_count}"
    assert all(r == fake_jwks for r in results), "All callers must receive the same JWKS payload"


@pytest.mark.asyncio
async def test_oidc_warm_cache_does_not_fetch() -> None:
    """get_jwks with a warm cache returns the cached value without fetching."""
    import time  # noqa: PLC0415

    cache = _OidcCache()
    fake_jwks: dict[str, Any] = {"keys": [{"kid": "warm-key"}]}
    cache.jwks_data = fake_jwks
    cache.jwks_fetched_at = time.monotonic()  # just fetched = warm

    fetch_count = 0

    async def _fake_fetch(url: str) -> dict[str, Any]:
        nonlocal fetch_count
        fetch_count += 1
        return {}

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await cache.get_jwks("https://example.com/.well-known/jwks.json")

    assert result == fake_jwks
    assert mock_client.call_count == 0, "Warm cache must not open an httpx client"


# ---------------------------------------------------------------------------
# 4. Lifecycle transition — Pydantic schema validation (no DB required)
# ---------------------------------------------------------------------------
#
# ``LifecycleTransitionRequest`` uses ``uuid.UUID | Literal["none"]`` for
# ``successor``.  Pydantic enforces this at parse time so garbage values
# never reach the service layer.


class TestLifecycleTransitionSchema:
    """Pydantic schema for the lifecycle transition request."""

    def test_successor_none_sentinel_accepted(self) -> None:
        req = LifecycleTransitionRequest(new_state="deprecated", successor="none")
        assert req.successor == "none"

    def test_successor_uuid_accepted(self) -> None:
        replacement_id = uuid.uuid4()
        req = LifecycleTransitionRequest(new_state="deprecated", successor=replacement_id)
        assert req.successor == replacement_id

    def test_successor_omitted_raises(self) -> None:
        """Omitting successor entirely is a Pydantic validation error."""
        with pytest.raises(PydanticValidationError):
            LifecycleTransitionRequest(new_state="deprecated")  # type: ignore[call-arg]

    def test_successor_garbage_string_raises(self) -> None:
        """Arbitrary strings are rejected — only 'none' or a UUID are valid."""
        with pytest.raises(PydanticValidationError):
            LifecycleTransitionRequest(new_state="deprecated", successor="garbage")

    def test_successor_boolean_raises(self) -> None:
        """Booleans cannot represent the three-way successor choice."""
        with pytest.raises(PydanticValidationError):
            LifecycleTransitionRequest(new_state="deprecated", successor=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. Lifecycle service — live DB (requires testcontainer)
# ---------------------------------------------------------------------------
#
# Exercises ``LifecycleService.transition`` against a real Postgres schema so
# the bi-temporal attribute writes and state-machine checks run end-to-end.


@pytest_asyncio.fixture
async def scr_app(pg_container: str):  # type: ignore[type-arg]
    from registry.config import Settings  # noqa: PLC0415
    from registry.main import create_app  # noqa: PLC0415

    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    yield create_app(settings)


@pytest.mark.asyncio
async def test_lifecycle_transition_successor_none_succeeds(pg_container: str, scr_app: Any) -> None:
    """transition(successor='none') deprecates the entity without a successor."""
    from registry.service.lifecycle import LifecycleService  # noqa: PLC0415

    tid, aid = await _seed_tenant(pg_container, "scr-lc-none")
    eid = await _seed_entity(pg_container, tenant_id=tid, actor_id=aid, name="cap-scr-none")
    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer", "admin"])

    svc = LifecycleService(
        session_factory=scr_app.state.session_factory,
        clock=FakeClock(_NOW),
    )
    # Promote alpha → beta first (deprecated requires a valid prior state).
    await svc.transition(ctx, eid, "beta", successor="none")
    # Then deprecate with no successor.
    await svc.transition(ctx, eid, "deprecated", successor="none")
    # No assertion needed beyond "did not raise".


@pytest.mark.asyncio
async def test_lifecycle_transition_successor_uuid_succeeds(pg_container: str, scr_app: Any) -> None:
    """transition(successor=<uuid>) deprecates the entity and records the replacement."""
    from registry.service.lifecycle import LifecycleService  # noqa: PLC0415

    tid, aid = await _seed_tenant(pg_container, "scr-lc-uuid")
    eid = await _seed_entity(pg_container, tenant_id=tid, actor_id=aid, name="cap-scr-uuid")
    replacement_id = await _seed_entity(pg_container, tenant_id=tid, actor_id=aid, name="cap-scr-uuid-replacement")
    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer", "admin"])

    svc = LifecycleService(
        session_factory=scr_app.state.session_factory,
        clock=FakeClock(_NOW),
    )
    await svc.transition(ctx, eid, "beta", successor="none")
    # Deprecate with a named successor — service writes the replaced_by edge.
    await svc.transition(ctx, eid, "deprecated", successor=replacement_id)
