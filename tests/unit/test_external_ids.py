"""Unit tests for ExternalIdService.

All tests are in-memory — no database.  Session factories are mocked to
return pre-canned result rows matching the SQL columns fetched by the service.

Coverage (per task contract):
  - register_external_system: happy path returns dict; duplicate slug → ConflictError.
  - add_external_id: URL template substitution; explicit url takes precedence;
    no template → url=None; duplicate (tenant, slug, ext_id) → ConflictError.
  - list_external_ids: returns list ordered by creation time.
  - lookup_by_external_id: found → EntityRef; not found → None.
  - delete_external_id: hard delete confirmed by subsequent list returning empty;
    wrong tenant / missing row → NotFoundError.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from registry.exceptions import ConflictError, NotFoundError, TenantIsolationError
from registry.service.external_ids import ExternalIdService
from registry.types import EntityRef, ExternalIdRef, FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 5, 10, 12, 0, 0, tzinfo=datetime.UTC)
_CLOCK = FakeClock(_NOW)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(*, tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
    )


def _async_noop_ctx() -> Any:
    """Async context manager that does nothing — used as session.begin() mock."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_session(execute_side_effects: list[Any]) -> MagicMock:
    """Build a mock session whose execute() returns the provided results in order.

    Each element of ``execute_side_effects`` is either:
    * a ``MagicMock`` result object returned normally, or
    * an ``Exception`` instance that will be raised.
    """
    effects = list(execute_side_effects)
    idx = 0

    async def _execute(*_a: Any, **_kw: Any) -> Any:
        nonlocal idx
        effect = effects[idx % len(effects)]
        idx += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect

    session = MagicMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=_async_noop_ctx())
    return session


def _make_factory(execute_side_effects: list[Any]) -> MagicMock:
    session = _make_session(execute_side_effects)
    return MagicMock(return_value=session)


def _insert_ok() -> MagicMock:
    r = MagicMock()
    r.rowcount = 1
    return r


def _first_result(row: Any) -> MagicMock:
    """Result object whose .first() returns ``row``."""
    r = MagicMock()
    r.first = MagicMock(return_value=row)
    return r


def _all_result(rows: list[Any]) -> MagicMock:
    """Result object whose .all() returns ``rows``."""
    r = MagicMock()
    r.all = MagicMock(return_value=rows)
    return r


