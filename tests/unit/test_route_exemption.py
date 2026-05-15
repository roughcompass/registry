"""Unit tests for the three-class route exemption policy.

Three classes per the auth ADR:

- **Public** (no JWT): ``/healthz``, ``/readyz``, ``/metrics`` —
  reachable without an Authorization header. Probes don't carry
  bearers; metrics scrapers either run on a private network or
  authenticate via a separate mechanism (TLS cert, network ACL).
- **Authenticated but tenantless** (JWT required, no X-Tenant-ID):
  ``/v1/whoami``, ``/v1/capabilities``, ``/openapi.json`` — caller is
  identified but the response is tenant-agnostic. Uses
  ``get_authenticated_context`` (returns ``TenantContext`` with empty
  selected tenant; populated ``tenant_memberships`` for caller
  introspection).
- **Fully tenant-scoped**: every other route. Uses
  ``get_tenant_context`` (full pipeline including X-Tenant-ID
  selection).

These tests verify:

1. The two dependency variants are wired and importable from the
   middleware module.
2. ``get_authenticated_context`` returns a TenantContext whose
   ``actor_id`` is the nil sentinel and whose ``tenant_memberships`` is
   the full list — handlers that opt into the tenantless flow can
   render multi-tenant UIs without selecting one.
3. The middleware module's public surface remains compatible with both
   dependency callsites.

Note: end-to-end tests that wire dependencies onto real route handlers
live in the integration suite (test_entitlement_auth_flow). This file
exercises the dependencies in isolation against a stub Request.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from registry.api.middleware import tenant as middleware
from registry.api.middleware.tenant import (
    get_authenticated_context,
    get_tenant_context,
)
from registry.auth.entitlements import client as entitlement_client
from registry.auth.resolver import AuditIdentity, ResolvedIdentity, TenantGrant


# Shared helpers — same shape used in test_entitlement_middleware.py.


def _make_request(
    *, authorization: str | None = "Bearer dummy.jwt"
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))

    settings = MagicMock()
    settings.oidc_discovery_url = "https://idp.example.com/.well-known/openid-configuration"
    app = MagicMock()
    app.state.settings = settings
    app.state.oidc_cache = MagicMock()
    return Request({"type": "http", "headers": headers, "app": app})


def _grant(slug: str = "111", role: str = "admin") -> TenantGrant:
    return TenantGrant(
        tenant_id=uuid.uuid4(), tenant_external_id=slug, catalog_role=role
    )


def _resolved(grants: list[TenantGrant]) -> ResolvedIdentity:
    return ResolvedIdentity(
        user_id="user-1",
        tenant_grants=grants,
        audit_identity=AuditIdentity(
            sub="user-1", email=None, preferred_username="user-1"
        ),
    )


def _patch_validator_and_resolver(
    request: Request, *, grants: list[TenantGrant]
):
    """Combined patch — applies both validator and resolver mocks. Use as
    ``with`` body."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        validator = AsyncMock(return_value=({"sub": "user-1"}, "user-1"))
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_resolved(grants))
        request.app.state.claim_resolver = resolver
        with patch.object(middleware, "validate_oidc_token", validator):
            yield validator, resolver

    return _ctx()


class TestDependencySurface:
    """Both dependency variants must be importable and callable from the
    middleware module — they're how routes opt into the right policy."""

    def test_get_tenant_context_is_exported(self):
        assert callable(get_tenant_context)
        assert get_tenant_context is middleware.get_tenant_context

    def test_get_authenticated_context_is_exported(self):
        assert callable(get_authenticated_context)
        assert get_authenticated_context is middleware.get_authenticated_context


@pytest.mark.asyncio
class TestTenantlessVariant:
    """``get_authenticated_context`` returns a TenantContext that
    handlers can use without committing to any single tenant."""

    async def test_returns_tenantless_context_with_full_memberships(self):
        request = _make_request()
        a = _grant("111", "admin")
        b = _grant("222", "consumer")
        with _patch_validator_and_resolver(request, grants=[a, b]):
            tc = await get_authenticated_context(request)
        # Caller is identified — oidc_subject is set.
        assert tc.oidc_subject == "user-1"
        # No selected tenant.
        assert tc.tenant_id == uuid.UUID(int=0)
        assert tc.actor_id == uuid.UUID(int=0)
        assert tc.roles == []
        # But the full membership list is available so the handler can
        # render multi-tenant UIs without picking one.
        assert {m.tenant_slug for m in tc.tenant_memberships} == {"111", "222"}

    async def test_no_x_tenant_id_required_for_multi_tenant(self):
        """The whole point of the tenantless variant: a multi-tenant
        caller hits this dependency without sending X-Tenant-ID and gets
        a 200, not a 400 like get_tenant_context would emit."""
        request = _make_request()  # no X-Tenant-ID header
        with _patch_validator_and_resolver(
            request, grants=[_grant("a"), _grant("b")]
        ):
            tc = await get_authenticated_context(request)
        assert len(tc.tenant_memberships) == 2

    async def test_missing_bearer_still_yields_401(self):
        """Tenantless != public — the JWT is still required."""
        request = _make_request(authorization=None)
        with pytest.raises(HTTPException) as exc:
            await get_authenticated_context(request)
        assert exc.value.status_code == 401

    async def test_resolver_failures_still_propagate(self):
        request = _make_request()
        # Mock the resolver to raise an upstream auth error.
        validator = AsyncMock(return_value=({"sub": "user-1"}, "user-1"))
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            side_effect=entitlement_client.EntitlementAuthError(401)
        )
        request.app.state.claim_resolver = resolver
        with patch.object(middleware, "validate_oidc_token", validator):
            with pytest.raises(HTTPException) as exc:
                await get_authenticated_context(request)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
class TestTenantScopedVariant:
    """``get_tenant_context`` enforces tenant selection — a multi-tenant
    caller without X-Tenant-ID is a 400."""

    async def test_multi_tenant_no_header_is_400(self):
        request = _make_request()  # no X-Tenant-ID
        a = _grant("111", "admin")
        b = _grant("222", "consumer")
        from sqlalchemy.ext.asyncio import AsyncSession
        session = MagicMock(spec=AsyncSession)
        with _patch_validator_and_resolver(request, grants=[a, b]):
            with pytest.raises(HTTPException) as exc:
                await get_tenant_context(request, session)
        assert exc.value.status_code == 400


class TestPublicRoutesAreUnchanged:
    """The public-route prefixes registered in main.py are documentation
    of the policy. This test pins the list so a future contributor
    cannot accidentally remove ``/healthz`` from the public set without
    a deliberate code change in this file too."""

    def test_public_path_prefixes_documented(self):
        from registry.main import _PUBLIC_PATH_PREFIXES

        # The four public-by-policy prefixes. Adding to or removing from
        # this set is a deliberate policy change.
        assert "/healthz" in _PUBLIC_PATH_PREFIXES
        assert "/readyz" in _PUBLIC_PATH_PREFIXES
        assert "/metrics" in _PUBLIC_PATH_PREFIXES
        # /webhooks is public-but-HMAC-verified — the receiver enforces
        # its own auth via signature, so it doesn't take a Bearer.
        assert "/webhooks" in _PUBLIC_PATH_PREFIXES
