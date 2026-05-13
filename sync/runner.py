"""Sync runner — APScheduler dispatch loop for all sync sources.

Design notes
------------
* One ``AsyncIOScheduler`` is shared between the embedding-drain job and all
  sync-source jobs.  ``create_scheduler`` is called by ``registry/main.py``;
  callers must start / shutdown it.
* ``SQLAlchemyJobStore`` requires a *synchronous* SQLAlchemy URL.  The helper
  ``_make_jobstore`` rewrites ``postgresql+asyncpg://`` →
  ``postgresql+psycopg2://`` so the jobstore's internal sync engine works.
  When ``settings.scheduler_use_memory_jobstore`` is ``True`` (unit tests,
  environments without a sync driver installed), a ``MemoryJobStore`` is used
  instead.
* Per-artifact retry: 3 attempts with exponential back-off on ``fetch()``
  network errors.  ``parse()`` errors skip the artifact and log at ERROR.
* Webhook idempotency: checked via ``webhook_deliveries(tenant_id, delivery_id)``
  before opening a ``sync_runs`` row.
* Facts are written via ``CatalogService`` (not direct DB).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from apscheduler.jobstores.memory import MemoryJobStore  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.config import Settings
from registry.service.catalog import CatalogService
from registry.storage.models import Actor, SyncRun, SyncSource, WebhookDelivery
from registry.types import TenantContext
from sync.connector import CredentialError
from sync.registry import UnknownConnectorError, get_connector

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-process actor-id cache: (tenant_id, source_type) → actor_id
# ---------------------------------------------------------------------------
_actor_cache: dict[tuple[uuid.UUID, str], uuid.UUID] = {}


# ---------------------------------------------------------------------------
# JobStore factory
# ---------------------------------------------------------------------------


def _make_jobstore(settings: Settings) -> Any:
    """Return either a ``SQLAlchemyJobStore`` (Postgres) or ``MemoryJobStore``.

    ``SQLAlchemyJobStore`` expects a *synchronous* URL.  The project's
    ``scheduler_jobstore_url`` is the same asyncpg URL as ``database_url``.
    We rewrite ``+asyncpg`` to ``+psycopg2``; if that fails (driver absent),
    we fall back to memory (config toggle ``scheduler_use_memory_jobstore``
    also forces memory — handy for unit tests).
    """
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # type: ignore[import-untyped]

    if settings.scheduler_use_memory_jobstore:
        _log.info("scheduler: using MemoryJobStore (scheduler_use_memory_jobstore=True)")
        return MemoryJobStore()

    url = settings.scheduler_jobstore_url
    # Rewrite asyncpg → psycopg2 for the synchronous jobstore engine.
    sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    # If unchanged it was already a sync URL (or a non-asyncpg DSN).
    try:
        store = SQLAlchemyJobStore(url=sync_url)
        _log.info("scheduler: using SQLAlchemyJobStore url=%s", _redact_url(sync_url))
        return store
    except Exception as exc:
        _log.warning(
            "scheduler: SQLAlchemyJobStore init failed (%s); falling back to MemoryJobStore",
            exc,
        )
        return MemoryJobStore()


def _redact_url(url: str) -> str:
    """Strip password from URL for logging."""
    try:
        from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

        p = urlparse(url)
        redacted = p._replace(netloc=p.netloc.split("@")[-1])
        return urlunparse(redacted)
    except Exception:
        return "<url>"


# ---------------------------------------------------------------------------
# Scheduler factory (called by registry/main.py)
# ---------------------------------------------------------------------------


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Build an ``AsyncIOScheduler`` with the configured jobstore.

    Callers are responsible for ``scheduler.start()`` / ``scheduler.shutdown()``.
    ``job_defaults`` enforce ``coalesce=True`` and ``max_instances=1`` across all
    jobs so missed cron firings don't pile up.
    """
    jobstore = _make_jobstore(settings)
    scheduler = AsyncIOScheduler(
        jobstores={"default": jobstore},
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    return scheduler


# ---------------------------------------------------------------------------
# Sync-source registration
# ---------------------------------------------------------------------------


async def register_sync_jobs(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    catalog: CatalogService,
    settings: Settings,
) -> None:
    """Query active ``sync_sources`` and register cron jobs on *scheduler*.

    Safe to call multiple times — ``replace_existing=True`` ensures re-entrant
    startup doesn't duplicate jobs.  Called from the FastAPI lifespan *after*
    ``scheduler.start()``.
    """
    async with session_factory() as session:
        result = await session.execute(select(SyncSource).where(SyncSource.is_active.is_(True)))
        sources: list[SyncSource] = list(result.scalars().all())

    for source in sources:
        if not source.schedule:
            _log.warning(
                "sync_source %s (%s) has no schedule; skipping",
                source.source_id,
                source.display_name,
            )
            continue

        job_id = f"sync:{source.source_id}"
        cron_parts = source.schedule.split()
        if len(cron_parts) != 5:  # noqa: PLR2004
            _log.error(
                "sync_source %s has invalid cron expression %r; skipping",
                source.source_id,
                source.schedule,
            )
            continue

        minute, hour, day, month, day_of_week = cron_parts
        scheduler.add_job(
            run_sync_job,
            trigger="cron",
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            kwargs={
                "source_id": str(source.source_id),
                "session_factory": session_factory,
                "catalog": catalog,
                "settings": settings,
                "trigger": "scheduled",
            },
            id=job_id,
            replace_existing=True,
            name=f"sync:{source.display_name}",
        )
        _log.info(
            "registered sync job %s schedule=%r source_type=%s",
            job_id,
            source.schedule,
            source.source_type,
        )


# ---------------------------------------------------------------------------
# Sync job entry point (called by scheduler)
# ---------------------------------------------------------------------------


async def run_sync_job(
    source_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    catalog: CatalogService,
    settings: Settings,
    trigger: str = "scheduled",
    delivery_id: str | None = None,
) -> None:
    """Top-level coroutine executed by the scheduler for one sync source.

    Args:
        source_id: UUID string of the ``sync_sources`` row.
        session_factory: Async session factory.
        catalog: ``CatalogService`` instance.
        settings: Application settings.
        trigger: One of ``'scheduled'``, ``'manual'``, ``'webhook'``.
        delivery_id: For webhook triggers only — checked for idempotency.
    """
    sid = uuid.UUID(source_id)

    # Explicit session.begin() — matches the codebase-wide pattern and avoids
    # the autobegin-rollback footgun if this is ever nested inside an outer
    # transaction context (e.g. in tests). session.begin() commits on
    # successful exit so no manual session.commit() is needed.
    async with session_factory() as session, session.begin():
        source_row = await session.get(SyncSource, sid)
        if source_row is None:
            _log.error("sync job: source_id=%s not found; aborting", source_id)
            return
        if not source_row.is_active:
            _log.info("sync job: source_id=%s is_active=False; skipping", source_id)
            return

        # Webhook idempotency check.
        if trigger == "webhook" and delivery_id is not None:
            dup = await _check_webhook_duplicate(session, source_row.tenant_id, delivery_id)
            if dup:
                _log.info(
                    "sync job: duplicate webhook delivery_id=%s tenant=%s; skipping",
                    delivery_id,
                    source_row.tenant_id,
                )
                return

        # Resolve actor_id for the sync-worker identity.
        actor_id = await resolve_sync_actor(session, source_row.tenant_id, source_row.source_type)

        # Open sync_runs row.
        now = datetime.now(tz=UTC)
        sync_run = SyncRun(
            sync_run_id=uuid.uuid4(),
            tenant_id=source_row.tenant_id,
            source_id=sid,
            status="running",
            trigger=trigger,
            started_at=now,
        )
        session.add(sync_run)

    ctx = TenantContext(
        tenant_id=source_row.tenant_id,
        actor_id=actor_id,
        roles=["sync_worker"],
    )

    await _execute_sync(
        source=source_row,
        sync_run_id=sync_run.sync_run_id,
        ctx=ctx,
        session_factory=session_factory,
        catalog=catalog,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Core dispatch: validate → discover → fetch/parse per artifact
# ---------------------------------------------------------------------------

_MAX_FETCH_ATTEMPTS = 3
_BACKOFF_BASE_S = 2.0


async def _execute_sync(
    source: SyncSource,
    sync_run_id: uuid.UUID,
    ctx: TenantContext,
    session_factory: async_sessionmaker[AsyncSession],
    catalog: CatalogService,
    settings: Settings,
) -> None:
    """Run the full discover→fetch→parse→upsert cycle for one sync source.

    Updates ``sync_runs.status`` on completion.
    """
    started = time.monotonic()
    errors: list[str] = []
    artifact_count = 0

    try:
        ConnectorClass = get_connector(source.source_type)
    except UnknownConnectorError as exc:
        _log.error("sync: %s", exc)
        await _finish_run(
            session_factory,
            sync_run_id,
            status="failed",
            artifact_count=0,
            duration_s=int(time.monotonic() - started),
            error_summary=str(exc),
        )
        return

    connector = ConnectorClass()

    # Step 1 — validate credentials.
    try:
        await connector.validate(source.credentials_ref)
    except CredentialError as exc:
        _log.error("sync: credential error source=%s: %s", source.source_id, exc)
        await _finish_run(
            session_factory,
            sync_run_id,
            status="failed",
            artifact_count=0,
            duration_s=int(time.monotonic() - started),
            error_summary=f"CredentialError: {exc}",
        )
        return
    except Exception as exc:
        _log.error("sync: validate() failed source=%s: %s", source.source_id, exc)
        await _finish_run(
            session_factory,
            sync_run_id,
            status="failed",
            artifact_count=0,
            duration_s=int(time.monotonic() - started),
            error_summary=f"validate() error: {exc}",
        )
        return

    # Step 2 — discover artifacts.
    try:
        artifacts = await asyncio.wait_for(
            connector.discover(source),
            timeout=settings.connector_run_timeout_s,
        )
    except Exception as exc:
        _log.error("sync: discover() failed source=%s: %s", source.source_id, exc)
        await _finish_run(
            session_factory,
            sync_run_id,
            status="failed",
            artifact_count=0,
            duration_s=int(time.monotonic() - started),
            error_summary=f"discover() error: {exc}",
        )
        return

    # Step 3 — fetch + parse each artifact.
    for artifact in artifacts:
        raw: bytes | None = None
        fetch_error: str | None = None

        # Per-artifact retry on network errors.
        for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
            try:
                raw = await connector.fetch(artifact, source)
                break
            except Exception as exc:
                wait = _BACKOFF_BASE_S ** (attempt - 1)
                _log.warning(
                    "sync: fetch attempt %d/%d failed artifact=%s: %s; retrying in %.1fs",
                    attempt,
                    _MAX_FETCH_ATTEMPTS,
                    artifact.artifact_id,
                    exc,
                    wait,
                )
                if attempt < _MAX_FETCH_ATTEMPTS:
                    await asyncio.sleep(wait)
                else:
                    fetch_error = f"fetch() failed after {_MAX_FETCH_ATTEMPTS} attempts: {exc}"

        if fetch_error is not None:
            errors.append(f"{artifact.artifact_id}: {fetch_error}")
            continue

        assert raw is not None

        # parse() is pure; wrap in try/except and skip on error.
        try:
            facts = connector.parse(artifact, raw)
        except Exception as exc:
            msg = f"{artifact.artifact_id}: parse() error: {exc}"
            _log.error("sync: %s source=%s", msg, source.source_id)
            errors.append(msg)
            continue

        # Delegate to CatalogService so conflict policy is applied centrally.
        try:
            await _upsert_synced_facts(catalog, ctx, facts, sync_run_id, source)
        except Exception as exc:
            msg = f"{artifact.artifact_id}: upsert error: {exc}"
            _log.error("sync: %s source=%s", msg, source.source_id)
            errors.append(msg)
            continue

        artifact_count += 1

    # Step 4 — close sync_run.
    if not errors:
        final_status = "done"
    elif artifact_count > 0:
        final_status = "partial"
    else:
        final_status = "failed"

    await _finish_run(
        session_factory,
        sync_run_id,
        status=final_status,
        artifact_count=artifact_count,
        duration_s=int(time.monotonic() - started),
        error_summary="; ".join(errors[:10]) if errors else None,
    )

    _log.info(
        "sync: run=%s source=%s status=%s artifacts=%d errors=%d",
        sync_run_id,
        source.source_id,
        final_status,
        artifact_count,
        len(errors),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _check_webhook_duplicate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    delivery_id: str,
) -> bool:
    """Return ``True`` if ``(tenant_id, delivery_id)`` already exists in ``webhook_deliveries``."""
    row = await session.get(WebhookDelivery, (tenant_id, delivery_id))
    return row is not None


async def resolve_sync_actor(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_type: str,
) -> uuid.UUID:
    """Return the ``actor_id`` for the sync-worker actor, creating it if absent.

    Cache key: ``(tenant_id, source_type)``.  The actor's ``display_name``
    follows the pattern ``sync-worker:{source_type}``.  Provisions the actor
    on first use so sync runs always have a traceable identity in the audit log.
    """
    cache_key = (tenant_id, source_type)
    if cache_key in _actor_cache:
        return _actor_cache[cache_key]

    display_name = f"sync-worker:{source_type}"
    result = await session.execute(
        select(Actor).where(
            Actor.tenant_id == tenant_id,
            Actor.display_name == display_name,
            Actor.actor_kind == "sync_worker",
        )
    )
    actor = result.scalar_one_or_none()
    if actor is None:
        # Provision the sync-worker actor on first use.
        actor = Actor(
            actor_id=uuid.uuid4(),
            tenant_id=tenant_id,
            display_name=display_name,
            actor_kind="sync_worker",
            created_at=datetime.now(tz=UTC),
        )
        session.add(actor)
        await session.flush()
        _log.info(
            "sync: provisioned sync_worker actor %s for tenant=%s source_type=%s",
            actor.actor_id,
            tenant_id,
            source_type,
        )

    _actor_cache[cache_key] = actor.actor_id
    return actor.actor_id


async def _finish_run(
    session_factory: async_sessionmaker[AsyncSession],
    sync_run_id: uuid.UUID,
    status: str,
    artifact_count: int,
    duration_s: int,
    error_summary: str | None,
) -> None:
    """Update ``sync_runs`` row to its terminal status."""
    async with session_factory() as session:
        run = await session.get(SyncRun, sync_run_id)
        if run is None:
            _log.error("_finish_run: sync_run_id=%s not found", sync_run_id)
            return
        run.status = status
        run.finished_at = datetime.now(tz=UTC)
        run.duration_s = duration_s
        run.artifact_count = artifact_count
        run.error_summary = error_summary
        await session.commit()


async def _upsert_synced_facts(
    catalog: CatalogService,
    ctx: TenantContext,
    facts: list[Any],
    sync_run_id: uuid.UUID,
    source: SyncSource,
) -> None:
    """Delegate to ``CatalogService.upsert_synced_facts``.

    Applies the authoritative-wins conflict policy for each ``ParsedFact``
    in *facts*.  Logs the write-result counts so each sync run is traceable
    without a live DB query.
    """
    result = await catalog.upsert_synced_facts(ctx, facts, sync_run_id, source)
    _log.info(
        "sync upsert complete run=%s source=%s created=%d skipped=%d superseded=%d",
        sync_run_id,
        source.source_id,
        result.created,
        result.skipped,
        result.superseded,
    )


__all__ = [
    "create_scheduler",
    "register_sync_jobs",
    "resolve_sync_actor",
    "run_sync_job",
]