def _scalar_result(value: Any) -> MagicMock:
    """Result object whose .scalar_one_or_none() returns ``value``."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _make_ext_row(
    *,
    external_id_pk: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    external_system_slug: str = "jira",
    external_id: str = "PROJ-1",
    url: str | None = None,
    metadata_jsonb: dict[str, Any] | None = None,
    created_at: datetime.datetime = _NOW,
    updated_at: datetime.datetime = _NOW,
) -> MagicMock:
    row = MagicMock()
    row.external_id_pk = external_id_pk or uuid.uuid4()
    row.entity_id = entity_id or uuid.uuid4()
    row.tenant_id = tenant_id or uuid.uuid4()
    row.external_system_slug = external_system_slug
    row.external_id = external_id
    row.url = url
    row.metadata_jsonb = metadata_jsonb
    row.created_at = created_at
    row.updated_at = updated_at
    return row


def _make_entity_row(
    *,
    entity_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    entity_type: str = "service",
    name: str = "My Service",
    external_id: str | None = None,
    is_active: bool = True,
    created_at: datetime.datetime = _NOW,
) -> MagicMock:
    row = MagicMock()
    row.entity_id = entity_id or uuid.uuid4()
    row.tenant_id = tenant_id or uuid.uuid4()
    row.entity_type = entity_type
    row.name = name
    row.external_id = external_id
    row.is_active = is_active
    row.created_at = created_at
    return row


def _make_sys_row(*, url_template: str | None = None) -> MagicMock:
    row = MagicMock()
    row.url_template = url_template
    return row


# ---------------------------------------------------------------------------
# register_external_system
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_external_system_happy_path() -> None:
    factory = _make_factory([_insert_ok()])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    result = await svc.register_external_system(
        ctx,
        slug="github",
        display_name="GitHub",
        url_template="https://github.com/issues/{external_id}",
        description="GitHub Issues",
    )

    assert result["slug"] == "github"
    assert result["tenant_id"] == ctx.tenant_id
    assert result["display_name"] == "GitHub"
    assert result["url_template"] == "https://github.com/issues/{external_id}"
    assert result["description"] == "GitHub Issues"
    assert isinstance(result["created_at"], datetime.datetime)


@pytest.mark.asyncio
async def test_register_external_system_duplicate_slug_raises_conflict() -> None:
    # Simulate DB uniqueness violation on (tenant_id, slug).
    factory = _make_factory([IntegrityError("duplicate", params={}, orig=Exception())])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    with pytest.raises(ConflictError, match="already exists"):
        await svc.register_external_system(ctx, slug="github", display_name="GitHub")


@pytest.mark.asyncio
async def test_register_external_system_no_template() -> None:
    factory = _make_factory([_insert_ok()])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    result = await svc.register_external_system(ctx, slug="backstage", display_name="Backstage")

    assert result["url_template"] is None
    assert result["description"] is None


# ---------------------------------------------------------------------------
# add_external_id — URL template substitution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_external_id_url_template_substituted() -> None:
    """URL from url_template.replace('{external_id}', external_id) is stored."""
    tid = uuid.uuid4()
    eid = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=tid)
    sys_row = _make_sys_row(url_template="https://jira.example.com/browse/{external_id}")

    factory = _make_factory(
        [
            _first_result(entity_row),  # entity ownership check
            _first_result(sys_row),  # external_systems lookup (url_template)
            _insert_ok(),  # entity_external_ids INSERT
        ]
    )
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    ref = await svc.add_external_id(ctx, eid, "jira", "PROJ-42")

    assert ref.url == "https://jira.example.com/browse/PROJ-42"
    assert ref.external_id == "PROJ-42"
    assert ref.external_system_slug == "jira"
    assert ref.entity_id == eid
    assert ref.tenant_id == tid


@pytest.mark.asyncio
async def test_add_external_id_explicit_url_overrides_template() -> None:
    """Explicit url argument takes precedence over the system url_template."""
    tid = uuid.uuid4()
    eid = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=tid)
    sys_exists = _first_result(MagicMock())  # just needs to be non-None

    factory = _make_factory(
        [
            _first_result(entity_row),  # entity check
            sys_exists,  # system existence check (explicit url path)
            _insert_ok(),
        ]
    )
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    explicit_url = "https://custom.example.com/PROJ-42"
    ref = await svc.add_external_id(ctx, eid, "jira", "PROJ-42", url=explicit_url)

    assert ref.url == explicit_url


@pytest.mark.asyncio
async def test_add_external_id_no_template_no_url_gives_none() -> None:
    """When system has no url_template and caller passes no url, url is None."""
    tid = uuid.uuid4()
    eid = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=tid)
    sys_row = _make_sys_row(url_template=None)

    factory = _make_factory(
        [
            _first_result(entity_row),
            _first_result(sys_row),
            _insert_ok(),
        ]
    )
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    ref = await svc.add_external_id(ctx, eid, "backstage", "component:default/my-svc")

    assert ref.url is None


@pytest.mark.asyncio
async def test_add_external_id_duplicate_raises_conflict() -> None:
    """Duplicate (tenant_id, external_system_slug, external_id) → ConflictError."""
    tid = uuid.uuid4()
    eid = uuid.uuid4()
    existing_pk = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=tid)
    sys_row = _make_sys_row(url_template=None)

    # INSERT raises IntegrityError; service then fetches existing PK.
    existing_pk_result = _first_result(MagicMock(external_id_pk=existing_pk))

    factory = _make_factory(
        [
            _first_result(entity_row),  # entity check
            _first_result(sys_row),  # system lookup
            IntegrityError("unique violation", params={}, orig=Exception()),  # INSERT fails
            existing_pk_result,  # fetch existing PK
        ]
    )
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    with pytest.raises(ConflictError, match=str(existing_pk)):
        await svc.add_external_id(ctx, eid, "jira", "PROJ-1")


@pytest.mark.asyncio
async def test_add_external_id_entity_not_found() -> None:
    factory = _make_factory([_first_result(None)])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    with pytest.raises(NotFoundError, match="not found"):
        await svc.add_external_id(ctx, uuid.uuid4(), "jira", "PROJ-1")


@pytest.mark.asyncio
async def test_add_external_id_tenant_isolation() -> None:
    """Entity belonging to a different tenant → TenantIsolationError."""
    tid = uuid.uuid4()
    other_tid = uuid.uuid4()
    eid = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=other_tid)
    factory = _make_factory([_first_result(entity_row)])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    with pytest.raises(TenantIsolationError):
        await svc.add_external_id(ctx, eid, "jira", "PROJ-1")


@pytest.mark.asyncio
async def test_add_external_id_external_system_not_found() -> None:
    """Unregistered external system slug → NotFoundError."""
    tid = uuid.uuid4()
    eid = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=tid)
    sys_missing = _first_result(None)  # url-resolution path: system not found

    factory = _make_factory([_first_result(entity_row), sys_missing])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    with pytest.raises(NotFoundError, match="not registered"):
        await svc.add_external_id(ctx, eid, "ghost_system", "EXT-1")


# ---------------------------------------------------------------------------
# list_external_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_external_ids_returns_ordered_refs() -> None:
    tid = uuid.uuid4()
    eid = uuid.uuid4()

    rows = [
        _make_ext_row(
            entity_id=eid,
            tenant_id=tid,
            external_id="PROJ-1",
            created_at=_NOW,
        ),
        _make_ext_row(
            entity_id=eid,
            tenant_id=tid,
            external_id="PROJ-2",
            created_at=_NOW + datetime.timedelta(seconds=1),
        ),
    ]

    factory = _make_factory([_all_result(rows)])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    result = await svc.list_external_ids(ctx, eid)

    assert len(result) == 2
    assert all(isinstance(r, ExternalIdRef) for r in result)
    assert result[0].external_id == "PROJ-1"
    assert result[1].external_id == "PROJ-2"


@pytest.mark.asyncio
async def test_list_external_ids_empty() -> None:
    factory = _make_factory([_all_result([])])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    result = await svc.list_external_ids(ctx, uuid.uuid4())

    assert result == []


# ---------------------------------------------------------------------------
# lookup_by_external_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_by_external_id_found() -> None:
    tid = uuid.uuid4()
    eid = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=tid, name="Orders API")
    factory = _make_factory([_first_result(entity_row)])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    result = await svc.lookup_by_external_id(ctx, "jira", "PROJ-42")

    assert isinstance(result, EntityRef)
    assert result.entity_id == eid
    assert result.name == "Orders API"


@pytest.mark.asyncio
async def test_lookup_by_external_id_not_found() -> None:
    factory = _make_factory([_first_result(None)])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    result = await svc.lookup_by_external_id(ctx, "jira", "UNKNOWN-999")

    assert result is None


# ---------------------------------------------------------------------------
# delete_external_id — hard delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_external_id_removes_row() -> None:
    """Hard delete confirmed: subsequent list_external_ids returns empty."""
    tid = uuid.uuid4()
    eid = uuid.uuid4()
    pk = uuid.uuid4()

    snap_row = MagicMock()
    snap_row.external_system_slug = "jira"
    snap_row.external_id = "PROJ-1"
    snap_row.entity_id = eid

    # delete_external_id sequence: ownership check, then DELETE.
    # Audit is written via audit.emit() in its own session (separate call to
    # session_factory); emit() uses session.add(), not session.execute(), so it
    # does not consume a slot in the execute mock.
    delete_factory = _make_factory(
        [
            _first_result(snap_row),  # ownership / snapshot query
            _insert_ok(),  # DELETE statement result
        ]
    )
    svc = ExternalIdService(delete_factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    # Should not raise.
    await svc.delete_external_id(ctx, pk)

    # Simulate a subsequent list returning empty (separate factory).
    list_factory = _make_factory([_all_result([])])
    svc2 = ExternalIdService(list_factory, _CLOCK)
    result = await svc2.list_external_ids(ctx, eid)
    assert result == []


@pytest.mark.asyncio
async def test_delete_external_id_not_found_raises() -> None:
    """Row missing or belonging to another tenant → NotFoundError."""
    factory = _make_factory([_first_result(None)])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    with pytest.raises(NotFoundError, match="not found"):
        await svc.delete_external_id(ctx, uuid.uuid4())


@pytest.mark.asyncio
async def test_delete_external_id_calls_audit_emit() -> None:
    """delete_external_id delegates audit writes to audit.emit() and passes the snapshot."""
    tid = uuid.uuid4()
    pk = uuid.uuid4()
    eid = uuid.uuid4()

    snap_row = MagicMock()
    snap_row.external_system_slug = "jira"
    snap_row.external_id = "PROJ-1"
    snap_row.entity_id = eid

    factory = _make_factory(
        [
            _first_result(snap_row),  # ownership check
            _insert_ok(),  # DELETE statement
        ]
    )
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx(tenant_id=tid)

    with patch("registry.service.external_ids.audit_emit.emit", new=AsyncMock()) as mock_emit:
        await svc.delete_external_id(ctx, pk)

    mock_emit.assert_awaited_once()
    _, call_kwargs = mock_emit.call_args
    assert call_kwargs["action"] == "delete"
    assert call_kwargs["target_type"] == "entity_external_id"
    assert call_kwargs["target_id"] == pk
    assert call_kwargs["after"]["external_id"] == "PROJ-1"
    assert call_kwargs["after"]["entity_id"] == str(eid)


# ---------------------------------------------------------------------------
# list_external_systems / delete_external_system
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_external_systems_empty() -> None:
    factory = _make_factory([_all_result([])])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    result = await svc.list_external_systems(ctx)

    assert result == []


@pytest.mark.asyncio
async def test_delete_external_system_not_found_raises() -> None:
    delete_result = MagicMock()
    delete_result.rowcount = 0
    factory = _make_factory([delete_result])
    svc = ExternalIdService(factory, _CLOCK)
    ctx = _ctx()

    with pytest.raises(NotFoundError, match="not found"):
        await svc.delete_external_system(ctx, "ghost")


# ---------------------------------------------------------------------------
# Frozen-clock timestamp determinism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_external_id_uses_frozen_clock() -> None:
    """Timestamps on the returned ExternalIdRef reflect the injected clock, not wall time."""
    frozen_ts = datetime.datetime(2030, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
    clock = FakeClock(frozen_ts)

    tid = uuid.uuid4()
    eid = uuid.uuid4()

    entity_row = _make_entity_row(entity_id=eid, tenant_id=tid)
    sys_row = _make_sys_row(url_template=None)

    factory = _make_factory(
        [
            _first_result(entity_row),  # entity ownership check
            _first_result(sys_row),  # external_systems lookup
            _insert_ok(),  # entity_external_ids INSERT
        ]
    )
    svc = ExternalIdService(factory, clock)
    ctx = _ctx(tenant_id=tid)

    ref = await svc.add_external_id(ctx, eid, "backstage", "component:default/svc")

    assert ref.created_at == frozen_ts
    assert ref.updated_at == frozen_ts


@pytest.mark.asyncio
async def test_register_external_system_uses_frozen_clock() -> None:
    """register_external_system created_at reflects the injected clock."""
    frozen_ts = datetime.datetime(2030, 6, 15, 8, 30, 0, tzinfo=datetime.UTC)
    clock = FakeClock(frozen_ts)

    factory = _make_factory([_insert_ok()])
    svc = ExternalIdService(factory, clock)
    ctx = _ctx()

    result = await svc.register_external_system(ctx, slug="gh", display_name="GitHub")

    assert result["created_at"] == frozen_ts


@pytest.mark.asyncio
async def test_update_external_id_uses_frozen_clock() -> None:
    """update_external_id updated_at reflects the injected clock."""
    frozen_ts = datetime.datetime(2031, 3, 10, 12, 0, 0, tzinfo=datetime.UTC)
    clock = FakeClock(frozen_ts)

    tid = uuid.uuid4()
    eid = uuid.uuid4()
    pk = uuid.uuid4()

    existing_row = _make_ext_row(
        external_id_pk=pk,
        entity_id=eid,
        tenant_id=tid,
        url="https://old.example.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    existing_row.external_system_slug = "jira"
    existing_row.external_id = "PROJ-1"
    existing_row.metadata_jsonb = None

    factory = _make_factory(
        [
            _first_result(existing_row),  # fetch current row
            _insert_ok(),  # UPDATE statement
        ]
    )
    svc = ExternalIdService(factory, clock)
    ctx = _ctx(tenant_id=tid)

    ref = await svc.update_external_id(ctx, eid, pk, url="https://new.example.com")

    assert ref.updated_at == frozen_ts
    assert ref.created_at == _NOW  # created_at comes from the DB row, unchanged
