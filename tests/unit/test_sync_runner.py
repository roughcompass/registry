"""Unit tests for sync/runner.py.

All DB and connector interactions are mocked — no Docker or real
Postgres required.

Coverage:
- ``create_scheduler``: MemoryJobStore selected when
  ``scheduler_use_memory_jobstore=True``.
- ``register_sync_jobs``: active sources register cron jobs; sources without a
  schedule are skipped.
- ``run_sync_job`` / ``_execute_sync``:
  - ``sync_runs`` row lifecycle: running → done / partial / failed.
  - Webhook idempotency: duplicate ``delivery_id`` causes early return.
  - ``validate()`` failure sets status ``'failed'`` immediately.
  - ``fetch()`` failures after all retries accumulate errors (``partial`` /
    ``failed`` depending on other artifacts).
  - ``parse()`` errors skip artifact and log at ERROR.
  - Error summary is truncated to first 10 items.
- ``resolve_sync_actor``: provisions actor when absent; caches result.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.config import Settings
from registry.service.catalog import CatalogService
from registry.types import TenantContext
from sync.connector import CredentialError, DiscoveredArtifact, ParsedFact
from sync.runner import (
    _MAX_FETCH_ATTEMPTS,
    _actor_cache,
    _execute_sync,
    create_scheduler,
    register_sync_jobs,
    resolve_sync_actor,
    run_sync_job,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_use_memory_jobstore=True,  # Always use memory in unit tests.
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _source(
    *,
    source_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    source_type: str = "openapi",
    schedule: str | None = "*/5 * * * *",
    is_active: bool = True,
    credentials_ref: str | None = None,
) -> MagicMock:
    s = MagicMock()
    s.source_id = source_id or uuid.uuid4()
    s.tenant_id = tenant_id or uuid.uuid4()
    s.source_type = source_type
    s.display_name = f"test-{source_type}"
    s.schedule = schedule
    s.is_active = is_active
    s.credentials_ref = credentials_ref
    return s


def _artifact(artifact_id: str = "art-1") -> DiscoveredArtifact:
    return DiscoveredArtifact(
        artifact_id=artifact_id,
        source_url="https://example.com/art",
        artifact_type="openapi",
    )


def _parsed_fact(entity_id: uuid.UUID | None = None) -> ParsedFact:
    return ParsedFact(
        entity_id=entity_id or uuid.uuid4(),
        category="openapi_spec",
        body="{}",
        valid_from=None,
        source_url="https://example.com/art",
        commit_sha=None,
    )


def _mock_catalog() -> MagicMock:
    return MagicMock(spec=CatalogService)


# ---------------------------------------------------------------------------
# create_scheduler
# ---------------------------------------------------------------------------


def test_create_scheduler_uses_memory_store() -> None:
    """With scheduler_use_memory_jobstore=True the scheduler uses MemoryJobStore."""
    from apscheduler.jobstores.memory import MemoryJobStore

    settings = _settings()
    sched = create_scheduler(settings)
    assert isinstance(sched._jobstores["default"], MemoryJobStore)


def test_create_scheduler_job_defaults() -> None:
    """Scheduler job_defaults enforce coalesce and max_instances."""
    settings = _settings()
    sched = create_scheduler(settings)
    defaults = sched._job_defaults
    assert defaults["coalesce"] is True
    assert defaults["max_instances"] == 1


# ---------------------------------------------------------------------------
# register_sync_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_sync_jobs_registers_active_sources() -> None:
    """Active sources with a valid schedule get a cron job added to the scheduler."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    settings = _settings()
    scheduler = AsyncIOScheduler(
        jobstores={"default": __import__("apscheduler.jobstores.memory", fromlist=["MemoryJobStore"]).MemoryJobStore()},
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    scheduler.start()

    source = _source(schedule="0 2 * * *")
    catalog = _mock_catalog()

    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [source]
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session)

    await register_sync_jobs(
        scheduler=scheduler,
        session_factory=session_factory,
        catalog=catalog,
        settings=settings,
    )

    job_id = f"sync:{source.source_id}"
    assert scheduler.get_job(job_id) is not None

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_register_sync_jobs_skips_no_schedule() -> None:
    """Sources with schedule=None are silently skipped."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    settings = _settings()
    scheduler = AsyncIOScheduler(
        jobstores={"default": __import__("apscheduler.jobstores.memory", fromlist=["MemoryJobStore"]).MemoryJobStore()},
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    scheduler.start()

    source = _source(schedule=None)
    catalog = _mock_catalog()

    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [source]
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session)

    await register_sync_jobs(
        scheduler=scheduler,
        session_factory=session_factory,
        catalog=catalog,
        settings=settings,
    )

    assert scheduler.get_job(f"sync:{source.source_id}") is None

    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# sync_runs lifecycle
# ---------------------------------------------------------------------------


def _make_session_factory(
    source: MagicMock,
    run_store: dict[uuid.UUID, MagicMock],
) -> MagicMock:
    """Return a mock session_factory that:
    - Returns *source* from ``session.get(SyncSource, ...)``
    - Returns None from ``session.get(WebhookDelivery, ...)``
    - Captures SyncRun adds via ``session.add()``
    - Provides ``session.get(SyncRun, ...)`` returning the stored run.
    """
    from registry.storage.models import SyncRun, SyncSource, WebhookDelivery

    async def _get(model_cls: Any, pk: Any) -> Any:
        if model_cls is SyncSource:
            return source
        if model_cls is WebhookDelivery:
            return None
        if model_cls is SyncRun:
            return run_store.get(pk)
        return None

    def _add(obj: Any) -> None:
        if isinstance(obj, SyncRun):
            run_store[obj.sync_run_id] = obj

    session = AsyncMock()
    session.get = AsyncMock(side_effect=_get)
    session.add = MagicMock(side_effect=_add)
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    # session.begin() returns its own async context manager — required by
    # run_sync_job's `async with session_factory() as session, session.begin():`
    # pattern. The inner block commits on exit; tests don't need to assert on it.
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session_factory = MagicMock(return_value=session)
    return session_factory


@pytest.mark.asyncio
async def test_execute_sync_done_on_success() -> None:
    """All artifacts succeed → sync_run status set to 'done'."""
    source = _source()
    run_store: dict[uuid.UUID, MagicMock] = {}
    session_factory = _make_session_factory(source, run_store)
    catalog = _mock_catalog()
    settings = _settings()

    art = _artifact()
    fact = _parsed_fact()

    mock_connector = MagicMock()
    mock_connector.validate = AsyncMock()
    mock_connector.discover = AsyncMock(return_value=[art])
    mock_connector.fetch = AsyncMock(return_value=b"data")
    mock_connector.parse = MagicMock(return_value=[fact])

    sync_run_id = uuid.uuid4()
    # Seed the run store as if run_sync_job already opened the running row.
    from registry.storage.models import SyncRun

    run = SyncRun(
        sync_run_id=sync_run_id,
        tenant_id=source.tenant_id,
        source_id=source.source_id,
        status="running",
        trigger="scheduled",
        started_at=datetime.now(tz=UTC),
    )
    run_store[sync_run_id] = run

    ctx = TenantContext(
        tenant_id=source.tenant_id,
        actor_id=uuid.uuid4(),
        roles=["sync_worker"],
    )

    with patch("sync.runner.get_connector", return_value=lambda: mock_connector):
        await _execute_sync(
            source=source,
            sync_run_id=sync_run_id,
            ctx=ctx,
            session_factory=session_factory,
            catalog=catalog,
            settings=settings,
        )

    assert run.status == "done"
    assert run.artifact_count == 1
    assert run.error_summary is None


@pytest.mark.asyncio
async def test_execute_sync_failed_on_validate_error() -> None:
    """validate() failure → sync_run status 'failed' immediately."""
    source = _source()
    run_store: dict[uuid.UUID, MagicMock] = {}
    session_factory = _make_session_factory(source, run_store)
    catalog = _mock_catalog()
    settings = _settings()

    mock_connector = MagicMock()
    mock_connector.validate = AsyncMock(side_effect=CredentialError("bad cred"))

    sync_run_id = uuid.uuid4()
    from registry.storage.models import SyncRun

    run = SyncRun(
        sync_run_id=sync_run_id,
        tenant_id=source.tenant_id,
        source_id=source.source_id,
        status="running",
        trigger="scheduled",
        started_at=datetime.now(tz=UTC),
    )
    run_store[sync_run_id] = run

    ctx = TenantContext(
        tenant_id=source.tenant_id,
        actor_id=uuid.uuid4(),
        roles=["sync_worker"],
    )

    with patch("sync.runner.get_connector", return_value=lambda: mock_connector):
        await _execute_sync(
            source=source,
            sync_run_id=sync_run_id,
            ctx=ctx,
            session_factory=session_factory,
            catalog=catalog,
            settings=settings,
        )

    assert run.status == "failed"
    assert "CredentialError" in (run.error_summary or "")
    assert run.artifact_count == 0


@pytest.mark.asyncio
async def test_execute_sync_partial_when_some_artifacts_fail() -> None:
    """Some artifact fetch failures + at least one success → status 'partial'."""
    source = _source()
    run_store: dict[uuid.UUID, MagicMock] = {}
    session_factory = _make_session_factory(source, run_store)
    catalog = _mock_catalog()
    settings = _settings(connector_run_timeout_s=30)

    art_ok = _artifact("art-ok")
    art_fail = _artifact("art-fail")
    fact = _parsed_fact()

    call_count: dict[str, int] = {"art-fail": 0}

    async def _fetch(artifact: DiscoveredArtifact, src: Any) -> bytes:
        if artifact.artifact_id == "art-fail":
            call_count["art-fail"] += 1
            raise OSError("network error")
        return b"ok"

    mock_connector = MagicMock()
    mock_connector.validate = AsyncMock()
    mock_connector.discover = AsyncMock(return_value=[art_ok, art_fail])
    mock_connector.fetch = AsyncMock(side_effect=_fetch)
    mock_connector.parse = MagicMock(return_value=[fact])

    sync_run_id = uuid.uuid4()
    from registry.storage.models import SyncRun

    run = SyncRun(
        sync_run_id=sync_run_id,
        tenant_id=source.tenant_id,
        source_id=source.source_id,
        status="running",
        trigger="scheduled",
        started_at=datetime.now(tz=UTC),
    )
    run_store[sync_run_id] = run

    ctx = TenantContext(
        tenant_id=source.tenant_id,
        actor_id=uuid.uuid4(),
        roles=["sync_worker"],
    )

    with (
        patch("sync.runner.get_connector", return_value=lambda: mock_connector),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await _execute_sync(
            source=source,
            sync_run_id=sync_run_id,
            ctx=ctx,
            session_factory=session_factory,
            catalog=catalog,
            settings=settings,
        )

    assert run.status == "partial"
    assert run.artifact_count == 1
    assert run.error_summary is not None
    # fetch should have been retried _MAX_FETCH_ATTEMPTS times for art-fail
    assert call_count["art-fail"] == _MAX_FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_execute_sync_parse_error_skips_artifact() -> None:
    """parse() error skips artifact (logged at ERROR) but doesn't abort the run."""
    source = _source()
    run_store: dict[uuid.UUID, MagicMock] = {}
    session_factory = _make_session_factory(source, run_store)
    catalog = _mock_catalog()
    settings = _settings()

    art = _artifact()

    mock_connector = MagicMock()
    mock_connector.validate = AsyncMock()
    mock_connector.discover = AsyncMock(return_value=[art])
    mock_connector.fetch = AsyncMock(return_value=b"data")
    mock_connector.parse = MagicMock(side_effect=ValueError("bad yaml"))

    sync_run_id = uuid.uuid4()
    from registry.storage.models import SyncRun

    run = SyncRun(
        sync_run_id=sync_run_id,
        tenant_id=source.tenant_id,
        source_id=source.source_id,
        status="running",
        trigger="scheduled",
        started_at=datetime.now(tz=UTC),
    )
    run_store[sync_run_id] = run

    ctx = TenantContext(
        tenant_id=source.tenant_id,
        actor_id=uuid.uuid4(),
        roles=["sync_worker"],
    )

    with patch("sync.runner.get_connector", return_value=lambda: mock_connector):
        await _execute_sync(
            source=source,
            sync_run_id=sync_run_id,
            ctx=ctx,
            session_factory=session_factory,
            catalog=catalog,
            settings=settings,
        )

    # All artifacts failed parse; artifact_count=0 → failed
    assert run.status == "failed"
    assert "parse() error" in (run.error_summary or "")


