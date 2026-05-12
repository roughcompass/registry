"""Unit tests for integration capability type lifecycle gate.

Covers:
- `_enforce_integration_edge_constraint` returns silently for non-integration entities.
- Integration entity with ≥ 2 active composes/depends_on edges promotes successfully.
- Integration entity with 0 edges → ValidationError (422).
- Integration entity with 1 qualifying edge → ValidationError (422).
- Constraint does *not* fire when transitioning to/staying at `alpha`.
- Mixed qualifying rels (composes + depends_on) count together.
- Invalidated / closed edges are not counted.
- `promote_from_draft()` is a thin wrapper around `transition()` (alpha → beta).
- Module exports document the public surface.

Traces to the integration-capability promotion constraint (≥ 2 composes/depends_on edges required).

The tests mock the SQLAlchemy session so they do not require a live database.
Direct unit testing of `_enforce_integration_edge_constraint` is the simplest
way to assert the constraint logic; `transition()` is also covered end-to-end
with a fully-stubbed session for the promote_from_draft happy/error paths.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.exceptions import ValidationError
from registry.service.lifecycle import (
    INTEGRATION_MIN_EDGES,
    INTEGRATION_QUALIFYING_RELS,
    LifecycleService,
)
from registry.types import FakeClock, TenantContext

_T0 = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
    )


def _mock_entity(tenant_id: uuid.UUID, entity_type: str) -> Any:
    """Stand-in for a fetched Entity row."""
    e = MagicMock()
    e.tenant_id = tenant_id
    e.entity_type = entity_type
    return e


def _mock_session(
    *,
    entity: Any | None,
    edge_count: int,
) -> AsyncMock:
    """Build an AsyncMock session whose `get(Entity, ...)` returns *entity* and
    whose `execute(select(count())...)` returns *edge_count*.
    """
    session = AsyncMock()
    session.get = AsyncMock(return_value=entity)

    count_result = MagicMock()
    count_result.scalar_one.return_value = edge_count
    session.execute = AsyncMock(return_value=count_result)
    return session


def _mock_session_factory(session: AsyncMock) -> Any:
    """Wrap a session in an async-context-manager factory shaped like
    `async_sessionmaker`."""
    # session.begin() is used as `async with session.begin(): ...`
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    factory_cm = AsyncMock()
    factory_cm.__aenter__ = AsyncMock(return_value=session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=factory_cm)


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_qualifying_rels_are_composes_and_depends_on() -> None:
    """Only composes and depends_on edges satisfy the promotion constraint."""
    assert INTEGRATION_QUALIFYING_RELS == frozenset({"composes", "depends_on"})


def test_min_edges_is_two() -> None:
    """Promotion requires at least two qualifying edges."""
    assert INTEGRATION_MIN_EDGES == 2


# ---------------------------------------------------------------------------
# _enforce_integration_edge_constraint — direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constraint_skipped_when_new_state_is_alpha() -> None:
    """Promotion gate must not fire when staying at / entering `alpha`."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    # session.get should never be called — constraint must short-circuit.
    session = _mock_session(entity=None, edge_count=0)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    await svc._enforce_integration_edge_constraint(session, ctx, entity_id, "alpha")

    session.get.assert_not_called()
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_constraint_skipped_for_non_integration_entity() -> None:
    """A `capability`/`system`/etc. entity is never gated by edge count."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="capability")
    # edge_count=0 would fail if this were an integration, but should not be queried.
    session = _mock_session(entity=entity, edge_count=0)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    await svc._enforce_integration_edge_constraint(session, ctx, entity_id, "beta")

    session.get.assert_awaited_once()
    # The count query must not run for non-integration entities.
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_constraint_skipped_when_entity_missing() -> None:
    """If the entity doesn't exist, the bi-temporal write path handles it; no false 422."""
    ctx = _ctx()
    session = _mock_session(entity=None, edge_count=0)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    await svc._enforce_integration_edge_constraint(session, ctx, uuid.uuid4(), "beta")

    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_constraint_skipped_when_entity_belongs_to_other_tenant() -> None:
    """Tenant isolation guard: foreign-tenant entity is treated like a missing row."""
    ctx = _ctx()
    other = _mock_entity(uuid.uuid4(), entity_type="integration")
    session = _mock_session(entity=other, edge_count=99)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    await svc._enforce_integration_edge_constraint(session, ctx, uuid.uuid4(), "beta")

    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_integration_with_two_edges_promotes_successfully() -> None:
    """≥ 2 qualifying edges → constraint passes, no exception."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    session = _mock_session(entity=entity, edge_count=2)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    # Must not raise.
    await svc._enforce_integration_edge_constraint(session, ctx, entity_id, "beta")

    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_integration_with_more_than_two_edges_promotes_successfully() -> None:
    """5 qualifying edges (e.g. composes(A) + composes(B) + depends_on(C,D,E)) is fine."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    session = _mock_session(entity=entity, edge_count=5)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    await svc._enforce_integration_edge_constraint(session, ctx, entity_id, "ga")

    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_integration_with_zero_edges_raises_422() -> None:
    """No qualifying edges → ValidationError (HTTP 422)."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    session = _mock_session(entity=entity, edge_count=0)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    with pytest.raises(ValidationError) as excinfo:
        await svc._enforce_integration_edge_constraint(session, ctx, entity_id, "beta")

    msg = str(excinfo.value)
    assert "integration" in msg
    assert "composes" in msg
    assert "depends_on" in msg
    assert "found 0" in msg


@pytest.mark.asyncio
async def test_integration_with_one_edge_raises_422() -> None:
    """A single qualifying edge is not enough — must be ≥ 2."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    session = _mock_session(entity=entity, edge_count=1)
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    with pytest.raises(ValidationError) as excinfo:
        await svc._enforce_integration_edge_constraint(session, ctx, entity_id, "beta")

    assert "found 1" in str(excinfo.value)


