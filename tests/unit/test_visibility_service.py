"""Unit tests for VisibilityService — cross-tenant visibility chokepoint.

All DB interactions are mocked via AsyncMock session factories; no Postgres
instance required.

Coverage:
- ``filter_entities``: private entity invisible to foreign tenant.
- ``filter_entities``: tenant-shared entity visible to ACL tenant, invisible to others.
- ``filter_entities``: public entity visible to any caller.
- ``filter_entities``: owning tenant always sees own private entity.
- ``filter_entities``: preserves input order; drops invisible IDs.
- ``filter_entities``: unknown entity_id silently excluded.
- ``assert_visible``: raises PermissionError for private entity (foreign caller).
- ``assert_visible``: passes for public entity.
- ``assert_visible``: passes for tenant-shared entity when caller is in ACL.
- ``assert_visible``: raises NotFoundError for non-existent entity.
- ``set_visibility``: raises ValidationError for unknown visibility string.
- ``set_visibility``: raises ValidationError for tenant-shared without ACL.
- ``set_visibility``: writes visibility column and closes/creates attribute.
- ``_validate_visibility_input``: standalone validation helper.
- ``_parse_shared_with_tenants``: handles list-of-strings, skips bad items.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import NotFoundError, ValidationError
from registry.service.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_TENANT_SHARED,
    VisibilityService,
    _parse_shared_with_tenants,
    _validate_visibility_input,
)
from registry.storage.models import Attribute, Entity
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

_TENANT_A = uuid.uuid4()
_TENANT_B = uuid.uuid4()
_TENANT_C = uuid.uuid4()

_ACTOR_A = uuid.uuid4()
_ACTOR_B = uuid.uuid4()


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        actor_id=actor_id or uuid.uuid4(),
        roles=["consumer"],
    )


def _entity(
    entity_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID = _TENANT_A,
    visibility: str = VISIBILITY_PRIVATE,
) -> Entity:
    e = MagicMock(spec=Entity)
    e.entity_id = entity_id or uuid.uuid4()
    e.tenant_id = tenant_id
    e.visibility = visibility
    e.is_active = True
    return e


def _clock() -> FakeClock:
    return FakeClock(_NOW)


# ---------------------------------------------------------------------------
# Session factory mock helpers
# ---------------------------------------------------------------------------


def _async_noop_ctx() -> MagicMock:
    """Async context manager that does nothing — used for session.begin()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_session_factory(
    entities: list[Entity] | None = None,
    attributes: list[Attribute] | None = None,
) -> MagicMock:
    """Build an async session factory returning fixed rows.

    Handles both the read-only pattern
    ``async with factory() as session: session.execute(...)``
    and the write pattern
    ``async with factory() as session, session.begin(): session.execute(...)``.
    """
    entities = entities or []
    attributes = attributes or []

    session = MagicMock()

    # Model scalars query — return fixed rows keyed by table name in compiled SQL.
    async def _execute(stmt: Any) -> Any:  # noqa: ANN401
        result = MagicMock()
        compiled = str(stmt)
        if "attributes" in compiled:
            result.scalars.return_value.all.return_value = attributes
            result.scalar_one_or_none.return_value = attributes[0] if len(attributes) == 1 else None
        else:
            result.scalars.return_value.all.return_value = entities
            result.scalar_one_or_none.return_value = entities[0] if len(entities) == 1 else None
        return result

    session.execute = _execute
    session.add = MagicMock()
    # session.begin() must return an async context manager (not a coroutine).
    session.begin = MagicMock(return_value=_async_noop_ctx())
    # session itself must be usable as async context manager.
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session
    return factory


# ---------------------------------------------------------------------------
# _validate_visibility_input (pure function — no async needed)
# ---------------------------------------------------------------------------


def test_validate_visibility_private_ok() -> None:
    _validate_visibility_input(VISIBILITY_PRIVATE, None)  # no raise


def test_validate_visibility_public_ok() -> None:
    _validate_visibility_input(VISIBILITY_PUBLIC, None)  # no raise