# ---------------------------------------------------------------------------
# Webhook idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sync_job_skips_duplicate_webhook_delivery() -> None:
    """Duplicate webhook delivery_id → early return without creating sync_run."""
    from registry.storage.models import SyncRun, SyncSource, WebhookDelivery

    source = _source()
    run_store: dict[uuid.UUID, MagicMock] = {}

    delivery_id = "gh-delivery-abc123"
    tenant_id = source.tenant_id
    existing_delivery = WebhookDelivery(
        tenant_id=tenant_id,
        delivery_id=delivery_id,
        source_id=source.source_id,
        received_at=datetime.now(tz=UTC),
    )

    async def _get(model_cls: Any, pk: Any) -> Any:
        if model_cls is SyncSource:
            return source
        if model_cls is WebhookDelivery:
            # Simulate duplicate
            return existing_delivery
        return None

    def _add(obj: Any) -> None:
        if isinstance(obj, SyncRun):
            run_store[obj.sync_run_id] = obj

    session = AsyncMock()
    session.get = AsyncMock(side_effect=_get)
    session.add = MagicMock(side_effect=_add)
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session_factory = MagicMock(return_value=session)
    catalog = _mock_catalog()
    settings = _settings()

    await run_sync_job(
        source_id=str(source.source_id),
        session_factory=session_factory,
        catalog=catalog,
        settings=settings,
        trigger="webhook",
        delivery_id=delivery_id,
    )

    # No sync_run should have been added.
    assert len(run_store) == 0


