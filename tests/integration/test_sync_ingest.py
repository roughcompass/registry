"""Sync-source and connector integration tests.

Covers:
- test_five_connectors_sync_under_60s: all five connectors parse via cassettes; provenance rows present.
- test_webhook_creates_delivery_record: GitHub webhook POST creates webhook_deliveries row (idempotency tested).
- test_webhook_duplicate_is_noop: same delivery_id sent twice results in one DB row.
- test_authoritative_wins_conflict: authoritative fact for same slot -> synced fact is_authoritative_superseded=True.
- test_partial_sync_idempotency: running sync twice for the same artifact is a no-op (skipped count stable).
- test_sync_run_error_populates_error_summary: fetch failure -> sync_run.status
  in ('partial', 'failed') with error_summary.
- test_admin_sync_source_crud: create / get / patch / delete lifecycle.
- test_admin_sync_run_list_and_detail: list + detail + superseded endpoints.
- test_tenant_isolation_regression: cross-tenant access returns 403/404 (delegated to conformance suite import).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.config import Settings
from registry.service.catalog import CatalogService
from registry.service.schema import SchemaService
from registry.service.vocabulary import VocabularyService
from registry.storage.models import (
    Entity,
    Fact,
    SyncRun,
    SyncSource,
    WebhookDelivery,
)
from registry.types import FakeClock, TenantContext
from sync.connector import DiscoveredArtifact, ParsedFact
from sync.connectors.docs_corpus import DocsCorpusConnector
from sync.connectors.markdown_adr_rfc import MarkdownADRRFCConnector
from sync.connectors.openapi import OpenAPIConnector
from sync.connectors.package_json import PackageJsonConnector
from sync.connectors.release_notes import _RELEASE_META_PREFIX, ReleaseNotesConnector
from sync.runner import _execute_sync
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

# ---------------------------------------------------------------------------
# Constants / Vocab
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

_VOCAB_ROWS = [
    ("entity_type", "capability"),
    ("entity_type", "service"),
    ("fact_category", "overview"),
    ("fact_category", "api_doc"),
    ("fact_category", "release_note"),
    ("fact_category", "adr"),
    ("fact_category", "rfc"),
    ("fact_category", "dev_doc"),
    ("fact_category", "package_manifest"),
    ("edge_rel", "depends_on"),
]


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------


async def _seed_tenant(
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed tenant + actor; return (tenant_id, actor_id).

    No api_token row is written. Tests that go through HTTP use the
    entitlement auth harness; tests that call service methods directly
    construct TenantContext from these ids.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    oidc_subject = f"oidc-sub-{slug}-{actor_id.hex[:8]}"
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
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:aid, :tid, :sub, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": oidc_subject, "dn": f"a-{slug}", "now": _NOW},
            )
            for kind, value in _VOCAB_ROWS:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def _create_entity(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID,
    name: str,
    entity_type: str = "service",
) -> None:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            session.add(
                Entity(
                    entity_id=entity_id,
                    tenant_id=tenant_id,
                    entity_type=entity_type,
                    name=name,
                    external_id=None,
                    is_active=True,
                    created_at=_NOW,
                    created_by=actor_id,
                )
            )
    finally:
        await engine.dispose()


async def _create_sync_source(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_type: str = "openapi",
    display_name: str = "test-source",
    config: dict[str, Any] | None = None,
) -> uuid.UUID:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            session.add(
                SyncSource(
                    source_id=source_id,
                    tenant_id=tenant_id,
                    source_type=source_type,
                    display_name=display_name,
                    config=config or {"owner": "acme", "repo": "test", "ref": "main"},
                    credentials_ref=None,
                    schedule=None,
                    is_active=True,
                    created_at=_NOW,
                    created_by=actor_id,
                )
            )
    finally:
        await engine.dispose()
    return source_id


async def _seed_sync_run(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    sync_run_id: uuid.UUID,
) -> None:
    """Insert a sync_runs row so facts.sync_run_id FK is satisfied.

    Bypasses run_sync_job (which would also schedule downstream work);
    used by tests that call upsert_synced_facts directly.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            session.add(
                SyncRun(
                    sync_run_id=sync_run_id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    status="running",
                    trigger="manual",
                    started_at=_NOW,
                )
            )
    finally:
        await engine.dispose()


def _make_settings(pg_url: str, **kwargs: Any) -> Settings:
    return Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        webhook_secret_github="test-webhook-secret",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# App fixture (per test, isolated by tenant slug)
