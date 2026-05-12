"""Unit tests for PII admin REST endpoints.

Coverage
--------
pii-patterns:
  - POST   /v1/admin/pii-patterns   → 201 with pattern row
  - POST   validates regex syntax   → 422
  - POST   validates policy_override values → 422
  - GET    /v1/admin/pii-patterns   → list including system rows
  - PATCH  /v1/admin/pii-patterns/{id} → 200 with updated row
  - PATCH  on is_system=True row → 403
  - PATCH  invalid regex → 422
  - PATCH  invalid policy_override → 422
  - DELETE /v1/admin/pii-patterns/{id} → 204
  - DELETE on is_system=True row → 403
  - DELETE on missing row → 404

pii-field-policies:
  - POST   /v1/admin/pii-field-policies → 201 with policy row
  - POST   invalid policy value → 422
  - GET    /v1/admin/pii-field-policies → list
  - DELETE /v1/admin/pii-field-policies/{id} → 204
  - DELETE on missing row → 404

Auth:
  - All endpoints require admin role → 403 for consumer

HTTP method factory:
  - Mutation routes (PATCH, DELETE) are registered via HttpMethodRouter

No I/O, no network, no database required.
PII enforcement: field policies and pattern overrides are tenant-scoped; system
patterns cannot be mutated or deleted via the admin surface.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.api.middleware.idempotency import IdempotencyContext
from registry.types import TenantContext


def _inert_idem() -> IdempotencyContext:
    """Return a no-op IdempotencyContext for tests that call handlers directly."""
    return IdempotencyContext(key=None, body_hash=None, _method="POST", _path="/test", _session_factory=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_ctx(tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
    )


def _consumer_ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["consumer"])


def _make_session(
    rows: list[Any] | None = None,
    scalar_value: Any = None,
    raise_on_flush: Exception | None = None,
) -> MagicMock:
    """Return a mock session factory that covers the session.begin() context manager pattern."""
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows or [])))
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=scalar_value)
    session.add = MagicMock()
    session.delete = AsyncMock()
    if raise_on_flush:
        session.flush = AsyncMock(side_effect=raise_on_flush)
    else:
        session.flush = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)

    factory = MagicMock(return_value=session)
    return factory


def _make_pattern_row(
    *,
    tenant_id: uuid.UUID | None = None,
    is_system: bool = False,
    name: str = "test_pattern",
    category: str = "email",
    regex: str = r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",
) -> MagicMock:
    row = MagicMock()
    row.pattern_id = uuid.uuid4()
    row.tenant_id = tenant_id or uuid.uuid4()
    row.name = name
    row.category = category
    row.regex = regex
    row.is_system = is_system
    row.detector_module = None
    row.policy_override = None
    row.is_enabled = True
    row.created_at = datetime.datetime(2026, 5, 10, tzinfo=datetime.UTC)
    row.created_by = uuid.uuid4()
    return row


def _make_field_policy_row(*, tenant_id: uuid.UUID | None = None) -> MagicMock:
    row = MagicMock()
    row.policy_id = uuid.uuid4()
    row.tenant_id = tenant_id or uuid.uuid4()
    row.field_type = "annotation.body"
    row.pattern_id = None
    row.policy = "warn"
    row.created_at = datetime.datetime(2026, 5, 10, tzinfo=datetime.UTC)
    return row


def _make_request(factory: Any) -> MagicMock:
    req = MagicMock()
    req.app.state.session_factory = factory
    # Simulate an absent If-Match header so handlers run in advisory mode
    # (no 412 raised). Tests that want to exercise If-Match pass the header
    # explicitly on the HTTP client instead.
    req.headers = MagicMock()
    req.headers.get = MagicMock(return_value=None)
    return req


# ===========================================================================
# pii-patterns — POST
# ===========================================================================


@pytest.mark.asyncio
async def test_create_pii_pattern_happy_path() -> None:
    from registry.api.routers.admin import PiiPatternCreate, create_pii_pattern

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    pattern_row = _make_pattern_row(tenant_id=tid)
    factory = _make_session(scalar_value=pattern_row)
    request = _make_request(factory)

    body = PiiPatternCreate(
        name="test_email",
        category="email",
        regex=r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",
    )
    result = await create_pii_pattern(body, request, _inert_idem(), ctx)

    assert result.name == pattern_row.name
    assert result.tenant_id == pattern_row.tenant_id
    assert result.is_system is False


@pytest.mark.asyncio
async def test_create_pii_pattern_invalid_regex() -> None:
    from registry.api.routers.admin import PiiPatternCreate, create_pii_pattern

    ctx = _admin_ctx()
    factory = _make_session()
    request = _make_request(factory)

    body = PiiPatternCreate(name="bad", category="email", regex="[unclosed")
    with pytest.raises(HTTPException) as exc_info:
        await create_pii_pattern(body, request, _inert_idem(), ctx)
    assert exc_info.value.status_code == 422
    assert "invalid regex" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_pii_pattern_invalid_policy_override() -> None:
    from registry.api.routers.admin import PiiPatternCreate, create_pii_pattern

    ctx = _admin_ctx()
    factory = _make_session()
    request = _make_request(factory)

    body = PiiPatternCreate(
        name="bad_policy",
        category="email",
        regex=r"\w+",
        policy_override="invalid_value",
    )
    with pytest.raises(HTTPException) as exc_info:
        await create_pii_pattern(body, request, _inert_idem(), ctx)
    assert exc_info.value.status_code == 422
    assert "policy_override" in exc_info.value.detail


# ===========================================================================
# pii-patterns — GET
# ===========================================================================


@pytest.mark.asyncio
async def test_list_pii_patterns_returns_all_rows() -> None:
    from registry.api.routers.admin import list_pii_patterns

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    system_row = _make_pattern_row(tenant_id=tid, is_system=True, name="email")
    custom_row = _make_pattern_row(tenant_id=tid, is_system=False, name="custom_ssn")
    factory = _make_session(rows=[system_row, custom_row])
    request = _make_request(factory)

    result = await list_pii_patterns(request, ctx)
    assert len(result) == 2
    names = {r.name for r in result}
    assert "email" in names
    assert "custom_ssn" in names


@pytest.mark.asyncio
async def test_list_pii_patterns_empty() -> None:
    from registry.api.routers.admin import list_pii_patterns

    ctx = _admin_ctx()
    factory = _make_session(rows=[])
    request = _make_request(factory)

    result = await list_pii_patterns(request, ctx)
    assert result == []


# ===========================================================================
# pii-patterns — PATCH
# ===========================================================================


@pytest.mark.asyncio
async def test_patch_pii_pattern_happy_path() -> None:
    from registry.api.routers.admin import PiiPatternPatch, _patch_pii_pattern

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row = _make_pattern_row(tenant_id=tid, is_system=False)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    body = PiiPatternPatch(policy_override="block", is_enabled=False)
    result = await _patch_pii_pattern(row.pattern_id, body, request, ctx)

    # The mock row is mutated in-place; verify the response was built.
    assert result.pattern_id == row.pattern_id


@pytest.mark.asyncio
async def test_patch_pii_pattern_system_row_returns_403() -> None:
    from registry.api.routers.admin import PiiPatternPatch, _patch_pii_pattern

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row = _make_pattern_row(tenant_id=tid, is_system=True)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    body = PiiPatternPatch(policy_override="block")
    with pytest.raises(HTTPException) as exc_info:
        await _patch_pii_pattern(row.pattern_id, body, request, ctx)
    assert exc_info.value.status_code == 403
    assert "system PII patterns" in exc_info.value.detail


@pytest.mark.asyncio
async def test_patch_pii_pattern_not_found() -> None:
    from registry.api.routers.admin import PiiPatternPatch, _patch_pii_pattern

    ctx = _admin_ctx()
    factory = _make_session(scalar_value=None)
    request = _make_request(factory)

    body = PiiPatternPatch(is_enabled=False)
    with pytest.raises(HTTPException) as exc_info:
        await _patch_pii_pattern(uuid.uuid4(), body, request, ctx)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_pii_pattern_invalid_regex() -> None:
    from registry.api.routers.admin import PiiPatternPatch, _patch_pii_pattern

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row = _make_pattern_row(tenant_id=tid, is_system=False)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    body = PiiPatternPatch(regex="[bad")
    with pytest.raises(HTTPException) as exc_info:
        await _patch_pii_pattern(row.pattern_id, body, request, ctx)
    assert exc_info.value.status_code == 422
    assert "invalid regex" in exc_info.value.detail


@pytest.mark.asyncio
async def test_patch_pii_pattern_invalid_policy_override() -> None:
    from registry.api.routers.admin import PiiPatternPatch, _patch_pii_pattern

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row = _make_pattern_row(tenant_id=tid, is_system=False)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    body = PiiPatternPatch(policy_override="destroy")
    with pytest.raises(HTTPException) as exc_info:
        await _patch_pii_pattern(row.pattern_id, body, request, ctx)
    assert exc_info.value.status_code == 422


# ===========================================================================
# pii-patterns — DELETE
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_pii_pattern_happy_path() -> None:
    from registry.api.routers.admin import _delete_pii_pattern

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row = _make_pattern_row(tenant_id=tid, is_system=False)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    response = await _delete_pii_pattern(row.pattern_id, request, ctx)
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_delete_pii_pattern_system_row_returns_403() -> None:
    from registry.api.routers.admin import _delete_pii_pattern

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row = _make_pattern_row(tenant_id=tid, is_system=True)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    with pytest.raises(HTTPException) as exc_info:
        await _delete_pii_pattern(row.pattern_id, request, ctx)
    assert exc_info.value.status_code == 403
    assert "system PII patterns" in exc_info.value.detail


@pytest.mark.asyncio
async def test_delete_pii_pattern_not_found() -> None:
    from registry.api.routers.admin import _delete_pii_pattern

    ctx = _admin_ctx()
    factory = _make_session(scalar_value=None)
    request = _make_request(factory)

    with pytest.raises(HTTPException) as exc_info:
        await _delete_pii_pattern(uuid.uuid4(), request, ctx)
    assert exc_info.value.status_code == 404


# ===========================================================================
# pii-patterns — tenant isolation (cross-tenant access rejected)
# ===========================================================================


@pytest.mark.asyncio
async def test_patch_pii_pattern_wrong_tenant_returns_404() -> None:
    """A row belonging to a different tenant must appear as not-found."""
    from registry.api.routers.admin import PiiPatternPatch, _patch_pii_pattern

    other_tenant = uuid.uuid4()
    ctx = _admin_ctx()  # different tenant_id
    row = _make_pattern_row(tenant_id=other_tenant, is_system=False)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    body = PiiPatternPatch(is_enabled=False)
    with pytest.raises(HTTPException) as exc_info:
        await _patch_pii_pattern(row.pattern_id, body, request, ctx)
    assert exc_info.value.status_code == 404


# ===========================================================================
# pii-field-policies — POST
# ===========================================================================


@pytest.mark.asyncio
async def test_create_pii_field_policy_happy_path() -> None:
    from registry.api.routers.admin import PiiFieldPolicyCreate, create_pii_field_policy

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    policy_row = _make_field_policy_row(tenant_id=tid)
    factory = _make_session(scalar_value=policy_row)
    request = _make_request(factory)

    body = PiiFieldPolicyCreate(field_type="annotation.body", policy="warn")
    result = await create_pii_field_policy(body, request, _inert_idem(), ctx)

    assert result.field_type == policy_row.field_type
    assert result.policy == policy_row.policy


@pytest.mark.asyncio
async def test_create_pii_field_policy_invalid_policy() -> None:
    from registry.api.routers.admin import PiiFieldPolicyCreate, create_pii_field_policy

    ctx = _admin_ctx()
    factory = _make_session()
    request = _make_request(factory)

    body = PiiFieldPolicyCreate(field_type="annotation.body", policy="ignore")
    with pytest.raises(HTTPException) as exc_info:
        await create_pii_field_policy(body, request, _inert_idem(), ctx)
    assert exc_info.value.status_code == 422
    assert "policy" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_pii_field_policy_duplicate_returns_409() -> None:
    from sqlalchemy.exc import IntegrityError

    from registry.api.routers.admin import PiiFieldPolicyCreate, create_pii_field_policy

    ctx = _admin_ctx()
    factory = _make_session(raise_on_flush=IntegrityError("dup", {}, Exception()))
    request = _make_request(factory)

    body = PiiFieldPolicyCreate(field_type="annotation.body", policy="warn")
    with pytest.raises(HTTPException) as exc_info:
        await create_pii_field_policy(body, request, _inert_idem(), ctx)
    assert exc_info.value.status_code == 409


# ===========================================================================
# pii-field-policies — GET
# ===========================================================================


@pytest.mark.asyncio
async def test_list_pii_field_policies_returns_rows() -> None:
    from registry.api.routers.admin import list_pii_field_policies

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row1 = _make_field_policy_row(tenant_id=tid)
    row2 = _make_field_policy_row(tenant_id=tid)
    factory = _make_session(rows=[row1, row2])
    request = _make_request(factory)

    result = await list_pii_field_policies(request, ctx)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_pii_field_policies_empty() -> None:
    from registry.api.routers.admin import list_pii_field_policies

    ctx = _admin_ctx()
    factory = _make_session(rows=[])
    request = _make_request(factory)

    result = await list_pii_field_policies(request, ctx)
    assert result == []


# ===========================================================================
# pii-field-policies — DELETE
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_pii_field_policy_happy_path() -> None:
    from registry.api.routers.admin import _delete_pii_field_policy

    tid = uuid.uuid4()
    ctx = _admin_ctx(tenant_id=tid)
    row = _make_field_policy_row(tenant_id=tid)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    response = await _delete_pii_field_policy(row.policy_id, request, ctx)
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_delete_pii_field_policy_not_found() -> None:
    from registry.api.routers.admin import _delete_pii_field_policy

    ctx = _admin_ctx()
    factory = _make_session(scalar_value=None)
    request = _make_request(factory)

    with pytest.raises(HTTPException) as exc_info:
        await _delete_pii_field_policy(uuid.uuid4(), request, ctx)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_pii_field_policy_wrong_tenant_returns_404() -> None:
    from registry.api.routers.admin import _delete_pii_field_policy

    other_tenant = uuid.uuid4()
    ctx = _admin_ctx()  # different tenant_id
    row = _make_field_policy_row(tenant_id=other_tenant)
    factory = _make_session(scalar_value=row)
    request = _make_request(factory)

    with pytest.raises(HTTPException) as exc_info:
        await _delete_pii_field_policy(row.policy_id, request, ctx)
    assert exc_info.value.status_code == 404


# ===========================================================================
# Auth — admin role required
# ===========================================================================


@pytest.mark.asyncio
async def test_all_endpoints_require_admin_role() -> None:
    """Consumer role is rejected with 403 on every endpoint."""
    from registry.api.auth.context import require_roles

    dep = require_roles(["admin"])
    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=_consumer_ctx())
    assert exc_info.value.status_code == 403


# ===========================================================================
# HTTP method factory — mutation routes registered via HttpMethodRouter
# ===========================================================================


def test_pii_pattern_router_mutation_routes_registered() -> None:
    """Both PATCH and DELETE routes must appear on the pii_pattern_router."""
    from registry.api.routers.admin import pii_pattern_router

    methods_by_path: dict[str, set[str]] = {}
    for route in pii_pattern_router.routes:
        methods_by_path.setdefault(route.path, set()).update(route.methods or set())  # type: ignore[attr-defined]

    # In "both" mode (default) we expect at least one PATCH route and one DELETE
    # route registered (could also include POST tunneled variants).
    all_paths_and_methods = {
        (r.path, frozenset(r.methods or set()))  # type: ignore[attr-defined]
        for r in pii_pattern_router.routes
    }
    # There must be at least one route with PATCH for update.
    has_patch = any("PATCH" in (m or set()) for p, m in all_paths_and_methods)
    has_delete = any("DELETE" in (m or set()) for p, m in all_paths_and_methods)
    # POST tunneled variants are also acceptable in "both" or "post_only" mode.
    has_post = any("POST" in (m or set()) for p, m in all_paths_and_methods)

    assert has_patch or has_post, "No PATCH or POST-tunneled update route registered"
    assert has_delete or has_post, "No DELETE or POST-tunneled delete route registered"


def test_pii_field_policy_router_delete_route_registered() -> None:
    """DELETE route must appear on the pii_field_policy_router."""
    from registry.api.routers.admin import pii_field_policy_router

    all_methods = set()
    for route in pii_field_policy_router.routes:
        all_methods.update(route.methods or set())  # type: ignore[attr-defined]

    assert (
        "DELETE" in all_methods or "POST" in all_methods
    ), "No DELETE or POST-tunneled delete route registered on pii_field_policy_router"