def test_validate_visibility_tenant_shared_requires_acl() -> None:
    with pytest.raises(ValidationError, match="non-empty shared_with_tenants"):
        _validate_visibility_input(VISIBILITY_TENANT_SHARED, None)


def test_validate_visibility_tenant_shared_empty_list() -> None:
    with pytest.raises(ValidationError, match="non-empty shared_with_tenants"):
        _validate_visibility_input(VISIBILITY_TENANT_SHARED, [])


def test_validate_visibility_tenant_shared_with_acl_ok() -> None:
    _validate_visibility_input(VISIBILITY_TENANT_SHARED, [_TENANT_B])  # no raise


def test_validate_visibility_unknown_value() -> None:
    with pytest.raises(ValidationError, match="invalid visibility"):
        _validate_visibility_input("world-readable", None)


# ---------------------------------------------------------------------------
# _parse_shared_with_tenants (pure function)
# ---------------------------------------------------------------------------


def test_parse_shared_with_tenants_list_of_strings() -> None:
    tid = uuid.uuid4()
    result = _parse_shared_with_tenants([str(tid)])
    assert result == [tid]


def test_parse_shared_with_tenants_empty_list() -> None:
    assert _parse_shared_with_tenants([]) == []


def test_parse_shared_with_tenants_not_a_list() -> None:
    assert _parse_shared_with_tenants(None) == []  # type: ignore[arg-type]
    assert _parse_shared_with_tenants("bad") == []  # type: ignore[arg-type]


def test_parse_shared_with_tenants_skips_bad_entries() -> None:
    good = str(uuid.uuid4())
    result = _parse_shared_with_tenants([good, "not-a-uuid", 42])
    assert len(result) == 1
    assert str(result[0]) == good


# ---------------------------------------------------------------------------
# VisibilityService._is_visible (static, no I/O)
# ---------------------------------------------------------------------------


def test_is_visible_private_own_tenant() -> None:
    e = _entity(tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE)
    ctx = _ctx(_TENANT_A)
    assert VisibilityService._is_visible(ctx, e, []) is True


def test_is_visible_private_foreign_tenant() -> None:
    e = _entity(tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE)
    ctx = _ctx(_TENANT_B)
    assert VisibilityService._is_visible(ctx, e, []) is False


def test_is_visible_tenant_shared_in_acl() -> None:
    e = _entity(tenant_id=_TENANT_A, visibility=VISIBILITY_TENANT_SHARED)
    ctx = _ctx(_TENANT_B)
    assert VisibilityService._is_visible(ctx, e, [_TENANT_B, _TENANT_C]) is True


def test_is_visible_tenant_shared_not_in_acl() -> None:
    e = _entity(tenant_id=_TENANT_A, visibility=VISIBILITY_TENANT_SHARED)
    ctx = _ctx(_TENANT_C)
    # ACL only includes B
    assert VisibilityService._is_visible(ctx, e, [_TENANT_B]) is False


def test_is_visible_public_any_tenant() -> None:
    e = _entity(tenant_id=_TENANT_A, visibility=VISIBILITY_PUBLIC)
    for caller in (_TENANT_A, _TENANT_B, _TENANT_C):
        assert VisibilityService._is_visible(_ctx(caller), e, []) is True


def test_is_visible_tenant_shared_owner_always_visible() -> None:
    """Owner of a tenant-shared entity sees it even without being in their own ACL."""
    e = _entity(tenant_id=_TENANT_A, visibility=VISIBILITY_TENANT_SHARED)
    ctx = _ctx(_TENANT_A)
    assert VisibilityService._is_visible(ctx, e, [_TENANT_B]) is True


# ---------------------------------------------------------------------------
# VisibilityService.filter_entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_entities_private_own_tenant() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE)
    factory = _make_session_factory(entities=[entity])
    svc = VisibilityService(factory, _clock())
    result = await svc.filter_entities(_ctx(_TENANT_A), [eid])
    assert result == [eid]


