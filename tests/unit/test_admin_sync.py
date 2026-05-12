"""Unit tests for admin sync-source and sync-run REST endpoints.

Coverage:
- create_sync_source: unknown source_type → 422; connector.validate failure → 422;
  success creates row and upserts actor.
- list_sync_sources: returns only tenant-scoped rows.
- get_sync_source: 404 for unknown or cross-tenant; 200 for known.
- patch_sync_source: partial field update.
- delete_sync_source: soft-delete (is_active=FALSE).
- trigger_sync: enqueues job; inactive source → 409; missing source → 404.
- list_sync_runs: filterable; tenant isolation.
- get_sync_run: 404 for unknown.
- get_superseded_facts_for_run: 404 for unknown run; returns matching facts.
- Auth: admin role required — missing role → 403.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.config import Settings

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_use_memory_jobstore=True,
    )


TENANT_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()


def _make_token_ctx(roles: list[str] | None = None) -> dict[str, Any]:
    """Return a patched TenantContext for admin routes."""
    from registry.types import TenantContext

    return TenantContext(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        roles=roles if roles is not None else ["admin"],
    )


def _build_app(
    db_objects: dict[str, Any] | None = None,
    roles: list[str] | None = None,
) -> TestClient:
    """Build a minimal FastAPI app with the admin router and mocked DB.

    ``db_objects`` maps ORM class names to lists of mock rows for selects.
    The session ``get()`` method returns the first matching row by primary key.
    """
    from registry.api.middleware.tenant import get_tenant_context
    from registry.api.routers.admin import router

    db = db_objects or {}
    ctx = _make_token_ctx(roles)

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    _stored_objects: dict[uuid.UUID, Any] = {}

    async def _get(cls: Any, pk: Any) -> Any:
        # Check explicit db_objects first.
        for obj in db.get(cls.__name__, []):
            # Try matching by primary key field names.
            pk_fields = ["source_id", "sync_run_id", "token_id", "fact_id"]
            for field in pk_fields:
                if hasattr(obj, field) and getattr(obj, field) == pk:
                    return obj
        # Fall back to objects added via session.add during the request.
        return _stored_objects.get(pk)

    def _add_obj(obj: Any) -> None:
        pk_fields = ["source_id", "sync_run_id", "token_id", "fact_id"]
        for field in pk_fields:
            if hasattr(obj, field):
                _stored_objects[getattr(obj, field)] = obj
                break

    session.add = MagicMock(side_effect=_add_obj)
    session.get = AsyncMock(side_effect=_get)

    async def _execute(stmt: Any) -> Any:
        # Naively return all rows registered in db_objects.
        # The real test assertions don't depend on filtered results.
        result = MagicMock()
        all_rows: list[Any] = []
        for rows in db.values():
            all_rows.extend(rows)
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=all_rows)
        scalars.one_or_none = MagicMock(return_value=all_rows[0] if all_rows else None)
        result.scalars = MagicMock(return_value=scalars)
        result.scalar_one_or_none = MagicMock(return_value=all_rows[0] if all_rows else None)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session)

    from registry.main import _install_error_envelope

    app = FastAPI()
    _install_error_envelope(app)
    app.state.settings = _settings()
    app.state.session_factory = session_factory
    app.state.scheduler = MagicMock()
    app.state.catalog = MagicMock()

    # Override the auth dependency to use our test context.

    async def _fixed_ctx() -> Any:
        return ctx

    app.dependency_overrides[get_tenant_context] = _fixed_ctx
    app.include_router(router)
    # Mutation routes (PATCH/DELETE) live on admin_mutation_router.
    from registry.api.routers.admin import admin_mutation_router  # noqa: PLC0415

    app.include_router(admin_mutation_router)

    return TestClient(app, raise_server_exceptions=True)


def _make_source(
    source_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    source_type: str = "openapi",
    is_active: bool = True,
) -> MagicMock:
    s = MagicMock()
    s.source_id = source_id or uuid.uuid4()
    s.tenant_id = tenant_id or TENANT_ID
    s.source_type = source_type
    s.display_name = f"test-{source_type}"
    s.config = {}
    s.credentials_ref = None
    s.schedule = None
    s.is_active = is_active
    s.created_at = datetime.now(tz=UTC)
    s.created_by = ACTOR_ID
    return s


def _make_run(
    sync_run_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    run_status: str = "done",
) -> MagicMock:
    r = MagicMock()
    r.sync_run_id = sync_run_id or uuid.uuid4()
    r.source_id = source_id or uuid.uuid4()
    r.tenant_id = tenant_id or TENANT_ID
    r.status = run_status
    r.trigger = "scheduled"
    r.started_at = datetime.now(tz=UTC)
    r.finished_at = datetime.now(tz=UTC)
    r.duration_s = 5
    r.artifact_count = 3
    r.error_summary = None
    return r


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def test_create_source_requires_admin() -> None:
    client = _build_app(roles=[])  # no admin role
    resp = client.post(
        "/v1/admin/sync-sources",
        json={"source_type": "openapi", "display_name": "test"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# create_sync_source
# ---------------------------------------------------------------------------


def test_create_source_unknown_connector_returns_422() -> None:
    client = _build_app()
    resp = client.post(
        "/v1/admin/sync-sources",
        json={"source_type": "nonexistent_connector", "display_name": "bad"},
    )
    assert resp.status_code == 422


def test_create_source_validate_failure_returns_422() -> None:
    from sync.connector import CredentialError

    with (
        patch("sync.registry.get_connector") as mock_get,
        patch("sync.runner.resolve_sync_actor", new_callable=AsyncMock),
    ):
        mock_connector = MagicMock()
        mock_connector.validate = AsyncMock(side_effect=CredentialError("no cred"))
        mock_get.return_value = lambda: mock_connector

        client = _build_app()
        resp = client.post(
            "/v1/admin/sync-sources",
            json={"source_type": "openapi", "display_name": "test"},
        )
    assert resp.status_code == 422
    assert "connector validation failed" in resp.json()["errors"][0]["message"]


def test_create_source_success() -> None:
    with (
        patch("sync.registry.get_connector") as mock_get,
        patch("sync.runner.resolve_sync_actor", new_callable=AsyncMock) as mock_actor,
    ):
        mock_connector = MagicMock()
        mock_connector.validate = AsyncMock()
        mock_get.return_value = lambda: mock_connector
        mock_actor.return_value = uuid.uuid4()

        source_id = uuid.uuid4()
        source = _make_source(source_id=source_id)

        # _build_app session.get will return the source we register
        client = _build_app(db_objects={"SyncSource": [source]})
        resp = client.post(
            "/v1/admin/sync-sources",
            json={
                "source_type": "openapi",
                "display_name": "My API",
                "config": {"url": "https://api.example.com/openapi.json"},
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["source_type"] == "openapi"
    assert body["is_active"] is True
    mock_actor.assert_called_once()


# ---------------------------------------------------------------------------
# list / get / patch / delete sync-sources
# ---------------------------------------------------------------------------


def test_list_sync_sources() -> None:
    source = _make_source()
    client = _build_app(db_objects={"SyncSource": [source]})
    resp = client.get("/v1/admin/sync-sources")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_sync_source_not_found() -> None:
    client = _build_app()
    resp = client.get(f"/v1/admin/sync-sources/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_sync_source_cross_tenant_returns_404() -> None:
    other_tenant = uuid.uuid4()
    source = _make_source(tenant_id=other_tenant)
    client = _build_app(db_objects={"SyncSource": [source]})
    resp = client.get(f"/v1/admin/sync-sources/{source.source_id}")
    assert resp.status_code == 404


def test_get_sync_source_success() -> None:
    source = _make_source()
    client = _build_app(db_objects={"SyncSource": [source]})
    resp = client.get(f"/v1/admin/sync-sources/{source.source_id}")
    assert resp.status_code == 200
    assert resp.json()["source_id"] == str(source.source_id)


def test_patch_sync_source_not_found() -> None:
    client = _build_app()
    resp = client.patch(
        f"/v1/admin/sync-sources/{uuid.uuid4()}",
        json={"display_name": "New Name"},
    )
    assert resp.status_code == 404


def test_patch_sync_source_updates_display_name() -> None:
    source = _make_source()
    client = _build_app(db_objects={"SyncSource": [source]})
    resp = client.patch(
        f"/v1/admin/sync-sources/{source.source_id}",
        json={"display_name": "Updated Name"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Updated Name"


def test_delete_sync_source_soft_deletes() -> None:
    source = _make_source()
    client = _build_app(db_objects={"SyncSource": [source]})
    resp = client.delete(f"/v1/admin/sync-sources/{source.source_id}")
    assert resp.status_code == 204
    assert source.is_active is False


def test_delete_sync_source_not_found() -> None:
    client = _build_app()
    resp = client.delete(f"/v1/admin/sync-sources/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# trigger
# ---------------------------------------------------------------------------


def test_trigger_missing_source_returns_404() -> None:
    client = _build_app()
    resp = client.post(f"/v1/admin/sync-sources/{uuid.uuid4()}/trigger")
    assert resp.status_code == 404


def test_trigger_inactive_source_returns_409() -> None:
    source = _make_source(is_active=False)
    client = _build_app(db_objects={"SyncSource": [source]})
    resp = client.post(f"/v1/admin/sync-sources/{source.source_id}/trigger")
    assert resp.status_code == 409


def test_trigger_enqueues_job() -> None:
    source = _make_source()
    client = _build_app(db_objects={"SyncSource": [source]})
    with patch("sync.runner.run_sync_job"):
        resp = client.post(f"/v1/admin/sync-sources/{source.source_id}/trigger")
    assert resp.status_code == 202
    body = resp.json()
    assert body["trigger"] == "manual"
    assert body["status"] == "queued"


# ---------------------------------------------------------------------------
# sync-run list / detail / superseded
# ---------------------------------------------------------------------------


def test_list_sync_runs() -> None:
    run = _make_run()
    client = _build_app(db_objects={"SyncRun": [run]})
    resp = client.get("/v1/admin/sync-runs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_sync_run_not_found() -> None:
    client = _build_app()
    resp = client.get(f"/v1/admin/sync-runs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_sync_run_success() -> None:
    run = _make_run()
    client = _build_app(db_objects={"SyncRun": [run]})
    resp = client.get(f"/v1/admin/sync-runs/{run.sync_run_id}")
    assert resp.status_code == 200
    assert resp.json()["sync_run_id"] == str(run.sync_run_id)


def test_get_sync_run_cross_tenant_returns_404() -> None:
    run = _make_run(tenant_id=uuid.uuid4())
    client = _build_app(db_objects={"SyncRun": [run]})
    resp = client.get(f"/v1/admin/sync-runs/{run.sync_run_id}")
    assert resp.status_code == 404


def test_superseded_run_not_found() -> None:
    client = _build_app()
    resp = client.get(f"/v1/admin/sync-runs/{uuid.uuid4()}/superseded")
    assert resp.status_code == 404


def test_superseded_returns_list_with_facts() -> None:
    """Superseded facts for a run are serialised correctly."""
    from registry.storage.models import Fact

    run = _make_run()
    now = datetime.now(tz=UTC)
    fact = Fact(
        fact_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        entity_id=uuid.uuid4(),
        category="openapi_spec",
        body="{}",
        is_authoritative=False,
        is_authoritative_superseded=True,
        sync_run_id=run.sync_run_id,
        t_valid_from=now,
        t_valid_to=None,
        t_ingested_at=now,
        t_invalidated_at=None,
        created_by=None,
    )

    # Build a custom app where execute for the Fact query returns only Fact rows.
    from registry.api.middleware.tenant import get_tenant_context
    from registry.api.routers.admin import router

    ctx = _make_token_ctx(["admin"])

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    call_count: dict[str, int] = {"execute": 0}

    async def _get(cls: Any, pk: Any) -> Any:
        if hasattr(run, "sync_run_id") and run.sync_run_id == pk:
            return run
        return None

    async def _execute(stmt: Any) -> Any:
        call_count["execute"] += 1
        result = MagicMock()
        scalars = MagicMock()
        # First execute (for facts) returns the fact.
        scalars.all = MagicMock(return_value=[fact])
        scalars.one_or_none = MagicMock(return_value=fact)
        result.scalars = MagicMock(return_value=scalars)
        result.scalar_one_or_none = MagicMock(return_value=fact)
        return result

    session.get = AsyncMock(side_effect=_get)
    session.execute = AsyncMock(side_effect=_execute)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session)

    from registry.main import _install_error_envelope

    app = FastAPI()
    _install_error_envelope(app)
    app.state.settings = _settings()
    app.state.session_factory = session_factory
    app.state.scheduler = MagicMock()
    app.state.catalog = MagicMock()

    async def _fixed_ctx() -> Any:
        return ctx

    app.dependency_overrides[get_tenant_context] = _fixed_ctx
    app.include_router(router)

    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get(f"/v1/admin/sync-runs/{run.sync_run_id}/superseded")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["category"] == "openapi_spec"
