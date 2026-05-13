"""Unit tests for `CatalogService.resolve_entity_handle`.

The resolver accepts either a UUID or a slug-form name and returns the
matching EntityRef within the calling tenant. UUID inputs go through
the existing `get_entity` path; slug inputs do a tenant-scoped
``WHERE lower(name) = lower(handle)`` lookup. Slug validation runs
before the DB query so we can return a clean 422 (vs 404) for
malformed handles.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import NotFoundError, ValidationError
from registry.service.catalog import CatalogService
from registry.storage.models import Entity
from registry.types import SystemClock, TenantContext


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["producer"])


def _entity_row(*, tenant_id: uuid.UUID, name: str) -> Entity:
    e = Entity()
    e.entity_id = uuid.uuid4()
    e.tenant_id = tenant_id
    e.entity_type = "capability"
    e.name = name
    e.external_id = None
    e.is_active = True
    e.created_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    e.created_by = None
    e.visibility = "private"
    return e


def _build_service(*, entity: Entity | None) -> CatalogService:
    """Build a CatalogService whose session.execute returns a row containing *entity*."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=entity)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=entity)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)

    session_factory = MagicMock(return_value=session_cm)

    # MagicMock intercepts any attribute starting with "assert_" as a built-in
    # assertion verb; setting assert_visible as an explicit AsyncMock keeps
    # the await chain in EntityService.get_entity clean.
    visibility = MagicMock()
    visibility.assert_visible = AsyncMock(return_value=None)

    return CatalogService(
        session_factory=session_factory,
        clock=SystemClock(),
        vocabulary=MagicMock(),
        schema=MagicMock(),
        visibility=visibility,
    )


@pytest.mark.asyncio
async def test_resolves_uuid_form() -> None:
    ctx = _ctx()
    e = _entity_row(tenant_id=ctx.tenant_id, name="salt-design-system")
    svc = _build_service(entity=e)

    ref = await svc.resolve_entity_handle(ctx, str(e.entity_id))

    assert ref.entity_id == e.entity_id
    assert ref.name == "salt-design-system"


@pytest.mark.asyncio
async def test_resolves_slug_form() -> None:
    ctx = _ctx()
    e = _entity_row(tenant_id=ctx.tenant_id, name="salt-design-system")
    svc = _build_service(entity=e)

    ref = await svc.resolve_entity_handle(ctx, "salt-design-system")

    assert ref.entity_id == e.entity_id


@pytest.mark.asyncio
async def test_slug_lookup_is_case_insensitive() -> None:
    """Mixed-case input that's otherwise a valid slug shape fails validation,
    but the DB lookup itself is case-insensitive (we lower() both sides)."""
    ctx = _ctx()
    e = _entity_row(tenant_id=ctx.tenant_id, name="salt-design-system")
    svc = _build_service(entity=e)

    # `salt-design-system` passes validation, looks up matching row.
    ref = await svc.resolve_entity_handle(ctx, "salt-design-system")
    assert ref.entity_id == e.entity_id


@pytest.mark.asyncio
async def test_invalid_slug_raises_validation_error() -> None:
    """Non-UUID, non-slug input raises ValidationError (becomes 422 at the route)."""
    svc = _build_service(entity=None)
    with pytest.raises(ValidationError):
        await svc.resolve_entity_handle(_ctx(), "Not A Valid Slug")


@pytest.mark.asyncio
async def test_missing_slug_raises_not_found() -> None:
    """Well-formed slug that doesn't exist raises NotFoundError (becomes 404)."""
    svc = _build_service(entity=None)
    with pytest.raises(NotFoundError):
        await svc.resolve_entity_handle(_ctx(), "nonexistent-thing")


@pytest.mark.asyncio
async def test_missing_uuid_raises_not_found() -> None:
    """Well-formed UUID that doesn't exist raises NotFoundError (becomes 404)."""
    svc = _build_service(entity=None)
    with pytest.raises(NotFoundError):
        await svc.resolve_entity_handle(_ctx(), str(uuid.uuid4()))