@pytest.mark.asyncio
async def test_filter_entities_private_foreign_tenant_excluded() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE)
    factory = _make_session_factory(entities=[entity])
    svc = VisibilityService(factory, _clock())
    result = await svc.filter_entities(_ctx(_TENANT_B), [eid])
    assert result == []


@pytest.mark.asyncio
async def test_filter_entities_tenant_shared_acl_tenant_b_visible() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_TENANT_SHARED)

    attr = MagicMock(spec=Attribute)
    attr.entity_id = eid
    attr.value = [str(_TENANT_B)]

    factory = _make_session_factory(entities=[entity], attributes=[attr])
    svc = VisibilityService(factory, _clock())
    result = await svc.filter_entities(_ctx(_TENANT_B), [eid])
    assert result == [eid]


@pytest.mark.asyncio
async def test_filter_entities_tenant_shared_acl_tenant_c_excluded() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_TENANT_SHARED)

    attr = MagicMock(spec=Attribute)
    attr.entity_id = eid
    attr.value = [str(_TENANT_B)]  # only B in ACL

    factory = _make_session_factory(entities=[entity], attributes=[attr])
    svc = VisibilityService(factory, _clock())
    result = await svc.filter_entities(_ctx(_TENANT_C), [eid])
    assert result == []


@pytest.mark.asyncio
async def test_filter_entities_public_visible_to_all() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PUBLIC)
    factory = _make_session_factory(entities=[entity])
    svc = VisibilityService(factory, _clock())
    for caller in (_TENANT_A, _TENANT_B, _TENANT_C):
        result = await svc.filter_entities(_ctx(caller), [eid])
        assert result == [eid], f"expected visible to {caller}"


@pytest.mark.asyncio
async def test_filter_entities_preserves_order() -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    entities = [
        _entity(entity_id=ids[0], tenant_id=_TENANT_A, visibility=VISIBILITY_PUBLIC),
        _entity(entity_id=ids[1], tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE),
        _entity(entity_id=ids[2], tenant_id=_TENANT_A, visibility=VISIBILITY_PUBLIC),
    ]
    factory = _make_session_factory(entities=entities)
    svc = VisibilityService(factory, _clock())
    # B should see only ids[0] and ids[2]
    result = await svc.filter_entities(_ctx(_TENANT_B), ids)
    assert result == [ids[0], ids[2]]


@pytest.mark.asyncio
async def test_filter_entities_unknown_entity_excluded() -> None:
    phantom = uuid.uuid4()
    factory = _make_session_factory(entities=[])  # DB returns nothing
    svc = VisibilityService(factory, _clock())
    result = await svc.filter_entities(_ctx(_TENANT_A), [phantom])
    assert result == []


@pytest.mark.asyncio
async def test_filter_entities_empty_input() -> None:
    factory = _make_session_factory(entities=[])
    svc = VisibilityService(factory, _clock())
    result = await svc.filter_entities(_ctx(_TENANT_A), [])
    assert result == []
    # Session factory must not be called for empty input.
    factory.assert_not_called()


# ---------------------------------------------------------------------------
# VisibilityService.assert_visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_visible_private_own_tenant_ok() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE)
    factory = _make_session_factory(entities=[entity])
    svc = VisibilityService(factory, _clock())
    await svc.assert_visible(_ctx(_TENANT_A), eid)  # no raise


@pytest.mark.asyncio
async def test_assert_visible_private_foreign_tenant_raises() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE)
    factory = _make_session_factory(entities=[entity])
    svc = VisibilityService(factory, _clock())
    with pytest.raises(PermissionError):
        await svc.assert_visible(_ctx(_TENANT_B), eid)


@pytest.mark.asyncio
async def test_assert_visible_public_ok() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PUBLIC)
    factory = _make_session_factory(entities=[entity])
    svc = VisibilityService(factory, _clock())
    await svc.assert_visible(_ctx(_TENANT_B), eid)  # no raise


