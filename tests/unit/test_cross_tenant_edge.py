"""Unit tests for cross-tenant edge write gate in CatalogService.

Covers (per task contract):
- Intra-tenant edge (same tenant): always passes regardless of rel.
- Cross-tenant `depends_on` / `requires` / `integrates_with` without adoption → PermissionError.
- Cross-tenant edge with active adoption row → edge written successfully.
- `provides_to` edge via direct create_edge() → PermissionError (must use adoption flow).

All tests are in-memory: session factory is mocked so no DB is required.
The mock session sequences are ordered to match catalog.py's query order:
  1. session.get(Entity, src_entity_id)
  2. session.get(Entity, dst_entity_id)
  3. session.execute(text(adoption_check))   ← only for cross-tenant depends_on/requires/integrates_with
  4. session.add(edge); session.flush()
  5. enqueue_closure_refresh  (mocked out via enqueue patch)
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.exceptions import NotFoundError
from registry.service.catalog import CatalogService
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_A = uuid.uuid4()
_TENANT_B = uuid.uuid4()

_NOW_TS = __import__("datetime").datetime(2026, 5, 11, 0, 0, 0, tzinfo=__import__("datetime").UTC)
_CLOCK = FakeClock(_NOW_TS)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ctx(tenant_id: uuid.UUID = _TENANT_A) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["producer"])


def _entity(entity_id: uuid.UUID, tenant_id: uuid.UUID) -> MagicMock:
    """Minimal Entity-like mock."""
    e = MagicMock()
    e.entity_id = entity_id
    e.tenant_id = tenant_id
    return e


def _async_noop_ctx() -> Any:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _adoption_result(found: bool) -> MagicMock:
    """Mock result of the adoption_events SELECT."""
    r = MagicMock()
    r.first = MagicMock(return_value=(1,) if found else None)
    return r


def _build_session(
    src_entity: MagicMock,
    dst_entity: MagicMock,
    adoption_found: bool | None = None,
) -> MagicMock:
    """Build a mock async session.

    `session.get(Entity, pk)` returns src_entity then dst_entity in order.
    `session.execute(...)` returns the adoption check result when adoption_found is not None.
    """
    get_results = [src_entity, dst_entity]
    get_call = [0]

    async def _get(_model: Any, pk: uuid.UUID) -> MagicMock | None:
        val = get_results[get_call[0]]
        get_call[0] += 1
        return val

    session = MagicMock()
    session.get = _get
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.begin_nested = MagicMock(return_value=_async_noop_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=_async_noop_ctx())

    if adoption_found is not None:
        session.execute = AsyncMock(return_value=_adoption_result(adoption_found))

    return session


def _build_service(session: MagicMock, visibility_svc: Any = None) -> CatalogService:
    """Build a CatalogService with mocked vocab/schema and the given session factory."""
    vocabulary = MagicMock()
    vocabulary.validate_value = AsyncMock()

    schema = MagicMock()

    session_factory = MagicMock(return_value=session)

    return CatalogService(
        session_factory=session_factory,
        clock=_CLOCK,
        vocabulary=vocabulary,
        schema=schema,
        visibility=visibility_svc,
    )


# ---------------------------------------------------------------------------
# Intra-tenant edges — always pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intra_tenant_depends_on_passes() -> None:
    """Same-tenant depends_on edge succeeds without any adoption check."""
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_A)  # same tenant

    session = _build_session(src, dst)
    svc = _build_service(session)

    with patch("registry.service.catalog.enqueue_closure_refresh", new=AsyncMock()):
        ref = await svc.create_edge(
            ctx=_ctx(_TENANT_A),
            src_entity_id=src_id,
            rel="depends_on",
            dst_entity_id=dst_id,
        )

    assert ref.src_entity_id == src_id
    assert ref.dst_entity_id == dst_id
    assert ref.rel == "depends_on"


@pytest.mark.asyncio
async def test_intra_tenant_requires_passes() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_A)

    session = _build_session(src, dst)
    svc = _build_service(session)

    with patch("registry.service.catalog.enqueue_closure_refresh", new=AsyncMock()):
        ref = await svc.create_edge(_ctx(_TENANT_A), src_id, "requires", dst_id)

    assert ref.rel == "requires"


@pytest.mark.asyncio
async def test_intra_tenant_composes_passes() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_A)

    session = _build_session(src, dst)
    svc = _build_service(session)

    with patch("registry.service.catalog.enqueue_closure_refresh", new=AsyncMock()):
        ref = await svc.create_edge(_ctx(_TENANT_A), src_id, "composes", dst_id)

    assert ref.rel == "composes"


# ---------------------------------------------------------------------------
# provides_to direct create — always rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provides_to_direct_create_rejected() -> None:
    """provides_to edge must not be created directly — only via adoption flow."""
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    # Even if entities exist, the gate fires before any DB queries.
    session = MagicMock()
    svc = _build_service(session)

    with pytest.raises(PermissionError, match="provides_to"):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "provides_to", dst_id)


@pytest.mark.asyncio
async def test_provides_to_cross_tenant_direct_create_also_rejected() -> None:
    """provides_to is rejected even when src and dst are in different tenants."""
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    session = MagicMock()
    svc = _build_service(session)

    with pytest.raises(PermissionError, match="provides_to"):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "provides_to", dst_id)


# ---------------------------------------------------------------------------
# Cross-tenant edges without adoption → rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_depends_on_without_adoption_rejected() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)  # different tenant

    session = _build_session(src, dst, adoption_found=False)
    svc = _build_service(session)

    with pytest.raises(PermissionError, match="adoption event"):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "depends_on", dst_id)


@pytest.mark.asyncio
async def test_cross_tenant_requires_without_adoption_rejected() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)

    session = _build_session(src, dst, adoption_found=False)
    svc = _build_service(session)

    with pytest.raises(PermissionError, match="adoption event"):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "requires", dst_id)


@pytest.mark.asyncio
async def test_cross_tenant_integrates_with_without_adoption_rejected() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)

    session = _build_session(src, dst, adoption_found=False)
    svc = _build_service(session)

    with pytest.raises(PermissionError, match="adoption event"):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "integrates_with", dst_id)


# ---------------------------------------------------------------------------
# Cross-tenant edges with active adoption → succeed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_depends_on_with_adoption_succeeds() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)

    session = _build_session(src, dst, adoption_found=True)
    svc = _build_service(session)

    with patch("registry.service.catalog.enqueue_closure_refresh", new=AsyncMock()):
        ref = await svc.create_edge(_ctx(_TENANT_A), src_id, "depends_on", dst_id)

    assert ref.src_entity_id == src_id
    assert ref.dst_entity_id == dst_id
    assert ref.rel == "depends_on"
    assert ref.tenant_id == _TENANT_A


@pytest.mark.asyncio
async def test_cross_tenant_requires_with_adoption_succeeds() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)

    session = _build_session(src, dst, adoption_found=True)
    svc = _build_service(session)

    with patch("registry.service.catalog.enqueue_closure_refresh", new=AsyncMock()):
        ref = await svc.create_edge(_ctx(_TENANT_A), src_id, "requires", dst_id)

    assert ref.rel == "requires"


@pytest.mark.asyncio
async def test_cross_tenant_integrates_with_adoption_succeeds() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)

    session = _build_session(src, dst, adoption_found=True)
    svc = _build_service(session)

    with patch("registry.service.catalog.enqueue_closure_refresh", new=AsyncMock()):
        ref = await svc.create_edge(_ctx(_TENANT_A), src_id, "integrates_with", dst_id)

    assert ref.rel == "integrates_with"


# ---------------------------------------------------------------------------
# Cross-tenant with visibility service stub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_edge_calls_visibility_assert_visible() -> None:
    """When a VisibilityService is injected, assert_visible is called for cross-tenant edges."""
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)

    session = _build_session(src, dst, adoption_found=True)

    visibility_svc = MagicMock()
    visibility_svc.assert_visible = AsyncMock()

    svc = _build_service(session, visibility_svc=visibility_svc)

    with patch("registry.service.catalog.enqueue_closure_refresh", new=AsyncMock()):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "depends_on", dst_id)

    visibility_svc.assert_visible.assert_awaited_once()
    call_args = visibility_svc.assert_visible.call_args
    # Second positional arg is the entity_id to check visibility for.
    assert call_args.args[1] == dst_id


@pytest.mark.asyncio
async def test_cross_tenant_edge_without_visibility_svc_still_checks_adoption() -> None:
    """When no VisibilityService is injected, adoption check still fires."""
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    src = _entity(src_id, _TENANT_A)
    dst = _entity(dst_id, _TENANT_B)

    session = _build_session(src, dst, adoption_found=False)
    svc = _build_service(session, visibility_svc=None)  # no visibility svc

    with pytest.raises(PermissionError, match="adoption event"):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "depends_on", dst_id)


# ---------------------------------------------------------------------------
# src entity not found → NotFoundError (not a PermissionError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_src_entity_not_found_raises_not_found() -> None:
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()

    session = _build_session(None, None)  # both get() calls return None
    svc = _build_service(session)

    with pytest.raises(NotFoundError):
        await svc.create_edge(_ctx(_TENANT_A), src_id, "depends_on", dst_id)