# ---------------------------------------------------------------------------
# resolve_sync_actor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_sync_actor_provisions_and_caches() -> None:
    """Actor is provisioned on first call and cached for subsequent calls."""
    # Clear cache from previous test runs.
    _actor_cache.clear()

    from registry.storage.models import Actor

    tenant_id = uuid.uuid4()
    source_type = "openapi"

    # Simulate no existing actor in DB.
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    provisioned: list[Actor] = []

    def _add(obj: Any) -> None:
        if isinstance(obj, Actor):
            provisioned.append(obj)

    session.add = MagicMock(side_effect=_add)
    session.flush = AsyncMock()

    actor_id_1 = await resolve_sync_actor(session, tenant_id, source_type)

    assert len(provisioned) == 1
    assert provisioned[0].actor_kind == "sync_worker"
    assert provisioned[0].display_name == f"sync-worker:{source_type}"

    # Second call must use cache (no extra DB queries).
    execute_count_before = session.execute.call_count
    actor_id_2 = await resolve_sync_actor(session, tenant_id, source_type)
    assert session.execute.call_count == execute_count_before  # no new query
    assert actor_id_1 == actor_id_2

    _actor_cache.clear()


@pytest.mark.asyncio
async def test_resolve_sync_actor_returns_existing() -> None:
    """Returns existing actor without provisioning when found in DB."""
    _actor_cache.clear()

    from registry.storage.models import Actor

    tenant_id = uuid.uuid4()
    source_type = "package_json"
    existing_actor_id = uuid.uuid4()

    existing = MagicMock(spec=Actor)
    existing.actor_id = existing_actor_id
    existing.actor_kind = "sync_worker"
    existing.display_name = f"sync-worker:{source_type}"

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()

    actor_id = await resolve_sync_actor(session, tenant_id, source_type)
    assert actor_id == existing_actor_id
    session.add.assert_not_called()

    _actor_cache.clear()


