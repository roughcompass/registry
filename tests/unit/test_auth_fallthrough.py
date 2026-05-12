"""Unit tests for the OIDC → bearer fallthrough in get_tenant_context.

The middleware behaviour under test:
- A token without three dot-separated parts skips OIDC entirely and goes
  straight to API-token validation.
- A JWT-shaped token with OIDC configured tries OIDC first; on
  CatalogError it falls through to API-token validation.
- A JWT-shaped token without OIDC configured skips OIDC entirely.

Each test mocks both validators so no DB and no IdP discovery are
required.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from registry.api.middleware.tenant import get_tenant_context
from registry.exceptions import CatalogError
from registry.types import FakeClock, TenantContext

_FIXED_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _make_request(*, authorization: str, oidc_url: str | None) -> Request:
    """Build a Request stub carrying an Authorization header and settings."""
    settings = MagicMock()
    settings.oidc_discovery_url = oidc_url
    app = MagicMock()
    app.state.settings = settings
    scope = {
        "type": "http",
        "headers": [(b"authorization", authorization.encode())],
        "app": app,
    }
    return Request(scope)


def _tenant_context() -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["producer"],
    )


@pytest.mark.asyncio
async def test_opaque_token_skips_oidc_path() -> None:
    """A token with no dots is never sent through validate_oidc_token."""
    ctx = _tenant_context()
    request = _make_request(
        authorization="Bearer opaque-token-no-dots",
        oidc_url="https://auth.example.com/.well-known/openid-configuration",
    )
    session = AsyncMock()
    clock = FakeClock(_FIXED_NOW)

    with (
        patch("catalog.api.middleware.tenant.validate_token", AsyncMock(return_value=ctx)) as v_api,
        patch("catalog.api.auth.oidc.validate_oidc_token", AsyncMock()) as v_oidc,
    ):
        out = await get_tenant_context(request, session, clock)

    assert out is ctx
    v_api.assert_awaited_once()
    v_oidc.assert_not_awaited()


@pytest.mark.asyncio
async def test_jwt_shaped_token_falls_through_on_oidc_failure() -> None:
    """JWT-shaped token + OIDC configured: try OIDC, then fall through to api-token."""
    ctx = _tenant_context()
    request = _make_request(
        authorization="Bearer header.payload.signature",
        oidc_url="https://auth.example.com/.well-known/openid-configuration",
    )
    session = AsyncMock()
    clock = FakeClock(_FIXED_NOW)

    with (
        patch(
            "catalog.api.auth.oidc.validate_oidc_token",
            AsyncMock(side_effect=CatalogError("invalid OIDC token: stub")),
        ) as v_oidc,
        patch("catalog.api.middleware.tenant.validate_token", AsyncMock(return_value=ctx)) as v_api,
    ):
        out = await get_tenant_context(request, session, clock)

    assert out is ctx
    v_oidc.assert_awaited_once()
    v_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_jwt_shaped_token_skips_oidc_when_not_configured() -> None:
    """No OIDC_DISCOVERY_URL → OIDC path not attempted, even for JWT-shaped tokens."""
    ctx = _tenant_context()
    request = _make_request(
        authorization="Bearer header.payload.signature",
        oidc_url=None,
    )
    session = AsyncMock()
    clock = FakeClock(_FIXED_NOW)

    with (
        patch("catalog.api.middleware.tenant.validate_token", AsyncMock(return_value=ctx)) as v_api,
        patch("catalog.api.auth.oidc.validate_oidc_token", AsyncMock()) as v_oidc,
    ):
        out = await get_tenant_context(request, session, clock)

    assert out is ctx
    v_oidc.assert_not_awaited()
    v_api.assert_awaited_once()