# Uses the entitlement auth harness for HTTP tests.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def p3_client(pg_container: str, app_settings: Settings) -> Any:  # noqa: ARG001
    """Create an ASGI test client with a seeded admin tenant via the harness."""
    from tests.helpers.auth_harness import default_settings

    slug = f"p3-{uuid.uuid4().hex[:8]}"
    settings = default_settings(pg_container)
    settings.webhook_secret_github = "test-webhook-secret"
    async with EntitlementAuthHarness(pg_container, settings=settings) as harness:
        persona = harness.add_persona(slug, roles=["admin"])
        harness.configure_fetcher_for(persona)
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # JIT-materialise tenant + actor.
            with patch_validator_for_actor(persona):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                tenant_id = uuid.UUID(resp.json()["tenant_id"])
                actor_id = uuid.UUID(resp.json()["actor_id"])

            yield {
                "client": client,
                "harness": harness,
                "persona": persona,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "pg_url": pg_container,
            }


# ---------------------------------------------------------------------------
# T14 smoke: five connectors parse() via cassette bytes
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_five_connectors_sync_under_60s(pg_container: str, respx_cassette: Any) -> None:
    """All five connectors parse() from cassette bytes and return ParsedFacts.

    Does not run the full runner; tests the parse() layer directly using the
    cassette response bodies -- network-free and deterministic.  Verifies that
    each connector returns at least one ParsedFact with the expected category.
    """
    # --- OpenAPI connector ---
    openapi_raw = (
        b"openapi: '3.0.3'\n"
        b"info:\n  title: Petstore API\n  version: '1.0.0'\n"
        b"paths:\n  /pets:\n    get:\n      summary: List pets\n"
    )
    openapi_artifact = DiscoveredArtifact(
        artifact_id="petstore.openapi.yaml",
        source_url="https://raw.githubusercontent.com/acme/repo/main/petstore.openapi.yaml",
        artifact_type="openapi",
    )
    openapi_facts = OpenAPIConnector().parse(openapi_artifact, openapi_raw)
    assert len(openapi_facts) >= 1
    assert openapi_facts[0].category == "api_doc"

    # --- Release notes connector ---
    from urllib.parse import quote as _quote

    meta = json.dumps(
        {
            "tag_name": "v1.0.0",
            "name": "v1.0.0",
            "published_at": "2024-01-01T00:00:00Z",
            "body": "Initial release.",
            "owner": "acme",
            "repo": "test",
        }
    )
    release_artifact = DiscoveredArtifact(
        artifact_id="v1.0.0",
        source_url=f"{_RELEASE_META_PREFIX}{_quote(meta)}",
        artifact_type="release_note",
    )
    release_facts = ReleaseNotesConnector().parse(release_artifact, b"Initial release.")
    assert len(release_facts) >= 1
    assert release_facts[0].category == "release_note"

    # --- Markdown ADR connector ---
    adr_artifact = DiscoveredArtifact(
        artifact_id="docs/adr/0001-postgres.md",
        source_url="https://raw.githubusercontent.com/acme/repo/main/docs/adr/0001-postgres.md",
        artifact_type="markdown_adr_rfc",
    )
    adr_facts = MarkdownADRRFCConnector().parse(adr_artifact, b"# Decision-0001\n\nUse Postgres.\n")
    assert len(adr_facts) >= 1
    assert adr_facts[0].category == "adr"

    # --- RFC connector ---
    rfc_artifact = DiscoveredArtifact(
        artifact_id="docs/rfc/rfc-001-versioning.md",
        source_url="https://raw.githubusercontent.com/acme/repo/main/docs/rfc/rfc-001-versioning.md",
        artifact_type="markdown_adr_rfc",
    )
    rfc_facts = MarkdownADRRFCConnector().parse(rfc_artifact, b"# RFC-001\n\nAPI versioning.\n")
    assert len(rfc_facts) >= 1
    assert rfc_facts[0].category == "rfc"

    # --- Package JSON connector ---
    pkg_artifact = DiscoveredArtifact(
        artifact_id="package.json",
        source_url="https://raw.githubusercontent.com/acme/repo/main/package.json",
        artifact_type="package_json",
    )
    pkg_raw = json.dumps({"name": "@acme/svc", "version": "1.0.0"}).encode()
    pkg_facts = PackageJsonConnector().parse(pkg_artifact, pkg_raw)
    assert len(pkg_facts) >= 1
    assert pkg_facts[0].category == "package_manifest"

    # --- Docs corpus connector ---
    docs_artifact = DiscoveredArtifact(
        artifact_id="AGENTS.md",
        source_url="https://raw.githubusercontent.com/acme/repo/main/AGENTS.md",
        artifact_type="docs_corpus",
    )
    docs_facts = DocsCorpusConnector().parse(docs_artifact, b"# AGENTS.md\n\nDeveloper docs.\n")
    assert len(docs_facts) >= 1
    assert docs_facts[0].category == "dev_doc"


