"""Unit tests — require_roles() FastAPI dependency."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from registry.api.auth.context import VALID_ROLES, has_any_role, require_roles
from registry.types import TenantContext


def _ctx(roles: list[str]) -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=roles,
    )


# ---------------------------------------------------------------------------
# VALID_ROLES constant
# ---------------------------------------------------------------------------


def test_valid_roles_constant() -> None:
    assert VALID_ROLES == frozenset({"consumer", "producer", "admin", "auditor"})


# ---------------------------------------------------------------------------
# has_any_role helper
# ---------------------------------------------------------------------------


def test_has_any_role_match() -> None:
    assert has_any_role(_ctx(["admin"]), ["admin"]) is True


def test_has_any_role_no_match() -> None:
    assert has_any_role(_ctx(["consumer"]), ["admin"]) is False


def test_has_any_role_partial_match() -> None:
    assert has_any_role(_ctx(["consumer", "producer"]), ["admin", "producer"]) is True


# ---------------------------------------------------------------------------
# require_roles dependency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_roles_passes_when_role_present() -> None:
    ctx = _ctx(["admin"])
    dep = require_roles(["admin"])

    with patch("catalog.api.auth.context.get_tenant_context", AsyncMock(return_value=ctx)):
        result = await dep(ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_roles_raises_403_when_role_missing() -> None:
    ctx = _ctx(["consumer"])
    dep = require_roles(["admin"])

    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=ctx)

    assert exc_info.value.status_code == 403
    assert "admin" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_roles_any_of_satisfies() -> None:
    """At least one matching role is sufficient."""
    ctx = _ctx(["producer"])
    dep = require_roles(["admin", "producer"])

    result = await dep(ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_roles_empty_list_is_noop() -> None:
    """Empty required list: always passes."""
    ctx = _ctx([])
    dep = require_roles([])

    result = await dep(ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_roles_consumer_passes() -> None:
    ctx = _ctx(["consumer"])
    dep = require_roles(["consumer"])
    result = await dep(ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_roles_auditor_denied_for_admin_endpoint() -> None:
    ctx = _ctx(["auditor"])
    dep = require_roles(["admin"])

    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=ctx)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_roles_multiple_user_roles_one_matches() -> None:
    ctx = _ctx(["consumer", "auditor", "admin"])
    dep = require_roles(["admin"])
    result = await dep(ctx=ctx)
    assert result is ctx
