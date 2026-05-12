"""Unit tests for GET /v1/admin/tenants/{slug} and GET /v1/admin/actors[/{id}].

All tests use mocked session factories — no DB required.

Coverage:
  - GET /v1/admin/tenants/<own slug>  → 200 with _links.self
  - GET /v1/admin/tenants/<UUID str>  → 200 via UUID resolution
  - GET /v1/admin/tenants/<other slug> → 404 (cross-tenant isolation)
  - GET /v1/admin/tenants/<unknown>   → 404
  - ?view=audit on tenant             → includes is_active
  - GET /v1/admin/actors/<own id>     → 200 with _links.self
  - GET /v1/admin/actors/<other id>   → 404 (cross-tenant isolation)
  - ?view=audit on actor              → includes oidc_subject
  - GET /v1/admin/actors              → list with envelope + page_size
  - GET /v1/admin/actors cursor pagination → next_cursor set when more rows
  - ?view=audit on list               → includes oidc_subject on items
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_ctx(tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
    )


def _make_tenant(tenant_id: uuid.UUID | None = None, slug: str = "acme") -> MagicMock:
    row = MagicMock()
    row.tenant_id = tenant_id or uuid.uuid4()
    row.slug = slug
    row.display_name = "Acme Corp"
    row.created_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    row.is_active = True
    return row


def _make_actor(tenant_id: uuid.UUID | None = None, oidc_subject: str | None = "sub|123") -> MagicMock:
    row = MagicMock()
    row.actor_id = uuid.uuid4()
    row.tenant_id = tenant_id or uuid.uuid4()
    row.display_name = "Alice"
    row.email = "alice@example.com"
    row.actor_kind = "human"
    row.created_at = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)
    row.oidc_subject = oidc_subject
    return row


def _make_session(scalar_value: Any = None, rows: list[Any] | None = None) -> MagicMock:
    """Build a mock session factory that returns scalar_value or rows."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows or [])))

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=scalar_value)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)

    return MagicMock(return_value=session)


def _make_request(factory: Any) -> MagicMock:
    req = MagicMock()
    req.app.state.session_factory = factory
    return req


# ===========================================================================
# GET /v1/admin/tenants/{slug}
# ===========================================================================


@pytest.mark.asyncio
async def test_get_tenant_own_slug_returns_200() -> None:
    from registry.api.routers.admin import get_tenant

    tid = uuid.uuid4()
    tenant_row = _make_tenant(tenant_id=tid, slug="acme")
    factory = _make_session(scalar_value=tenant_row)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await get_tenant(slug="acme", request=request, view=None, ctx=ctx)

    assert result.slug == "acme"
    assert result.tenant_id == tid
    assert result.links is not None
    assert result.links.self == "/v1/admin/tenants/acme"


@pytest.mark.asyncio
async def test_get_tenant_own_uuid_returns_200() -> None:
    from registry.api.routers.admin import get_tenant

    tid = uuid.uuid4()
    tenant_row = _make_tenant(tenant_id=tid, slug="acme")
    factory = _make_session(scalar_value=tenant_row)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    # Pass the tenant's UUID as a string — should resolve correctly.
    result = await get_tenant(slug=str(tid), request=request, view=None, ctx=ctx)

    assert result.tenant_id == tid


@pytest.mark.asyncio
async def test_get_tenant_cross_tenant_returns_404() -> None:
    from registry.api.routers.admin import get_tenant

    # Tenant row exists but belongs to a different tenant than the caller.
    other_tid = uuid.uuid4()
    tenant_row = _make_tenant(tenant_id=other_tid, slug="other")
    factory = _make_session(scalar_value=tenant_row)
    request = _make_request(factory)
    ctx = _admin_ctx()  # different tenant_id

    with pytest.raises(HTTPException) as exc_info:
        await get_tenant(slug="other", request=request, view=None, ctx=ctx)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_tenant_not_found_returns_404() -> None:
    from registry.api.routers.admin import get_tenant

    factory = _make_session(scalar_value=None)
    request = _make_request(factory)
    ctx = _admin_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await get_tenant(slug="ghost", request=request, view=None, ctx=ctx)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_tenant_audit_view_includes_is_active() -> None:
    from registry.api.routers.admin import get_tenant

    tid = uuid.uuid4()
    tenant_row = _make_tenant(tenant_id=tid, slug="acme")
    factory = _make_session(scalar_value=tenant_row)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await get_tenant(slug="acme", request=request, view="audit", ctx=ctx)

    assert result.is_active is True  # populated by audit view


@pytest.mark.asyncio
async def test_get_tenant_default_view_excludes_is_active() -> None:
    from registry.api.routers.admin import get_tenant

    tid = uuid.uuid4()
    tenant_row = _make_tenant(tenant_id=tid, slug="acme")
    factory = _make_session(scalar_value=tenant_row)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await get_tenant(slug="acme", request=request, view=None, ctx=ctx)

    assert result.is_active is None  # not set in default view


# ===========================================================================
# GET /v1/admin/actors/{actor_id}
# ===========================================================================