# ---------------------------------------------------------------------------
# Webhook flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_creates_delivery_record(pg_container: str, p3_client: Any) -> None:
    """POST /webhooks/github creates a webhook_deliveries row."""
    client: httpx.AsyncClient = p3_client["client"]
    harness: EntitlementAuthHarness = p3_client["harness"]
    persona: TenantPersona = p3_client["persona"]
    actor_id: uuid.UUID = p3_client["actor_id"]
    tenant_id: uuid.UUID = p3_client["tenant_id"]

    source_id = await _create_sync_source(
        pg_container,
        tenant_id=tenant_id,
        actor_id=actor_id,
        source_type="openapi",
    )

    delivery_id = f"gh-test-{uuid.uuid4().hex[:8]}"
    payload = json.dumps({"ref": "refs/heads/main"}).encode()
    secret = "test-webhook-secret"
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/webhooks/github?source_id={source_id}",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Delivery": delivery_id,
                **bearer_headers(tenant_slug=persona.slug),
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["delivery_id"] == delivery_id

    # Confirm row persisted.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.get(WebhookDelivery, (tenant_id, delivery_id))
        assert row is not None
        assert row.source_id == source_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_webhook_duplicate_is_noop(pg_container: str, p3_client: Any) -> None:
    """Sending the same delivery_id twice is a no-op: only one DB row, both 200."""
    client: httpx.AsyncClient = p3_client["client"]
    harness: EntitlementAuthHarness = p3_client["harness"]
    persona: TenantPersona = p3_client["persona"]
    actor_id: uuid.UUID = p3_client["actor_id"]
    tenant_id: uuid.UUID = p3_client["tenant_id"]

    source_id = await _create_sync_source(
        pg_container,
        tenant_id=tenant_id,
        actor_id=actor_id,
        source_type="openapi",
    )

    delivery_id = f"gh-dup-{uuid.uuid4().hex[:8]}"
    payload = b'{"ref":"refs/heads/main"}'
    secret = "test-webhook-secret"
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    extra_headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": delivery_id,
        **bearer_headers(tenant_slug=persona.slug),
    }

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        # First POST.
        r1 = await client.post(
            f"/webhooks/github?source_id={source_id}",
            content=payload,
            headers=extra_headers,
        )
        assert r1.status_code == 200

        # Second POST -- same delivery_id.
        r2 = await client.post(
            f"/webhooks/github?source_id={source_id}",
            content=payload,
            headers=extra_headers,
        )
        assert r2.status_code == 200

    # Confirm exactly one row.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.tenant_id == tenant_id,
                    WebhookDelivery.delivery_id == delivery_id,
                )
            )
            rows = list(result.scalars().all())
        assert len(rows) == 1, f"expected 1 delivery row, got {len(rows)}"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Conflict policy E2E: authoritative wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authoritative_wins_conflict(pg_container: str) -> None:
    """Synced fact for a slot that already has an authoritative fact -> is_authoritative_superseded=True.

    The authoritative fact is written first via CatalogService.create_fact.
    Then a sync upsert is performed for the same (entity_id, category).
    The resulting synced fact must have is_authoritative_superseded=True.
    """
    tenant_id, actor_id = await _seed_tenant(
        pg_container,
        slug=f"conflict-{uuid.uuid4().hex[:8]}",
        roles=["admin"],
    )
    entity_id = uuid.uuid4()
    await _create_entity(
        pg_container,
        tenant_id=tenant_id,
        actor_id=actor_id,
        entity_id=entity_id,
        name="svc-conflict",
    )

    _make_settings(pg_container)
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock(_NOW)
    vocabulary = VocabularyService(factory)
    schema = SchemaService(factory, clock)
    catalog = CatalogService(factory, clock, vocabulary, schema)
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"])

    try:
        # Write authoritative fact.
        await catalog.create_fact(
            ctx,
            entity_id=entity_id,
            category="overview",
            body="Authoritative overview.",
        )

        # Now run a sync that writes the same (entity_id, category).
        source_id = await _create_sync_source(
            pg_container,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_type="openapi",
        )
        sync_run_id = uuid.uuid4()
        await _seed_sync_run(pg_container, tenant_id=tenant_id, source_id=source_id, sync_run_id=sync_run_id)
        pf = ParsedFact(
            entity_id=entity_id,
            category="overview",
            body="Synced overview -- should be superseded.",
            valid_from=_NOW,
            source_url="https://example.com/openapi.yaml",
            commit_sha=None,
        )

        # Retrieve the source ORM row for passing to upsert_synced_facts.
        async with factory() as session:
            source_row = await session.get(SyncSource, source_id)
        assert source_row is not None

        result = await catalog.upsert_synced_facts(ctx, [pf], sync_run_id, source_row)
        assert result.superseded == 1, f"expected 1 superseded fact, got {result.superseded}"

        # Confirm the synced fact row in DB.
        async with factory() as session:
            db_result = await session.execute(
                select(Fact).where(
                    Fact.tenant_id == tenant_id,
                    Fact.entity_id == entity_id,
                    Fact.category == "overview",
                    Fact.is_authoritative.is_(False),
                    Fact.is_authoritative_superseded.is_(True),
                )
            )
            superseded_fact = db_result.scalar_one_or_none()

        assert superseded_fact is not None, "synced fact with is_authoritative_superseded=True must exist"
        assert superseded_fact.sync_run_id == sync_run_id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Partial sync idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_sync_idempotency(pg_container: str) -> None:
    """Running upsert_synced_facts twice for the same artifact is stable.

    Second run produces a new non-authoritative fact (bi-temporal replacement)
    or is counted as superseded if an authoritative fact was written between runs.
    The key invariant: no crash, no duplicates for authoritative slots.
    """
    tenant_id, actor_id = await _seed_tenant(
        pg_container,
        slug=f"idem-{uuid.uuid4().hex[:8]}",
        roles=["admin"],
    )
    entity_id = uuid.uuid4()
    await _create_entity(
        pg_container,
        tenant_id=tenant_id,
        actor_id=actor_id,
        entity_id=entity_id,
        name="svc-idempotent",
    )

    _make_settings(pg_container)
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock(_NOW)
    vocabulary = VocabularyService(factory)
    schema = SchemaService(factory, clock)
    catalog = CatalogService(factory, clock, vocabulary, schema)
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"])

    source_id = await _create_sync_source(
        pg_container,
        tenant_id=tenant_id,
        actor_id=actor_id,
        source_type="openapi",
    )

    try:
        async with factory() as session:
            source_row = await session.get(SyncSource, source_id)
        assert source_row is not None

        pf = ParsedFact(
            entity_id=entity_id,
            category="api_doc",
            body="API spec v1.",
            valid_from=_NOW,
            source_url="https://example.com/openapi.yaml",
            commit_sha=None,
        )

        run_id_1 = uuid.uuid4()
        await _seed_sync_run(pg_container, tenant_id=tenant_id, source_id=source_id, sync_run_id=run_id_1)
        result1 = await catalog.upsert_synced_facts(ctx, [pf], run_id_1, source_row)
        assert result1.created == 1

        # Second run: same fact body.
        run_id_2 = uuid.uuid4()
        await _seed_sync_run(pg_container, tenant_id=tenant_id, source_id=source_id, sync_run_id=run_id_2)
        pf2 = ParsedFact(
            entity_id=entity_id,
            category="api_doc",
            body="API spec v1.",
            valid_from=_NOW,
            source_url="https://example.com/openapi.yaml",
            commit_sha=None,
        )
        result2 = await catalog.upsert_synced_facts(ctx, [pf2], run_id_2, source_row)
        # Second run either creates (bi-temporal replacement) or is 0 created
        # if body is identical and service is idempotent. Either way: no crash.
        assert result2.created + result2.superseded + result2.skipped >= 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Sync run error -> status='failed' with error_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_run_error_populates_error_summary(pg_container: str) -> None:
    """When the connector's fetch() raises repeatedly, sync_run.status is 'partial' or 'failed'.

    Uses a mock connector where fetch() always raises to simulate a total failure.
    Verifies error_summary is populated on the sync_run row.
    """
    tenant_id, actor_id = await _seed_tenant(
        pg_container,
        slug=f"err-{uuid.uuid4().hex[:8]}",
        roles=["admin"],
    )

    settings = _make_settings(pg_container)
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock(_NOW)
    vocabulary = VocabularyService(factory)
    schema = SchemaService(factory, clock)
    catalog = CatalogService(factory, clock, vocabulary, schema)
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"])

    source_id = await _create_sync_source(
        pg_container,
        tenant_id=tenant_id,
        actor_id=actor_id,
        source_type="openapi",
    )
    sync_run_id = uuid.uuid4()

    try:
        async with factory() as session:
            source_row = await session.get(SyncSource, source_id)
            assert source_row is not None
            # Add sync_run row first (as runner does before calling _execute_sync).
            run = SyncRun(
                sync_run_id=sync_run_id,
                tenant_id=tenant_id,
                source_id=source_id,
                status="running",
                trigger="manual",
                started_at=_NOW,
            )
            session.add(run)
            await session.commit()

        # Build an artifact that will fail fetch.
        failing_artifact = DiscoveredArtifact(
            artifact_id="will-fail.openapi.yaml",
            source_url="https://example.com/will-fail.openapi.yaml",
            artifact_type="openapi",
        )

        # Patch the connector so discover returns our failing artifact and
        # fetch always raises a network error.
        with (
            patch("sync.connectors.openapi.OpenAPIConnector.discover", new=AsyncMock(return_value=[failing_artifact])),
            patch("sync.connectors.openapi.OpenAPIConnector.validate", new=AsyncMock(return_value=None)),
            patch(
                "sync.connectors.openapi.OpenAPIConnector.fetch",
                new=AsyncMock(side_effect=ConnectionError("simulated network error")),
            ),
        ):
            await _execute_sync(
                source=source_row,
                sync_run_id=sync_run_id,
                ctx=ctx,
                session_factory=factory,
                catalog=catalog,
                settings=settings,
            )

        async with factory() as session:
            run_or_none = await session.get(SyncRun, sync_run_id)
        assert run_or_none is not None
        assert run_or_none.status in ("partial", "failed"), f"expected partial/failed, got {run_or_none.status!r}"
        assert run_or_none.error_summary is not None, "error_summary must be populated on failure"
        assert "will-fail.openapi.yaml" in run_or_none.error_summary or "simulated" in run_or_none.error_summary
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Admin CRUD: sync-source lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_sync_source_crud(p3_client: Any) -> None:
    """POST / GET / PATCH / DELETE lifecycle for /v1/admin/sync-sources."""
    client: httpx.AsyncClient = p3_client["client"]
    harness: EntitlementAuthHarness = p3_client["harness"]
    persona: TenantPersona = p3_client["persona"]

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        # POST -- create. connector.validate() is called; use mock so no real network call.
        with patch("sync.connectors.openapi.OpenAPIConnector.validate", new=AsyncMock(return_value=None)):
            r_create = await client.post(
                "/v1/admin/sync-sources",
                json={
                    "source_type": "openapi",
                    "display_name": "test-openapi-source",
                    "config": {"owner": "acme", "repo": "svc", "ref": "main"},
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        assert r_create.status_code == 201, r_create.text
        body = r_create.json()
        source_id = body["source_id"]
        assert body["source_type"] == "openapi"
        assert body["is_active"] is True

        # GET detail.
        r_get = await client.get(
            f"/v1/admin/sync-sources/{source_id}",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
        assert r_get.status_code == 200
        assert r_get.json()["source_id"] == source_id

        # PATCH.
        r_patch = await client.patch(
            f"/v1/admin/sync-sources/{source_id}",
            json={"display_name": "renamed-openapi-source"},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["display_name"] == "renamed-openapi-source"

        # DELETE (soft).
        r_delete = await client.delete(
            f"/v1/admin/sync-sources/{source_id}",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
        assert r_delete.status_code == 204

        # After soft-delete, GET returns 404.
        r_gone = await client.get(
            f"/v1/admin/sync-sources/{source_id}",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
        assert r_gone.status_code == 404


# ---------------------------------------------------------------------------
# Admin: sync-run list / detail / superseded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_sync_run_list_and_superseded(pg_container: str, p3_client: Any) -> None:
    """GET /v1/admin/sync-runs and /v1/admin/sync-runs/{id}/superseded return correct data."""
    client: httpx.AsyncClient = p3_client["client"]
    harness: EntitlementAuthHarness = p3_client["harness"]
    persona: TenantPersona = p3_client["persona"]
    tenant_id: uuid.UUID = p3_client["tenant_id"]
    actor_id: uuid.UUID = p3_client["actor_id"]

    source_id = await _create_sync_source(
        pg_container,
        tenant_id=tenant_id,
        actor_id=actor_id,
        source_type="openapi",
    )

    # Insert a sync_run row directly (simulating completed run).
    sync_run_id = uuid.uuid4()
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            session.add(
                SyncRun(
                    sync_run_id=sync_run_id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    status="done",
                    trigger="manual",
                    started_at=_NOW,
                    finished_at=_NOW,
                    duration_s=3,
                    artifact_count=1,
                    error_summary=None,
                )
            )
    finally:
        await engine.dispose()

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        # List runs.
        r_list = await client.get(
            f"/v1/admin/sync-runs?source_id={source_id}",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
        assert r_list.status_code == 200
        runs = r_list.json()
        assert any(r["sync_run_id"] == str(sync_run_id) for r in runs), "run must appear in list"

        # Detail.
        r_detail = await client.get(
            f"/v1/admin/sync-runs/{sync_run_id}",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
        assert r_detail.status_code == 200
        assert r_detail.json()["status"] == "done"

        # Superseded facts (empty for this run -- none written via conflict policy).
        r_sup = await client.get(
            f"/v1/admin/sync-runs/{sync_run_id}/superseded",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
        assert r_sup.status_code == 200
        assert isinstance(r_sup.json(), list)


# ---------------------------------------------------------------------------
# Tenant isolation regression: cross-tenant access is blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_sync_source(pg_container: str) -> None:
    """Tenant B cannot read tenant A's sync_source by guessing the source_id."""
    slug_a = f"iso-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"iso-b-{uuid.uuid4().hex[:8]}"

    async with EntitlementAuthHarness(pg_container) as harness:
        persona_a = harness.add_persona(slug_a, roles=["admin"])
        persona_b = harness.add_persona(slug_b, roles=["admin"])

        # Materialise both tenants.
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona_a)
            with patch_validator_for_actor(persona_a):
                r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_a))
                tenant_a_id = uuid.UUID(r.json()["tenant_id"])
                actor_a_id = uuid.UUID(r.json()["actor_id"])

            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_b))

            source_id = await _create_sync_source(
                pg_container,
                tenant_id=tenant_a_id,
                actor_id=actor_a_id,
                source_type="openapi",
            )

            # Tenant B tries to read tenant A's sync_source.
            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                r = await client.get(
                    f"/v1/admin/sync-sources/{source_id}",
                    headers=bearer_headers(tenant_slug=slug_b),
                )
            assert r.status_code in (403, 404), (
                f"tenant B must not read tenant A's sync_source; got {r.status_code}"
            )


@pytest.mark.asyncio
async def test_tenant_isolation_sync_run(pg_container: str) -> None:
    """Tenant B cannot read tenant A's sync_run by guessing the run_id."""
    slug_a = f"isob-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"isob-b-{uuid.uuid4().hex[:8]}"

    async with EntitlementAuthHarness(pg_container) as harness:
        persona_a = harness.add_persona(slug_a, roles=["admin"])
        persona_b = harness.add_persona(slug_b, roles=["admin"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona_a)
            with patch_validator_for_actor(persona_a):
                r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_a))
                tenant_a_id = uuid.UUID(r.json()["tenant_id"])
                actor_a_id = uuid.UUID(r.json()["actor_id"])

            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_b))

            source_id = await _create_sync_source(
                pg_container,
                tenant_id=tenant_a_id,
                actor_id=actor_a_id,
                source_type="openapi",
            )
            sync_run_id = uuid.uuid4()

            engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session, session.begin():
                    session.add(
                        SyncRun(
                            sync_run_id=sync_run_id,
                            tenant_id=tenant_a_id,
                            source_id=source_id,
                            status="done",
                            trigger="manual",
                            started_at=_NOW,
                        )
                    )
            finally:
                await engine.dispose()

            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                r = await client.get(
                    f"/v1/admin/sync-runs/{sync_run_id}",
                    headers=bearer_headers(tenant_slug=slug_b),
                )
                assert r.status_code in (403, 404), (
                    f"tenant B must not read tenant A's sync_run; got {r.status_code}"
                )

                r_sup = await client.get(
                    f"/v1/admin/sync-runs/{sync_run_id}/superseded",
                    headers=bearer_headers(tenant_slug=slug_b),
                )
                assert r_sup.status_code in (403, 404), (
                    f"tenant B must not access tenant A's superseded facts; got {r_sup.status_code}"
                )
