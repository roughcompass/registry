"""Unit tests for FactService / CatalogService conflict policy.

Tests ``create_fact_from_sync`` and ``upsert_synced_facts`` covering:
- Non-authoritative incoming blocked by existing authoritative fact
  (is_authoritative_superseded=True set on new row).
- Non-authoritative incoming with no authoritative blocker → creates cleanly.
- Same-category bi-temporal supersession: prior open non-auth row is closed.
- ``upsert_synced_facts`` bulk path: correct created/superseded counts.
- ``upsert_synced_facts`` bulk path: empty batch returns all-zero counts.
- ``upsert_synced_facts`` bulk path: 100-fact batch issues O(1) transactions.
- ``upsert_synced_facts`` bulk path: conflict resolution for same-key+different-content.
- ``upsert_synced_facts`` bulk path: same-key+same-content is a no-op insert (row added).
- ``upsert_synced_facts`` bulk path: unknown entity_id raises NotFoundError.
- Entity not found raises NotFoundError.

All DB interactions are mocked — no Postgres or Docker required.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.exceptions import NotFoundError
from registry.service.catalog import CatalogService
from registry.service.entity import EntityService
from registry.service.facts import FactService
from registry.service.schema import SchemaService
from registry.service.vocabulary import VocabularyService
from registry.storage.models import Entity, Fact
from registry.types import FakeClock, SyncWriteResult, TenantContext
from sync.connector import ParsedFact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime.datetime(2025, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)


def _ctx(tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["sync_worker"],
    )


def _entity(tenant_id: uuid.UUID, entity_id: uuid.UUID | None = None) -> Entity:
    e = MagicMock(spec=Entity)
    e.entity_id = entity_id or uuid.uuid4()
    e.tenant_id = tenant_id
    return e


def _fact_model(
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    category: str = "openapi_spec",
    is_authoritative: bool = True,
    t_invalidated_at: datetime.datetime | None = None,
    t_valid_to: datetime.datetime | None = None,
) -> MagicMock:
    f = MagicMock(spec=Fact)
    f.tenant_id = tenant_id
    f.entity_id = entity_id
    f.category = category
    f.is_authoritative = is_authoritative
    f.t_invalidated_at = t_invalidated_at
    f.t_valid_to = t_valid_to
    return f


def _build_service(
    entity: Entity | None,
    *,
    existing_auth_fact: Fact | None = None,
    existing_prev_fact: Fact | None = None,
) -> tuple[CatalogService, list[Fact]]:
    """Return a CatalogService wired with a mock session that:
    - Returns *entity* from ``session.get(Entity, ...)``
    - Returns *existing_auth_fact* from the authoritative conflict check SELECT
    - Returns *existing_prev_fact* from the prior non-auth row SELECT
    - Collects added facts.
    """
    inserted: list[Fact] = []

    # Alternate execute return values:
    # first call → auth conflict check, second call → prev open row check
    execute_results = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=existing_auth_fact)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=existing_prev_fact)),
    ]
    call_index: list[int] = [0]

    async def _execute(_stmt: Any, _params: Any = None) -> Any:
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(execute_results):
            return execute_results[idx]
        return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    session = AsyncMock()
    session.get = AsyncMock(return_value=entity)
    session.execute = AsyncMock(side_effect=_execute)
    session.flush = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: inserted.append(obj) if isinstance(obj, Fact) else None)

    # Support async context manager and nested SAVEPOINT for _enqueue_embedding
    savepoint_cm = AsyncMock()
    savepoint_cm.__aenter__ = AsyncMock(return_value=savepoint_cm)
    savepoint_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=savepoint_cm)
    session.execute = AsyncMock(side_effect=_execute)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    # begin() context manager
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=begin_cm)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session_factory = MagicMock(return_value=session_cm)

    vocab = MagicMock(spec=VocabularyService)
    vocab.validate_value = AsyncMock()
    schema = MagicMock(spec=SchemaService)

    svc = CatalogService(
        session_factory=session_factory,
        clock=FakeClock(_T0),
        vocabulary=vocab,
        schema=schema,
    )
    return svc, inserted


def _source(source_id: uuid.UUID | None = None) -> MagicMock:
    s = MagicMock()
    s.source_id = source_id or uuid.uuid4()
    return s


def _parsed_fact(
    entity_id: uuid.UUID | None = None,
    category: str = "openapi_spec",
    body: str = "{}",
) -> ParsedFact:
    return ParsedFact(
        entity_id=entity_id or uuid.uuid4(),
        category=category,
        body=body,
        valid_from=None,
        source_url="https://example.com",
        commit_sha=None,
    )


# ---------------------------------------------------------------------------
# Tests: create_fact_from_sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incoming_marked_superseded_when_authoritative_exists() -> None:
    """When an authoritative fact exists for (tenant, entity, category),
    the incoming synced fact is written with is_authoritative_superseded=True."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _entity(ctx.tenant_id, entity_id)
    auth_fact = _fact_model(tenant_id=ctx.tenant_id, entity_id=entity_id, is_authoritative=True)

    svc, inserted = _build_service(entity, existing_auth_fact=auth_fact)

    ref = await svc.create_fact_from_sync(
        ctx=ctx,
        entity_id=entity_id,
        category="openapi_spec",
        body="new body",
        sync_run_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
    )

    assert ref.is_authoritative is False
    assert ref.is_authoritative_superseded is True
    assert len(inserted) == 1
    assert inserted[0].is_authoritative_superseded is True


