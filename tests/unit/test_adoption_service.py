"""Unit tests for AdoptionService.

DB interactions are mocked at the ``session.execute`` boundary; no
real Postgres is needed. Each test records the SQL strings the service
issues and asserts the right operations fire in the right order.

Coverage:
- Authorization gates: caller must be in consumer tenant; producer/admin role.
- Visibility chokepoint: ``assert_visible`` called before any write.
- ``adopt``: full flow inserts adoption_events + provides_to edge + invokes
  the auto-subscribe hook.
- ``unadopt``: soft-deletes via t_invalidated_at; idempotent on second call;
  authorization required.
- ``get_active_adoption``: returns row when present, None when absent.
- Validators ``_validate_intent`` and ``_validate_version_pin``.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import NotFoundError, ValidationError
from registry.service.adoption import (
    AdoptionService,
    _validate_intent,
    _validate_version_pin,
)
from registry.types import AdoptionEventRef, FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

_PROVIDER_TENANT = uuid.uuid4()
_CONSUMER_TENANT = uuid.uuid4()
_OTHER_TENANT = uuid.uuid4()
_CAP_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    tenant_id: uuid.UUID = _CONSUMER_TENANT,
    roles: list[str] | None = None,
    actor_id: uuid.UUID = _ACTOR_ID,
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=roles if roles is not None else ["producer"],
    )


def _async_noop_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _execute_recorder(
    *,
    provider_tenant_lookup: uuid.UUID | None = _PROVIDER_TENANT,
    adoption_unadopt_row: tuple[uuid.UUID, datetime.datetime | None] | None = None,
    active_adoption_row: dict[str, Any] | None = None,
):
    """Build an AsyncMock execute() that records calls and routes by SQL keyword.

    ``provider_tenant_lookup`` — value returned for the ``SELECT tenant_id
    FROM entities`` lookup in ``_lookup_provider_tenant``. Pass ``None`` to
    simulate a missing capability.

    ``adoption_unadopt_row`` — (consumer_tenant_id, t_invalidated_at) returned
    for the SELECT in ``unadopt``. Pass ``None`` to simulate a missing
    adoption row.

    ``active_adoption_row`` — mapping returned for ``get_active_adoption``.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(stmt)
        calls.append((sql, params or {}))
        result = MagicMock()

        if "FROM entities" in sql and "tenant_id" in sql.lower():
            if provider_tenant_lookup is None:
                result.first = MagicMock(return_value=None)
            else:
                row = MagicMock()
                row.tenant_id = provider_tenant_lookup
                result.first = MagicMock(return_value=row)
            return result

        if "FROM adoption_events" in sql and "t_invalidated_at" in sql and "SELECT" in sql:
            if active_adoption_row is not None:
                # get_active_adoption path (mappings().first())
                result.mappings.return_value.first.return_value = active_adoption_row
                return result
            if adoption_unadopt_row is not None:
                # unadopt path (.first())
                row = MagicMock()
                row.consumer_tenant_id = adoption_unadopt_row[0]
                row.t_invalidated_at = adoption_unadopt_row[1]
                result.first = MagicMock(return_value=row)
            else:
                result.first = MagicMock(return_value=None)
                result.mappings.return_value.first.return_value = None
            return result

        # Default: writes (INSERT / UPDATE) — return an empty result.
        result.first = MagicMock(return_value=None)
        return result

    return _execute, calls


