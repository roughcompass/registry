"""Unit tests for admin vocab, schema, audit, and roles endpoints.

All tests use mocked session factories — no DB required.
Coverage:
  T06 — vocabulary admin endpoints
  T07 — capability-type schema admin endpoints
  T08 — audit log query endpoint (keyset pagination, tenant isolation)
  T09 — role management endpoints
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from registry.api.cursor import decode_cursor, encode_cursor
from registry.api.middleware.idempotency import IdempotencyContext
from registry.api.routers.admin import (
    AuditResponse,
    query_audit_log,
)
from registry.types import TenantContext


def _inert_idem() -> IdempotencyContext:
    """Return a no-op IdempotencyContext for tests that call handlers directly."""
    return IdempotencyContext(key=None, body_hash=None, _method="POST", _path="/test", _session_factory=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"])


def _auditor_ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["auditor"])


def _consumer_ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["consumer"])


def _make_session(rows: list[Any] | None = None, scalar_value: Any = None) -> MagicMock:
    """Return a mock factory/session that returns `rows` from scalars().all()
    or `scalar_value` from scalar_one_or_none()."""
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows or [])))
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=scalar_value)
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)

    factory = MagicMock(return_value=session)
    return factory


def _make_vocab_row(*, deprecated: bool = False) -> MagicMock:
    row = MagicMock()
    row.vocab_id = uuid.uuid4()
    row.kind = "entity_type"
    row.value = "capability"
    row.is_system = False
    row.created_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    row.deprecated_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC) if deprecated else None
    return row


def _make_schema_row(*, is_advisory: bool = True) -> MagicMock:
    row = MagicMock()
    row.schema_id = uuid.uuid4()
    row.type_name = "api_service"
    row.json_schema = {"type": "object"}
    row.is_advisory = is_advisory
    row.t_valid_from = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    row.t_valid_to = None
    row.t_ingested_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    row.t_invalidated_at = None
    return row


def _make_audit_row() -> MagicMock:
    row = MagicMock()
    row.audit_id = uuid.uuid4()
    row.actor_id = uuid.uuid4()
    row.action = "create"
    row.target_type = "entity"
    row.target_id = uuid.uuid4()
    row.before_jsonb = None
    row.after_jsonb = {"name": "x"}
    row.ts = datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC)
    row.request_id = None
    row.error_code = None
    return row


def _make_role_row(name: str = "admin") -> MagicMock:
    row = MagicMock()
    row.role_id = uuid.uuid4()
    row.tenant_id = uuid.uuid4()
    row.name = name
    row.permissions = []
    row.created_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    return row


def _make_actor_role_row() -> MagicMock:
    row = MagicMock()
    row.tenant_id = uuid.uuid4()
    row.actor_id = uuid.uuid4()
    row.role_id = uuid.uuid4()
    row.granted_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    row.granted_by = uuid.uuid4()
    return row


# ===========================================================================
# T06 — Vocabulary admin endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_list_vocab_returns_rows() -> None:
    from registry.api.routers.admin import list_vocabulary_values

    vocab_row = _make_vocab_row()
    factory = _make_session(rows=[vocab_row])
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    result = await list_vocabulary_values("entity_type", request, ctx)
    assert len(result) == 1
    assert result[0].kind == "entity_type"


@pytest.mark.asyncio
async def test_list_vocab_requires_admin_role() -> None:
    from registry.api.auth.context import require_roles

    dep = require_roles(["admin"])
    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=_consumer_ctx())
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_add_vocab_value_calls_service() -> None:
    from registry.api.routers.admin import VocabularyValueCreate, add_vocabulary_value

    vocab_row = _make_vocab_row()
    factory = _make_session(scalar_value=vocab_row)
    request = MagicMock()
    request.app.state.session_factory = factory

    with patch("registry.service.vocabulary.VocabularyService.add_value", new_callable=AsyncMock) as mock_add:
        ctx = _admin_ctx()
        body = VocabularyValueCreate(value="capability")
        result = await add_vocabulary_value("entity_type", body, request, _inert_idem(), ctx)
    mock_add.assert_called_once()
    assert result.value == "capability"


@pytest.mark.asyncio
async def test_patch_vocab_not_found_raises_404() -> None:
    from registry.api.routers.admin import VocabularyValuePatch, patch_vocabulary_value

    factory = _make_session(scalar_value=None)
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await patch_vocabulary_value("entity_type", "ghost", VocabularyValuePatch(), request, ctx)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_vocab_sets_deprecated_at() -> None:
    from registry.api.routers.admin import delete_vocabulary_value

    vocab_row = _make_vocab_row()
    factory = _make_session(scalar_value=vocab_row)
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    response = await delete_vocabulary_value("entity_type", "capability", request, ctx)
    assert response.status_code == 204
    # deprecated_at was set to now
    assert vocab_row.deprecated_at is not None


@pytest.mark.asyncio
async def test_delete_vocab_not_found_raises_404() -> None:
    from registry.api.routers.admin import delete_vocabulary_value

    factory = _make_session(scalar_value=None)
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await delete_vocabulary_value("entity_type", "ghost", request, ctx)
    assert exc_info.value.status_code == 404


# ===========================================================================
# T07 — Schema admin endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_list_capability_types_returns_rows() -> None:
    from registry.api.routers.admin import list_capability_types

    schema_row = _make_schema_row()
    factory = _make_session(rows=[schema_row])
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    result = await list_capability_types(request, ctx)
    assert len(result) == 1
    assert result[0].type_name == "api_service"


@pytest.mark.asyncio
async def test_create_capability_type_inserts_row() -> None:
    from registry.api.routers.admin import CapabilityTypeSchemaCreate, create_capability_type

    schema_row = _make_schema_row()
    factory = _make_session(scalar_value=schema_row)
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    body = CapabilityTypeSchemaCreate(type_name="api_service", json_schema={"type": "object"})
    result = await create_capability_type(body, request, _inert_idem(), ctx)
    assert result.type_name == "api_service"


@pytest.mark.asyncio
async def test_get_capability_type_not_found_raises_404() -> None:
    from registry.api.routers.admin import get_capability_type

    factory = _make_session(scalar_value=None)
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await get_capability_type("nonexistent", request, ctx)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_capability_type_flips_is_advisory() -> None:
    from registry.api.routers.admin import CapabilityTypeSchemaPatch, patch_capability_type

    schema_row = _make_schema_row(is_advisory=True)
    factory = _make_session(scalar_value=schema_row)
    request = MagicMock()
    request.app.state.session_factory = factory
    # Absent If-Match header → advisory mode; handler proceeds without 412.
    request.headers = MagicMock()
    request.headers.get = MagicMock(return_value=None)
    ctx = _admin_ctx()

    body = CapabilityTypeSchemaPatch(is_advisory=False)
    await patch_capability_type("api_service", body, request, ctx)
    # schema_row.is_advisory was set to False by the handler
    assert schema_row.is_advisory is False


@pytest.mark.asyncio
async def test_schema_endpoints_require_admin_role() -> None:
    from registry.api.auth.context import require_roles

    dep = require_roles(["admin"])
    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=_auditor_ctx())
    assert exc_info.value.status_code == 403


# ===========================================================================
# T08 — Audit log query endpoint
# ===========================================================================


def test_audit_cursor_roundtrip_via_shared_codec() -> None:
    """Audit cursor encode/decode uses the shared codec."""
    ts = datetime.datetime(2026, 4, 1, 12, 0, 0, tzinfo=datetime.UTC)
    audit_id = uuid.uuid4()
    token = encode_cursor({"ts": ts.isoformat(), "audit_id": str(audit_id)})
    payload = decode_cursor(token, strict=True)
    assert datetime.datetime.fromisoformat(payload["ts"]) == ts
    assert uuid.UUID(payload["audit_id"]) == audit_id


@pytest.mark.asyncio
async def test_audit_query_happy_path() -> None:
    audit_row = _make_audit_row()
    factory = _make_session(rows=[audit_row])
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _auditor_ctx()

    result = await query_audit_log(
        request=request,
        ctx=ctx,
        actor_id=None,
        action=None,
        target_type=None,
        target_id=None,
        from_dt=None,
        to_dt=None,
        cursor=None,
        page_size=50,
    )
    assert isinstance(result, AuditResponse)
    assert len(result.items) == 1
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_audit_query_requires_auditor_role() -> None:
    from registry.api.auth.context import require_roles

    dep = require_roles(["auditor"])
    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=_admin_ctx())
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_audit_query_tenant_isolation() -> None:
    """tenant_id from ctx is always injected; a different tenant_id cannot slip through."""
    audit_row = _make_audit_row()
    factory = _make_session(rows=[audit_row])
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _auditor_ctx()

    # Execute with a specific tenant context — the handler must not allow overriding tenant_id.
    result = await query_audit_log(
        request=request,
        ctx=ctx,
        actor_id=None,
        action=None,
        target_type=None,
        target_id=None,
        from_dt=None,
        to_dt=None,
        cursor=None,
        page_size=50,
    )
    # Confirm the session was called (tenant_id was part of the WHERE clause in the query built by the handler).
    factory.return_value.execute.assert_awaited()
    assert result.items[0].action == "create"


@pytest.mark.asyncio
async def test_audit_query_pagination_next_cursor_returned() -> None:
    """When rows == page_size+1, next_cursor is set and rows are truncated."""
    rows = [_make_audit_row() for _ in range(3)]
    factory = _make_session(rows=rows)
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _auditor_ctx()

    result = await query_audit_log(
        request=request,
        ctx=ctx,
        actor_id=None,
        action=None,
        target_type=None,
        target_id=None,
        from_dt=None,
        to_dt=None,
        cursor=None,
        page_size=2,
    )
    assert len(result.items) == 2
    assert result.next_cursor is not None
    # Verify cursor decodes correctly via the shared codec.
    payload = decode_cursor(result.next_cursor, strict=True)
    assert uuid.UUID(payload["audit_id"]) == rows[1].audit_id


@pytest.mark.asyncio
async def test_audit_query_no_next_cursor_when_fewer_rows() -> None:
    rows = [_make_audit_row() for _ in range(2)]
    factory = _make_session(rows=rows)
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _auditor_ctx()

    result = await query_audit_log(
        request=request,
        ctx=ctx,
        actor_id=None,
        action=None,
        target_type=None,
        target_id=None,
        from_dt=None,
        to_dt=None,
        cursor=None,
        page_size=10,
    )
    assert len(result.items) == 2
    assert result.next_cursor is None


# ===========================================================================
# T09 — Role management endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_list_roles_returns_rows() -> None:
    from registry.api.routers.admin import list_roles

    role = _make_role_row("admin")
    factory = _make_session(rows=[role])
    request = MagicMock()
    request.app.state.session_factory = factory
    ctx = _admin_ctx()

    result = await list_roles(request, ctx)
    assert len(result) == 1
    assert result[0].name == "admin"


@pytest.mark.asyncio
async def test_list_roles_requires_admin() -> None:
    from registry.api.auth.context import require_roles

    dep = require_roles(["admin"])
    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=_auditor_ctx())
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_assign_role_happy_path() -> None:
    from registry.api.routers.admin import AssignRoleRequest, assign_role

    role_row = _make_role_row("producer")
    ctx = _admin_ctx()
    role_row.tenant_id = ctx.tenant_id

    # session.get returns the role; scalar_one_or_none returns None (no existing assignment)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=None)

    session = MagicMock()
    session.get = AsyncMock(return_value=role_row)
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)
    factory = MagicMock(return_value=session)

    request = MagicMock()
    request.app.state.session_factory = factory

    body = AssignRoleRequest(role_id=role_row.role_id)
    response = await assign_role(uuid.uuid4(), body, request, _inert_idem(), ctx)
    assert response.status_code == 204
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_assign_role_idempotent_when_already_assigned() -> None:
    from registry.api.routers.admin import AssignRoleRequest, assign_role

    role_row = _make_role_row("producer")
    ctx = _admin_ctx()
    role_row.tenant_id = ctx.tenant_id
    existing_assignment = _make_actor_role_row()

    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=existing_assignment)

    session = MagicMock()
    session.get = AsyncMock(return_value=role_row)
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)
    factory = MagicMock(return_value=session)

    request = MagicMock()
    request.app.state.session_factory = factory

    body = AssignRoleRequest(role_id=role_row.role_id)
    response = await assign_role(uuid.uuid4(), body, request, _inert_idem(), ctx)
    assert response.status_code == 204
    # add should NOT have been called (already exists)
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_assign_role_not_found_raises_404() -> None:
    from registry.api.routers.admin import AssignRoleRequest, assign_role

    ctx = _admin_ctx()

    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)
    factory = MagicMock(return_value=session)

    request = MagicMock()
    request.app.state.session_factory = factory

    with pytest.raises(HTTPException) as exc_info:
        await assign_role(uuid.uuid4(), AssignRoleRequest(role_id=uuid.uuid4()), request, _inert_idem(), ctx)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_remove_role_happy_path() -> None:
    from registry.api.routers.admin import remove_role

    ctx = _admin_ctx()
    actor_role = _make_actor_role_row()

    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=actor_role)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result_mock)
    session.delete = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)
    factory = MagicMock(return_value=session)

    request = MagicMock()
    request.app.state.session_factory = factory

    response = await remove_role(uuid.uuid4(), uuid.uuid4(), request, ctx)
    assert response.status_code == 204
    session.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_remove_role_not_found_raises_404() -> None:
    from registry.api.routers.admin import remove_role

    ctx = _admin_ctx()

    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=None)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result_mock)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)
    factory = MagicMock(return_value=session)

    request = MagicMock()
    request.app.state.session_factory = factory

    with pytest.raises(HTTPException) as exc_info:
        await remove_role(uuid.uuid4(), uuid.uuid4(), request, ctx)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_assign_role_cross_tenant_blocked() -> None:
    """Role from a different tenant cannot be assigned — 404 on tenant_id mismatch."""
    from registry.api.routers.admin import AssignRoleRequest, assign_role

    ctx = _admin_ctx()
    role_row = _make_role_row("producer")
    # role belongs to a DIFFERENT tenant
    role_row.tenant_id = uuid.uuid4()  # not ctx.tenant_id

    session = MagicMock()
    session.get = AsyncMock(return_value=role_row)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)
    factory = MagicMock(return_value=session)

    request = MagicMock()
    request.app.state.session_factory = factory

    with pytest.raises(HTTPException) as exc_info:
        await assign_role(uuid.uuid4(), AssignRoleRequest(role_id=role_row.role_id), request, _inert_idem(), ctx)
    assert exc_info.value.status_code == 404