@pytest.mark.asyncio
async def test_incoming_created_cleanly_when_no_authoritative_exists() -> None:
    """No existing authoritative fact → incoming written as a normal synced fact
    (is_authoritative=False, is_authoritative_superseded=False)."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _entity(ctx.tenant_id, entity_id)

    svc, inserted = _build_service(entity, existing_auth_fact=None, existing_prev_fact=None)

    ref = await svc.create_fact_from_sync(
        ctx=ctx,
        entity_id=entity_id,
        category="openapi_spec",
        body="spec body",
        sync_run_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
    )

    assert ref.is_authoritative is False
    assert ref.is_authoritative_superseded is False
    assert len(inserted) == 1


@pytest.mark.asyncio
async def test_prior_open_nonauth_row_is_closed_on_re_sync() -> None:
    """When no authoritative blocker and a prior open non-auth row exists,
    that row's t_valid_to and t_invalidated_at are set (bi-temporal supersession)."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _entity(ctx.tenant_id, entity_id)
    prev = _fact_model(tenant_id=ctx.tenant_id, entity_id=entity_id, is_authoritative=False)
    prev.t_valid_to = None
    prev.t_invalidated_at = None

    svc, inserted = _build_service(entity, existing_auth_fact=None, existing_prev_fact=prev)

    await svc.create_fact_from_sync(
        ctx=ctx,
        entity_id=entity_id,
        category="openapi_spec",
        body="updated body",
        sync_run_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
    )

    # The prior row must have been closed.
    assert prev.t_valid_to == _T0
    assert prev.t_invalidated_at == _T0
    assert len(inserted) == 1


