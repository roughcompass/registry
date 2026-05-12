"""Unit tests for registry.service.identity.resolve_whoami.

All interactions with Actor, Tenant, and ApiToken are mocked — no database
required.  The tests verify that:

  - resolve_whoami returns a WhoamiPayload with all nine fields populated
    from the three ORM rows.
  - Missing actor row → actor_display_name / actor_email are None.
  - Missing tenant row → tenant_slug / tenant_display_name are empty strings.
  - Missing token row → token_id / token_expires_at are None.
  - roles are taken from the TenantContext, not from the ORM row.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.identity import WhoamiPayload, resolve_whoami
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Fixed instant — frozen clock for deterministic assertions
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 5, 11, 9, 0, 0, tzinfo=datetime.UTC)
_TENANT_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_ACTOR_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
_TOKEN_ID = uuid.UUID("cccccccc-0000-0000-0000-000000000003")
_TOKEN_EXPIRES = datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(roles: list[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT_ID,
        actor_id=_ACTOR_ID,
        roles=roles or ["producer"],
    )


def _scalar_result(value: Any) -> MagicMock:
    """Result whose .scalar_one_or_none() returns value."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _make_actor(*, display_name: str = "Alice", email: str = "alice@example.com") -> MagicMock:
    a = MagicMock()
    a.display_name = display_name
    a.email = email
    return a


def _make_tenant(*, slug: str = "acme", display_name: str = "Acme Corp") -> MagicMock:
    t = MagicMock()
    t.slug = slug
    t.display_name = display_name
    return t


def _make_token(
    *,
    token_id: uuid.UUID = _TOKEN_ID,
    expires_at: datetime.datetime | None = _TOKEN_EXPIRES,
) -> MagicMock:
    tok = MagicMock()
    tok.token_id = token_id
    tok.expires_at = expires_at
    return tok


def _make_session(scalar_returns: list[Any]) -> MagicMock:
    """Session mock whose execute() returns scalar_returns in sequence."""
    results = [_scalar_result(v) for v in scalar_returns]
    idx = 0

    async def _execute(*_a: Any, **_kw: Any) -> Any:
        nonlocal idx
        r = results[idx % len(results)]
        idx += 1
        return r

    session = MagicMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


def _make_factory(scalar_returns: list[Any]) -> MagicMock:
    session = _make_session(scalar_returns)
    return MagicMock(return_value=session)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_whoami_all_rows_present() -> None:
    """Happy path: all three rows exist → full payload."""
    actor = _make_actor()
    tenant = _make_tenant()
    token = _make_token()

    factory = _make_factory([actor, tenant, token])
    ctx = _ctx(roles=["producer", "admin"])

    result = await resolve_whoami(factory, ctx)

    assert isinstance(result, WhoamiPayload)
    assert result.tenant_id == _TENANT_ID
    assert result.tenant_slug == "acme"
    assert result.tenant_display_name == "Acme Corp"
    assert result.actor_id == _ACTOR_ID
    assert result.actor_display_name == "Alice"
    assert result.actor_email == "alice@example.com"
    assert result.token_id == _TOKEN_ID
    assert result.token_expires_at == _TOKEN_EXPIRES
    assert sorted(result.roles) == ["admin", "producer"]


@pytest.mark.asyncio
async def test_resolve_whoami_missing_actor() -> None:
    """No actor row → actor_display_name and actor_email are None."""
    tenant = _make_tenant()
    token = _make_token()

    factory = _make_factory([None, tenant, token])
    result = await resolve_whoami(factory, _ctx())

    assert result.actor_display_name is None
    assert result.actor_email is None
    assert result.tenant_slug == "acme"
    assert result.token_id == _TOKEN_ID


@pytest.mark.asyncio
async def test_resolve_whoami_missing_tenant() -> None:
    """No tenant row → slug and display_name fall back to empty string."""
    actor = _make_actor()
    token = _make_token()

    factory = _make_factory([actor, None, token])
    result = await resolve_whoami(factory, _ctx())

    assert result.tenant_slug == ""
    assert result.tenant_display_name == ""
    assert result.actor_display_name == "Alice"


@pytest.mark.asyncio
async def test_resolve_whoami_missing_token() -> None:
    """No active token row → token_id and token_expires_at are None."""
    actor = _make_actor()
    tenant = _make_tenant()

    factory = _make_factory([actor, tenant, None])
    result = await resolve_whoami(factory, _ctx())

    assert result.token_id is None
    assert result.token_expires_at is None


@pytest.mark.asyncio
async def test_resolve_whoami_token_no_expiry() -> None:
    """Token exists but expires_at is None (non-expiring token)."""
    actor = _make_actor()
    tenant = _make_tenant()
    token = _make_token(expires_at=None)

    factory = _make_factory([actor, tenant, token])
    result = await resolve_whoami(factory, _ctx())

    assert result.token_id == _TOKEN_ID
    assert result.token_expires_at is None


@pytest.mark.asyncio
async def test_resolve_whoami_roles_from_context() -> None:
    """Roles come from TenantContext, not from the Actor or ApiToken rows."""
    actor = _make_actor()
    tenant = _make_tenant()
    token = _make_token()

    factory = _make_factory([actor, tenant, token])
    ctx = _ctx(roles=["consumer"])

    result = await resolve_whoami(factory, ctx)
    assert result.roles == ["consumer"]


@pytest.mark.asyncio
async def test_resolve_whoami_all_missing() -> None:
    """All three rows absent — returns safe defaults for every nullable field."""
    factory = _make_factory([None, None, None])
    result = await resolve_whoami(factory, _ctx())

    assert result.tenant_slug == ""
    assert result.tenant_display_name == ""
    assert result.actor_display_name is None
    assert result.actor_email is None
    assert result.token_id is None
    assert result.token_expires_at is None
    # ids are always from ctx, never None
    assert result.tenant_id == _TENANT_ID
    assert result.actor_id == _ACTOR_ID