def _make_session_factory(execute_fn):
    session = MagicMock()
    session.execute = execute_fn
    session.begin = MagicMock(return_value=_async_noop_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    return factory


def _make_visibility_stub(*, raises: BaseException | None = None) -> MagicMock:
    """Stub VisibilityService that records assert_visible calls."""
    vis = MagicMock()
    if raises is not None:
        vis.assert_visible = AsyncMock(side_effect=raises)
    else:
        vis.assert_visible = AsyncMock(return_value=None)
    return vis


def _make_service(
    execute_fn,
    *,
    visibility=None,
    auto_subscribe=None,
):
    return AdoptionService(
        session_factory=_make_session_factory(execute_fn),
        clock=FakeClock(_NOW),
        visibility=visibility or _make_visibility_stub(),
        auto_subscribe=auto_subscribe,
    )


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def test_validate_intent_none_ok() -> None:
    _validate_intent(None)


def test_validate_intent_string_ok() -> None:
    _validate_intent("we depend on this for billing reconciliation")


def test_validate_intent_too_long_raises() -> None:
    with pytest.raises(ValidationError):
        _validate_intent("x" * 1001)


def test_validate_intent_wrong_type_raises() -> None:
    with pytest.raises(ValidationError):
        _validate_intent(42)  # type: ignore[arg-type]


def test_validate_version_pin_none_ok() -> None:
    _validate_version_pin(None)


def test_validate_version_pin_ok() -> None:
    _validate_version_pin(">=2.0,<3.0")


def test_validate_version_pin_too_long_raises() -> None:
    with pytest.raises(ValidationError):
        _validate_version_pin("v" * 65)


def test_validate_version_pin_wrong_type_raises() -> None:
    with pytest.raises(ValidationError):
        _validate_version_pin(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_rejects_caller_in_different_tenant() -> None:
    execute_fn, _ = _execute_recorder()
    svc = _make_service(execute_fn)

    with pytest.raises(PermissionError, match="cannot adopt on behalf of"):
        await svc.adopt(
            ctx=_ctx(tenant_id=_OTHER_TENANT, roles=["producer"]),
            provider_capability_id=_CAP_ID,
            consumer_tenant_id=_CONSUMER_TENANT,
        )


@pytest.mark.asyncio
async def test_adopt_rejects_caller_without_role() -> None:
    execute_fn, _ = _execute_recorder()
    svc = _make_service(execute_fn)

    with pytest.raises(PermissionError, match="requires one of"):
        await svc.adopt(
            ctx=_ctx(roles=["consumer"]),  # consumer alone is insufficient
            provider_capability_id=_CAP_ID,
            consumer_tenant_id=_CONSUMER_TENANT,
        )


@pytest.mark.asyncio
async def test_adopt_calls_visibility_before_write() -> None:
    execute_fn, calls = _execute_recorder()
    vis = _make_visibility_stub(raises=PermissionError("invisible"))
    svc = _make_service(execute_fn, visibility=vis)

    with pytest.raises(PermissionError, match="invisible"):
        await svc.adopt(
            ctx=_ctx(roles=["producer"]),
            provider_capability_id=_CAP_ID,
            consumer_tenant_id=_CONSUMER_TENANT,
        )

    vis.assert_visible.assert_awaited_once()
    # No INSERT/UPDATE issued before the visibility raise.
    assert not any("INSERT INTO adoption_events" in sql or "INSERT INTO edges" in sql for sql, _ in calls)


# ---------------------------------------------------------------------------
# adopt() happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_inserts_event_and_provides_to_edge() -> None:
    execute_fn, calls = _execute_recorder()
    captured: dict[str, Any] = {}

    async def _capture_subscribe(*, session, ctx, adoption):
        captured["called"] = True
        captured["adoption_id"] = adoption.adoption_id

    svc = _make_service(execute_fn, auto_subscribe=_capture_subscribe)

    ref = await svc.adopt(
        ctx=_ctx(roles=["producer"]),
        provider_capability_id=_CAP_ID,
        consumer_tenant_id=_CONSUMER_TENANT,
        intent="depends-on-payment-api",
        version_pin=">=2.0,<3.0",
    )

    # Returned ref reflects what we wrote.
    assert isinstance(ref, AdoptionEventRef)
    assert ref.tenant_id == _PROVIDER_TENANT  # provider is the owner
    assert ref.consumer_tenant_id == _CONSUMER_TENANT
    assert ref.provider_capability_id == _CAP_ID
    assert ref.intent == "depends-on-payment-api"
    assert ref.version_pin == ">=2.0,<3.0"
    assert ref.actor_id == _ACTOR_ID
    assert ref.t_valid_from == _NOW
    assert ref.t_invalidated_at is None

    # Both writes fired.
    sqls = [s for s, _ in calls]
    assert any("INSERT INTO adoption_events" in s for s in sqls)
    assert any("INSERT INTO edges" in s and "provides_to" in s for s in sqls)

    # adoption_events comes before edges (transactional ordering).
    adopt_idx = next(i for i, s in enumerate(sqls) if "INSERT INTO adoption_events" in s)
    edge_idx = next(i for i, s in enumerate(sqls) if "INSERT INTO edges" in s)
    assert adopt_idx < edge_idx

    # Auto-subscribe hook called inside the transaction.
    assert captured.get("called") is True
    assert captured.get("adoption_id") == ref.adoption_id


@pytest.mark.asyncio
async def test_adopt_missing_provider_capability_raises() -> None:
    execute_fn, _ = _execute_recorder(provider_tenant_lookup=None)
    svc = _make_service(execute_fn)

    with pytest.raises(NotFoundError, match="provider capability"):
        await svc.adopt(
            ctx=_ctx(roles=["producer"]),
            provider_capability_id=_CAP_ID,
            consumer_tenant_id=_CONSUMER_TENANT,
        )


# ---------------------------------------------------------------------------
# unadopt()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unadopt_soft_deletes_with_t_invalidated_at() -> None:
    aid = uuid.uuid4()
    execute_fn, calls = _execute_recorder(
        adoption_unadopt_row=(_CONSUMER_TENANT, None),  # active row
    )
    svc = _make_service(execute_fn)

    await svc.unadopt(ctx=_ctx(roles=["admin"]), adoption_id=aid)

    sqls = [s for s, _ in calls]
    assert any("UPDATE adoption_events" in s and "t_invalidated_at" in s for s in sqls)


@pytest.mark.asyncio
async def test_unadopt_idempotent_on_already_invalidated() -> None:
    aid = uuid.uuid4()
    execute_fn, calls = _execute_recorder(
        adoption_unadopt_row=(_CONSUMER_TENANT, _NOW),  # already invalidated
    )
    svc = _make_service(execute_fn)

    await svc.unadopt(ctx=_ctx(roles=["admin"]), adoption_id=aid)

    # No UPDATE issued — already-invalidated path returns early.
    sqls = [s for s, _ in calls]
    assert not any("UPDATE adoption_events" in s for s in sqls)


@pytest.mark.asyncio
async def test_unadopt_missing_adoption_raises_notfound() -> None:
    execute_fn, _ = _execute_recorder(adoption_unadopt_row=None)
    svc = _make_service(execute_fn)

    with pytest.raises(NotFoundError, match="not found"):
        await svc.unadopt(ctx=_ctx(roles=["admin"]), adoption_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_unadopt_rejects_caller_in_wrong_tenant() -> None:
    aid = uuid.uuid4()
    execute_fn, _ = _execute_recorder(
        adoption_unadopt_row=(_CONSUMER_TENANT, None),
    )
    svc = _make_service(execute_fn)

    with pytest.raises(PermissionError):
        await svc.unadopt(
            ctx=_ctx(tenant_id=_OTHER_TENANT, roles=["admin"]),
            adoption_id=aid,
        )


# ---------------------------------------------------------------------------
# get_active_adoption()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_adoption_returns_none_when_absent() -> None:
    execute_fn, _ = _execute_recorder()
    svc = _make_service(execute_fn)

    ref = await svc.get_active_adoption(
        consumer_tenant_id=_CONSUMER_TENANT,
        provider_capability_id=_CAP_ID,
    )
    assert ref is None


@pytest.mark.asyncio
async def test_get_active_adoption_returns_ref_when_present() -> None:
    aid = uuid.uuid4()
    row = {
        "adoption_id": aid,
        "tenant_id": _PROVIDER_TENANT,
        "provider_capability_id": _CAP_ID,
        "consumer_tenant_id": _CONSUMER_TENANT,
        "actor_id": _ACTOR_ID,
        "intent": None,
        "version_pin": None,
        "t_valid_from": _NOW,
        "t_valid_to": None,
        "t_ingested_at": _NOW,
        "t_invalidated_at": None,
    }
    execute_fn, _ = _execute_recorder(active_adoption_row=row)
    svc = _make_service(execute_fn)

    ref = await svc.get_active_adoption(
        consumer_tenant_id=_CONSUMER_TENANT,
        provider_capability_id=_CAP_ID,
    )
    assert ref is not None
    assert ref.adoption_id == aid
    assert ref.tenant_id == _PROVIDER_TENANT
    assert ref.consumer_tenant_id == _CONSUMER_TENANT