@pytest.mark.asyncio
async def test_prior_open_row_not_closed_when_authoritative_blocks() -> None:
    """When an authoritative fact blocks the write, the supersession of any
    prior open non-auth row is skipped (we don't touch existing rows)."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    entity = _entity(ctx.tenant_id, entity_id)
    auth_fact = _fact_model(tenant_id=ctx.tenant_id, entity_id=entity_id, is_authoritative=True)

    # prev row should never be touched when authoritative blocks
    prev = _fact_model(tenant_id=ctx.tenant_id, entity_id=entity_id, is_authoritative=False)
    prev.t_valid_to = None
    prev.t_invalidated_at = None

    svc, inserted = _build_service(entity, existing_auth_fact=auth_fact, existing_prev_fact=prev)

    ref = await svc.create_fact_from_sync(
        ctx=ctx,
        entity_id=entity_id,
        category="openapi_spec",
        body="blocked body",
        sync_run_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
    )

    # Authoritative row was found → superseded flag set on new row
    assert ref.is_authoritative_superseded is True
    # Prior non-auth row must NOT have been touched (no second execute call)
    assert prev.t_valid_to is None
    assert prev.t_invalidated_at is None


@pytest.mark.asyncio
async def test_entity_not_found_raises() -> None:
    """create_fact_from_sync raises NotFoundError when the entity does not exist."""
    ctx = _ctx()
    svc, _ = _build_service(entity=None)

    with pytest.raises(NotFoundError):
        await svc.create_fact_from_sync(
            ctx=ctx,
            entity_id=uuid.uuid4(),
            category="openapi_spec",
            body="body",
            sync_run_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# Helpers: FactService bulk-upsert mock builder
# ---------------------------------------------------------------------------


def _build_fact_service_with_bulk_session(
    *,
    entity_ids_present: list[uuid.UUID],
    auth_blocked_pairs: list[tuple[str, str]],  # (str(entity_id), category)
    prior_fact_ids: list[uuid.UUID],
    tenant_id: uuid.UUID,
) -> FactService:
    """Return a FactService wired with a mock session tuned for upsert_synced_facts.

    The mock session drives four execute() calls in order:
      1. Entity existence SELECT → rows with entity_ids_present.
      2. Auth-blocked pairs SELECT → rows with auth_blocked_pairs.
      3. Prior open rows SELECT → rows with prior_fact_ids.
      4. Bulk INSERT facts → no meaningful return value.
    Outbox INSERT via begin_nested is accepted silently.
    """
    # Each execute call needs a tailored result object.
    call_index: list[int] = [0]

    def _make_rows(pairs: list[Any]) -> MagicMock:
        result = MagicMock()
        result.__iter__ = MagicMock(return_value=iter(pairs))
        return result

    entity_rows = _make_rows([(eid,) for eid in entity_ids_present])
    auth_rows = _make_rows([(uuid.UUID(eid_s), cat) for eid_s, cat in auth_blocked_pairs])
    prior_rows = _make_rows([(fid,) for fid in prior_fact_ids])
    insert_result = MagicMock()
    # executemany-style: always returns something
    noop_result = MagicMock()
    noop_result.__iter__ = MagicMock(return_value=iter([]))

    call_sequence = [entity_rows, auth_rows, prior_rows, insert_result]

    async def _execute(_stmt: Any, _params: Any = None) -> Any:
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(call_sequence):
            return call_sequence[idx]
        return noop_result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)

    # begin_nested SAVEPOINT for outbox insert
    savepoint_cm = AsyncMock()
    savepoint_cm.__aenter__ = AsyncMock(return_value=savepoint_cm)
    savepoint_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=savepoint_cm)

    # begin() context manager
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=begin_cm)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session_cm)

    vocab = MagicMock(spec=VocabularyService)
    vocab.validate_value = AsyncMock()

    entity_svc = MagicMock(spec=EntityService)

    return FactService(
        session_factory=session_factory,
        clock=FakeClock(_T0),
        vocabulary=vocab,
        entity_service=entity_svc,
    )


# ---------------------------------------------------------------------------
# Tests: upsert_synced_facts bulk path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_synced_facts_bulk_counts_superseded_and_created() -> None:
    """Bulk upsert returns correct created/superseded counts across a batch."""
    ctx = _ctx()
    source = _source()
    sync_run_id = uuid.uuid4()

    entity_id_a = uuid.uuid4()
    entity_id_b = uuid.uuid4()

    pf_superseded = _parsed_fact(entity_id=entity_id_a, category="openapi_spec")
    pf_created = _parsed_fact(entity_id=entity_id_b, category="openapi_spec")

    svc = _build_fact_service_with_bulk_session(
        entity_ids_present=[entity_id_a, entity_id_b],
        auth_blocked_pairs=[(str(entity_id_a), "openapi_spec")],
        prior_fact_ids=[],
        tenant_id=ctx.tenant_id,
    )

    result = await svc.upsert_synced_facts(ctx, [pf_superseded, pf_created], sync_run_id, source)

    assert result.created == 1
    assert result.superseded == 1
    assert result.skipped == 0


@pytest.mark.asyncio
async def test_upsert_synced_facts_bulk_empty_batch() -> None:
    """Empty fact list returns all-zero counts without opening a transaction."""
    ctx = _ctx()
    source = _source()
    sync_run_id = uuid.uuid4()

    vocab = MagicMock(spec=VocabularyService)
    vocab.validate_value = AsyncMock()
    session_factory = MagicMock()
    entity_svc = MagicMock(spec=EntityService)

    svc = FactService(
        session_factory=session_factory,
        clock=FakeClock(_T0),
        vocabulary=vocab,
        entity_service=entity_svc,
    )

    result = await svc.upsert_synced_facts(ctx, [], sync_run_id, source)

    assert result.created == 0
    assert result.superseded == 0
    assert result.skipped == 0
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_synced_facts_bulk_100_facts_single_transaction() -> None:
    """100-fact batch issues O(1) execute calls, not 100 per-row transactions."""
    ctx = _ctx()
    source = _source()
    sync_run_id = uuid.uuid4()

    entity_id = uuid.uuid4()
    facts = [_parsed_fact(entity_id=entity_id, category="openapi_spec", body=f"body-{i}") for i in range(100)]

    svc = _build_fact_service_with_bulk_session(
        entity_ids_present=[entity_id],
        auth_blocked_pairs=[],
        prior_fact_ids=[],
        tenant_id=ctx.tenant_id,
    )

    # Patch make_chunk_plan to avoid real chunking logic in unit context
    with patch("registry.service.facts.make_chunk_plan", return_value=[]):
        result = await svc.upsert_synced_facts(ctx, facts, sync_run_id, source)

    assert result.created == 100
    assert result.superseded == 0

    # Verify the session was opened exactly once (single transaction)
    session_factory = svc._session_factory
    assert session_factory.call_count == 1


@pytest.mark.asyncio
async def test_upsert_synced_facts_bulk_conflict_same_key_different_content() -> None:
    """When (entity_id, category) has a live authoritative fact, the new row is
    written with is_authoritative_superseded=True (authoritative-wins policy)."""
    ctx = _ctx()
    source = _source()
    sync_run_id = uuid.uuid4()
    entity_id = uuid.uuid4()

    pf = _parsed_fact(entity_id=entity_id, category="openapi_spec", body="new content")

    svc = _build_fact_service_with_bulk_session(
        entity_ids_present=[entity_id],
        auth_blocked_pairs=[(str(entity_id), "openapi_spec")],
        prior_fact_ids=[],
        tenant_id=ctx.tenant_id,
    )

    with patch("registry.service.facts.make_chunk_plan", return_value=[]):
        result = await svc.upsert_synced_facts(ctx, [pf], sync_run_id, source)

    assert result.superseded == 1
    assert result.created == 0


@pytest.mark.asyncio
async def test_upsert_synced_facts_bulk_same_key_same_content_inserts_row() -> None:
    """When no authoritative blocker exists, the new row is always written
    (append-only bi-temporal table). The prior open row is closed."""
    ctx = _ctx()
    source = _source()
    sync_run_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    prior_fact_id = uuid.uuid4()

    pf = _parsed_fact(entity_id=entity_id, category="openapi_spec", body="same content")

    svc = _build_fact_service_with_bulk_session(
        entity_ids_present=[entity_id],
        auth_blocked_pairs=[],
        prior_fact_ids=[prior_fact_id],
        tenant_id=ctx.tenant_id,
    )

    with patch("registry.service.facts.make_chunk_plan", return_value=[]):
        result = await svc.upsert_synced_facts(ctx, [pf], sync_run_id, source)

    # Row is always written; prior row is closed.
    assert result.created == 1
    assert result.superseded == 0


@pytest.mark.asyncio
async def test_upsert_synced_facts_bulk_unknown_entity_raises() -> None:
    """Unknown entity_id in the batch raises NotFoundError before any writes."""
    ctx = _ctx()
    source = _source()
    sync_run_id = uuid.uuid4()
    unknown_entity_id = uuid.uuid4()

    pf = _parsed_fact(entity_id=unknown_entity_id, category="openapi_spec")

    svc = _build_fact_service_with_bulk_session(
        entity_ids_present=[],  # no entities found → unknown entity
        auth_blocked_pairs=[],
        prior_fact_ids=[],
        tenant_id=ctx.tenant_id,
    )

    with pytest.raises(NotFoundError):
        await svc.upsert_synced_facts(ctx, [pf], sync_run_id, source)


@pytest.mark.asyncio
async def test_upsert_synced_facts_catalog_delegates_to_fact_service() -> None:
    """CatalogService.upsert_synced_facts delegates to FactService without looping."""
    ctx = _ctx()
    source = _source()
    sync_run_id = uuid.uuid4()
    pf = _parsed_fact()

    expected_result = SyncWriteResult(created=1, skipped=0, superseded=0)

    svc = MagicMock(spec=CatalogService)
    svc._fact = MagicMock()
    svc._fact.upsert_synced_facts = AsyncMock(return_value=expected_result)

    result = await CatalogService.upsert_synced_facts(svc, ctx, [pf], sync_run_id, source)

    assert result == expected_result
    svc._fact.upsert_synced_facts.assert_awaited_once_with(ctx, [pf], sync_run_id, source)
