"""Unit tests for the batched attribute fetch in `EntityService.update_entity`.

Before CPR-T19, `update_entity` issued one `SELECT` per updated key inside a
loop — M round-trips for M attribute updates in a single transaction.

After the fix, a single `SELECT ... WHERE key = ANY(:keys)` fetches all
currently-open rows at once. The supersede loop then runs in Python with no
further DB I/O.  This test verifies that a 20-attribute PATCH issues exactly
one SELECT (for the batch fetch) and that the bi-temporal supersede semantics
are preserved.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.entity import EntityService
from registry.service.schema import ValidationResult
from registry.storage.models import Attribute, Entity
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 5, 11, 12, 0, 0, tzinfo=datetime.UTC)

ATTR_COUNT = 20


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["producer"])


def _entity_row(tenant_id: uuid.UUID) -> Entity:
    e = Entity()
    e.entity_id = uuid.uuid4()
    e.tenant_id = tenant_id
    e.entity_type = "capability"
    e.name = "batch-test-cap"
    e.external_id = None
    e.is_active = True
    e.created_at = _NOW
    e.created_by = None
    return e


def _attr_row(tenant_id: uuid.UUID, entity_id: uuid.UUID, key: str) -> Attribute:
    a = Attribute()
    a.attr_id = uuid.uuid4()
    a.tenant_id = tenant_id
    a.entity_id = entity_id
    a.key = key
    a.value = "old-value"
    a.t_valid_from = _NOW - datetime.timedelta(days=1)
    a.t_valid_to = None
    a.t_ingested_at = _NOW - datetime.timedelta(days=1)
    a.t_invalidated_at = None
    a.created_by = None
    return a


def _build_service_and_session(
    entity: Entity,
    existing_attrs: list[Attribute],
) -> tuple[EntityService, AsyncMock]:
    """Build EntityService with a fully mocked session.

    `session.execute` is called twice in `update_entity`:
      1. `session.get(Entity, ...)` — mocked via `session.get`.
      2. The batched `SELECT Attribute WHERE key IN (...)` — first execute call.

    We record every `session.execute` call so the test can assert the count.
    """
    # Scalars result for the batched attribute SELECT.
    scalars_result = MagicMock()
    scalars_result.scalars = MagicMock(return_value=iter(existing_attrs))

    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=iter(existing_attrs))

    session = AsyncMock()
    session.get = AsyncMock(return_value=entity)
    # execute returns the scalars-carrying result for the batch SELECT.
    session.execute = AsyncMock(return_value=execute_result)
    session.add = MagicMock()

    # Wrap the session in an async context-manager compatible factory.
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)

    # The begin() context manager must also be an async CM.
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)

    session_factory = MagicMock(return_value=session_cm)

    frozen_clock = MagicMock()
    frozen_clock.now = MagicMock(return_value=_NOW)

    # validate_capability is awaited inside update_entity; it must be an AsyncMock
    # so MagicMock's default sync callable doesn't break the await expression.
    schema_mock = MagicMock()
    schema_mock.validate_capability = AsyncMock(return_value=ValidationResult(valid=True, warnings=[]))

    svc = EntityService(
        session_factory=session_factory,
        clock=frozen_clock,
        vocabulary=MagicMock(),
        schema=schema_mock,
    )
    return svc, session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_select_issues_one_execute_for_20_attrs() -> None:
    """A 20-attribute PATCH must issue exactly one `session.execute` call.

    Before the fix: 20 execute calls (one SELECT per key).
    After the fix: 1 execute call (one SELECT for all keys at once).
    """
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id)

    # 20 existing open attribute rows (one per key).
    updates = {f"attr_{i}": f"new-value-{i}" for i in range(ATTR_COUNT)}
    existing = [_attr_row(ctx.tenant_id, entity.entity_id, k) for k in updates]

    svc, session = _build_service_and_session(entity, existing)

    await svc.update_entity(ctx, entity.entity_id, updates)

    # Only one DB round-trip for the existence check (the batched SELECT).
    assert session.execute.await_count == 1, (
        f"Expected 1 execute call (batched SELECT), got {session.execute.await_count}. "
        "Each key should NOT have its own SELECT."
    )


@pytest.mark.asyncio
async def test_supersede_closes_existing_rows() -> None:
    """Each currently-open attribute row must have `t_valid_to` set to `now`."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id)

    keys = ["alpha", "beta", "gamma"]
    updates = {k: f"new-{k}" for k in keys}
    existing = [_attr_row(ctx.tenant_id, entity.entity_id, k) for k in keys]

    svc, session = _build_service_and_session(entity, existing)

    await svc.update_entity(ctx, entity.entity_id, updates)

    # Every pre-existing open row should be closed.
    for attr in existing:
        assert attr.t_valid_to == _NOW, f"Attribute '{attr.key}' was not superseded (t_valid_to not set to now)."


@pytest.mark.asyncio
async def test_new_rows_inserted_for_every_updated_key() -> None:
    """One new Attribute row must be added (via session.add) for every updated key."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id)

    updates = {f"k{i}": f"v{i}" for i in range(5)}
    existing = [_attr_row(ctx.tenant_id, entity.entity_id, k) for k in updates]

    svc, session = _build_service_and_session(entity, existing)

    await svc.update_entity(ctx, entity.entity_id, updates)

    # session.add called once per updated key (new bi-temporal row).
    assert session.add.call_count == len(
        updates
    ), f"Expected {len(updates)} session.add calls, got {session.add.call_count}."


@pytest.mark.asyncio
async def test_no_existing_row_still_inserts_new_row() -> None:
    """If a key has no currently-open row, the new row is still inserted (no close step)."""
    ctx = _ctx()
    entity = _entity_row(ctx.tenant_id)

    updates = {"brand-new-key": "initial-value"}
    # No existing rows for this key.
    svc, session = _build_service_and_session(entity, existing_attrs=[])

    await svc.update_entity(ctx, entity.entity_id, updates)

    assert session.add.call_count == 1
    added: Attribute = session.add.call_args[0][0]
    assert added.key == "brand-new-key"
    assert added.value == "initial-value"
    assert added.t_valid_to is None
    assert added.t_invalidated_at is None