@pytest.mark.asyncio
async def test_get_actor_own_tenant_returns_200() -> None:
    from registry.api.routers.admin import get_actor

    tid = uuid.uuid4()
    actor_row = _make_actor(tenant_id=tid)
    factory = _make_session(scalar_value=actor_row)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await get_actor(actor_id=actor_row.actor_id, request=request, view=None, ctx=ctx)

    assert result.actor_id == actor_row.actor_id
    assert result.display_name == "Alice"
    assert result.links is not None
    assert result.links.self == f"/v1/admin/actors/{actor_row.actor_id}"


@pytest.mark.asyncio
async def test_get_actor_cross_tenant_returns_404() -> None:
    from registry.api.routers.admin import get_actor

    # Actor belongs to a different tenant.
    other_tid = uuid.uuid4()
    actor_row = _make_actor(tenant_id=other_tid)
    factory = _make_session(scalar_value=actor_row)
    request = _make_request(factory)
    ctx = _admin_ctx()  # different tenant_id

    with pytest.raises(HTTPException) as exc_info:
        await get_actor(actor_id=actor_row.actor_id, request=request, view=None, ctx=ctx)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_actor_not_found_returns_404() -> None:
    from registry.api.routers.admin import get_actor

    factory = _make_session(scalar_value=None)
    request = _make_request(factory)
    ctx = _admin_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await get_actor(actor_id=uuid.uuid4(), request=request, view=None, ctx=ctx)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_actor_audit_view_includes_oidc_subject() -> None:
    from registry.api.routers.admin import get_actor

    tid = uuid.uuid4()
    actor_row = _make_actor(tenant_id=tid, oidc_subject="sub|abc")
    factory = _make_session(scalar_value=actor_row)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await get_actor(actor_id=actor_row.actor_id, request=request, view="audit", ctx=ctx)

    assert result.oidc_subject == "sub|abc"


@pytest.mark.asyncio
async def test_get_actor_default_view_excludes_oidc_subject() -> None:
    from registry.api.routers.admin import get_actor

    tid = uuid.uuid4()
    actor_row = _make_actor(tenant_id=tid, oidc_subject="sub|abc")
    factory = _make_session(scalar_value=actor_row)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await get_actor(actor_id=actor_row.actor_id, request=request, view=None, ctx=ctx)

    assert result.oidc_subject is None


# ===========================================================================
# GET /v1/admin/actors (list)
# ===========================================================================


@pytest.mark.asyncio
async def test_list_actors_returns_tenant_scoped_items() -> None:
    from registry.api.routers.admin import list_actors

    tid = uuid.uuid4()
    actor1 = _make_actor(tenant_id=tid)
    actor2 = _make_actor(tenant_id=tid)
    factory = _make_session(rows=[actor1, actor2])
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await list_actors(request=request, view=None, page_size=50, cursor=None, ctx=ctx)

    assert len(result.items) == 2
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_list_actors_respects_page_size_and_sets_next_cursor() -> None:
    from registry.api.routers.admin import list_actors

    tid = uuid.uuid4()
    # Return page_size + 1 rows to trigger cursor generation.
    actors = [_make_actor(tenant_id=tid) for _ in range(3)]
    factory = _make_session(rows=actors)
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await list_actors(request=request, view=None, page_size=2, cursor=None, ctx=ctx)

    assert len(result.items) == 2
    assert result.next_cursor is not None


@pytest.mark.asyncio
async def test_list_actors_audit_view_includes_oidc_subject() -> None:
    from registry.api.routers.admin import list_actors

    tid = uuid.uuid4()
    actor = _make_actor(tenant_id=tid, oidc_subject="sub|xyz")
    factory = _make_session(rows=[actor])
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await list_actors(request=request, view="audit", page_size=50, cursor=None, ctx=ctx)

    assert len(result.items) == 1
    assert result.items[0].oidc_subject == "sub|xyz"


@pytest.mark.asyncio
async def test_list_actors_default_view_excludes_oidc_subject() -> None:
    from registry.api.routers.admin import list_actors

    tid = uuid.uuid4()
    actor = _make_actor(tenant_id=tid, oidc_subject="sub|xyz")
    factory = _make_session(rows=[actor])
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await list_actors(request=request, view=None, page_size=50, cursor=None, ctx=ctx)

    assert result.items[0].oidc_subject is None


@pytest.mark.asyncio
async def test_list_actors_items_have_self_links() -> None:
    from registry.api.routers.admin import list_actors

    tid = uuid.uuid4()
    actor = _make_actor(tenant_id=tid)
    factory = _make_session(rows=[actor])
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await list_actors(request=request, view=None, page_size=50, cursor=None, ctx=ctx)

    assert result.items[0].links is not None
    assert result.items[0].links.self == f"/v1/admin/actors/{actor.actor_id}"


@pytest.mark.asyncio
async def test_list_actors_empty_returns_no_cursor() -> None:
    from registry.api.routers.admin import list_actors

    tid = uuid.uuid4()
    factory = _make_session(rows=[])
    request = _make_request(factory)
    ctx = _admin_ctx(tenant_id=tid)

    result = await list_actors(request=request, view=None, page_size=50, cursor=None, ctx=ctx)

    assert result.items == []
    assert result.next_cursor is None