@pytest.mark.asyncio
async def test_assert_visible_tenant_shared_in_acl_ok() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_TENANT_SHARED)

    attr = MagicMock(spec=Attribute)
    attr.entity_id = eid
    attr.value = [str(_TENANT_B)]

    factory = _make_session_factory(entities=[entity], attributes=[attr])
    svc = VisibilityService(factory, _clock())
    await svc.assert_visible(_ctx(_TENANT_B), eid)  # no raise


@pytest.mark.asyncio
async def test_assert_visible_not_found_raises() -> None:
    phantom = uuid.uuid4()
    factory = _make_session_factory(entities=[])
    svc = VisibilityService(factory, _clock())
    with pytest.raises(NotFoundError):
        await svc.assert_visible(_ctx(_TENANT_A), phantom)


# ---------------------------------------------------------------------------
# VisibilityService.set_visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_visibility_invalid_value_raises() -> None:
    factory = _make_session_factory()
    svc = VisibilityService(factory, _clock())
    with pytest.raises(ValidationError, match="invalid visibility"):
        await svc.set_visibility(_ctx(_TENANT_A), uuid.uuid4(), "open")


@pytest.mark.asyncio
async def test_set_visibility_tenant_shared_without_acl_raises() -> None:
    factory = _make_session_factory()
    svc = VisibilityService(factory, _clock())
    with pytest.raises(ValidationError, match="non-empty shared_with_tenants"):
        await svc.set_visibility(
            _ctx(_TENANT_A),
            uuid.uuid4(),
            VISIBILITY_TENANT_SHARED,
            shared_with_tenants=None,
        )


@pytest.mark.asyncio
async def test_set_visibility_not_found_raises() -> None:
    # Ownership check: entity not found for this tenant → NotFoundError.
    factory = _make_session_factory(entities=[])
    svc = VisibilityService(factory, _clock())
    with pytest.raises(NotFoundError):
        await svc.set_visibility(
            _ctx(_TENANT_A),
            uuid.uuid4(),
            VISIBILITY_PRIVATE,
        )


@pytest.mark.asyncio
async def test_set_visibility_private_writes_column() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PUBLIC)

    added_objects: list[Any] = []
    call_count = 0

    session = MagicMock()

    async def _execute(stmt: Any) -> Any:  # noqa: ANN401
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one_or_none.return_value = entity
        else:
            result.scalar_one_or_none.return_value = None  # no existing attr
        return result

    session.execute = _execute
    session.add = MagicMock(side_effect=added_objects.append)
    session.begin = MagicMock(return_value=_async_noop_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session

    svc = VisibilityService(factory, _clock())
    await svc.set_visibility(_ctx(_TENANT_A, _ACTOR_A), eid, VISIBILITY_PRIVATE)

    assert entity.visibility == VISIBILITY_PRIVATE
    # No shared_with_tenants attribute added for private (shared_with_tenants=None).
    assert added_objects == []


@pytest.mark.asyncio
async def test_set_visibility_tenant_shared_creates_attribute() -> None:
    eid = uuid.uuid4()
    entity = _entity(entity_id=eid, tenant_id=_TENANT_A, visibility=VISIBILITY_PRIVATE)

    added_objects: list[Any] = []
    call_count = 0

    session = MagicMock()

    async def _execute(stmt: Any) -> Any:  # noqa: ANN401
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one_or_none.return_value = entity
        else:
            result.scalar_one_or_none.return_value = None  # no existing attr
        return result

    session.execute = _execute
    session.add = MagicMock(side_effect=added_objects.append)
    session.begin = MagicMock(return_value=_async_noop_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session

    svc = VisibilityService(factory, _clock())
    await svc.set_visibility(
        _ctx(_TENANT_A, _ACTOR_A),
        eid,
        VISIBILITY_TENANT_SHARED,
        shared_with_tenants=[_TENANT_B],
    )

    assert entity.visibility == VISIBILITY_TENANT_SHARED
    assert len(added_objects) == 1
    attr = added_objects[0]
    assert isinstance(attr, Attribute)
    assert attr.key == "shared_with_tenants"
    assert str(_TENANT_B) in attr.value