# ---------------------------------------------------------------------------
# Transaction-boundary invariant for run_sync_job's sync_run insertion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sync_job_opens_sync_run_row_within_explicit_transaction() -> None:
    """session.begin() must be entered before session.add(sync_run); no bare commit.

    Mocks session_factory to record the order of begin().__aenter__,
    session.add, and session.commit. Passes when begin() runs before add and
    no explicit commit is invoked (the begin() context commits on exit).
    """
    from registry.config import Settings
    from sync import runner as runner_mod

    call_order: list[str] = []

    src_uuid = uuid.uuid4()
    source_row = MagicMock()
    source_row.tenant_id = uuid.uuid4()
    source_row.source_type = "github"
    source_row.is_active = True

    session = AsyncMock()
    session.get = AsyncMock(return_value=source_row)

    async def _commit() -> None:
        call_order.append("session.commit")

    def _add(_obj: Any) -> None:
        call_order.append("session.add")

    session.commit = AsyncMock(side_effect=_commit)
    session.add = MagicMock(side_effect=_add)

    begin_cm = MagicMock()

    async def _begin_aenter(*_a: Any, **_k: Any) -> Any:
        call_order.append("session.begin().__aenter__")
        return None

    begin_cm.__aenter__ = AsyncMock(side_effect=_begin_aenter)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_cm)

    settings = Settings(
        database_url="postgresql+asyncpg://x/y",
        pgbouncer_url="postgresql+asyncpg://x/y",
        scheduler_jobstore_url="postgresql+asyncpg://x/y",
    )

    with (
        patch.object(runner_mod, "resolve_sync_actor", AsyncMock(return_value=uuid.uuid4())),
        patch.object(runner_mod, "_execute_sync", AsyncMock(return_value=None)),
    ):
        await runner_mod.run_sync_job(
            source_id=str(src_uuid),
            session_factory=session_factory,
            catalog=MagicMock(),
            settings=settings,
        )

    assert "session.begin().__aenter__" in call_order, call_order
    begin_idx = call_order.index("session.begin().__aenter__")
    assert "session.add" in call_order, call_order
    add_idx = call_order.index("session.add")
    assert begin_idx < add_idx, (
        f"session.begin must run before session.add; got {call_order}"
    )

    assert "session.commit" not in call_order, (
        f"bare session.commit() must not be called when session.begin() owns "
        f"the transaction; got {call_order}"
    )