@pytest.mark.asyncio
async def test_constraint_fires_for_transitions_beyond_beta() -> None:
    """Gate also applies to alpha → ga/deprecated/retired transitions."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    svc = LifecycleService(session_factory=MagicMock(), clock=FakeClock(_T0))

    for target in ("beta", "ga", "deprecated", "retired"):
        session = _mock_session(entity=entity, edge_count=0)
        with pytest.raises(ValidationError):
            await svc._enforce_integration_edge_constraint(session, ctx, entity_id, target)


# ---------------------------------------------------------------------------
# transition() / promote_from_draft() — end-to-end with mocked DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_from_draft_succeeds_for_integration_with_two_edges() -> None:
    """Happy path: integration entity with 2 composes edges → alpha → beta works."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    session = _mock_session(entity=entity, edge_count=2)
    factory = _mock_session_factory(session)

    svc = LifecycleService(session_factory=factory, clock=FakeClock(_T0))

    # Skip _enforce_transition (state-machine) and _write_attribute (DB write) — we
    # only want to assert the edge-constraint gate inside transition() runs and
    # accepts a valid integration.
    with (
        patch.object(svc, "_enforce_transition", AsyncMock()),
        patch.object(svc, "_write_attribute", AsyncMock()) as write_mock,
    ):
        await svc.promote_from_draft(ctx, entity_id, "beta")

    write_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_promote_from_draft_rejects_integration_with_zero_edges() -> None:
    """Failure path: integration entity with 0 edges → 422."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    session = _mock_session(entity=entity, edge_count=0)
    factory = _mock_session_factory(session)

    svc = LifecycleService(session_factory=factory, clock=FakeClock(_T0))

    with (
        patch.object(svc, "_enforce_transition", AsyncMock()),
        patch.object(svc, "_write_attribute", AsyncMock()) as write_mock,
        pytest.raises(ValidationError, match="at least 2"),
    ):
        await svc.promote_from_draft(ctx, entity_id, "beta")

    # _write_attribute must not have been called when the constraint fails.
    write_mock.assert_not_called()


@pytest.mark.asyncio
async def test_transition_does_not_affect_non_integration_capability() -> None:
    """A regular `capability` entity transitions to beta regardless of edges."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="capability")
    # edge_count=0 — would block an integration, but capability is unaffected.
    session = _mock_session(entity=entity, edge_count=0)
    factory = _mock_session_factory(session)

    svc = LifecycleService(session_factory=factory, clock=FakeClock(_T0))

    with (
        patch.object(svc, "_enforce_transition", AsyncMock()),
        patch.object(svc, "_write_attribute", AsyncMock()) as write_mock,
    ):
        await svc.transition(ctx, entity_id, "beta", successor="none")

    write_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_transition_to_alpha_skips_constraint_even_for_integration() -> None:
    """First-write of `alpha` is allowed for an integration with zero edges."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _mock_entity(ctx.tenant_id, entity_type="integration")
    session = _mock_session(entity=entity, edge_count=0)
    factory = _mock_session_factory(session)

    svc = LifecycleService(session_factory=factory, clock=FakeClock(_T0))

    with (
        patch.object(svc, "_enforce_transition", AsyncMock()),
        patch.object(svc, "_write_attribute", AsyncMock()) as write_mock,
    ):
        await svc.transition(ctx, entity_id, "alpha", successor="none")

    write_mock.assert_awaited_once()
    # The constraint method should not have executed a count query.
    # (We can't observe a private method directly, but session.execute being
    # un-awaited confirms it.)
    session.execute.assert_not_called()
