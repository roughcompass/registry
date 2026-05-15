"""Unit tests for the entitlement middleware pipeline.

Covers the bearer extraction, JWT validation handoff, resolver
invocation + typed-error → HTTP-status mapping, the X-Tenant-ID
selection rules (single + multi tenant), and the tenantless variant
``get_authenticated_context``.

The OIDC validator and the resolver are both mocked — the middleware
under test is the orchestration layer that sits between them.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from registry.api.middleware import tenant as middleware
from registry.api.middleware.tenant import (
    _bearer_token,
    _select_tenant_grant,
    get_authenticated_context,
    get_tenant_context,
)
from registry.auth.entitlements import client as entitlement_client
from registry.auth.entitlements.actor_store import DisabledTenantError
from registry.auth.resolver import AuditIdentity, ResolvedIdentity, TenantGrant
from registry.exceptions import CatalogError


# ---------------------------------------------------------------------------
# Test scaffolding


def _make_request(
    *,
    authorization: str | None = "Bearer dummy.jwt.token",
    x_tenant_id: str | None = None,
    request_id: str | None = None,
) -> Request:
    """Build a minimal ASGI request with optional auth + tenant headers."""
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if x_tenant_id is not None:
        headers.append((b"x-tenant-id", x_tenant_id.encode()))
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode()))

    settings = MagicMock()
    settings.oidc_discovery_url = "https://idp.example.com/.well-known/openid-configuration"

    app = MagicMock()
    app.state.settings = settings
    app.state.oidc_cache = MagicMock()

    scope = {"type": "http", "headers": headers, "app": app}
    return Request(scope)


def _grant(tenant_external_id: str = "111", role: str = "admin") -> TenantGrant:
    return TenantGrant(
        tenant_id=uuid.uuid4(),
        tenant_external_id=tenant_external_id,
        catalog_role=role,
    )


def _resolved_identity(
    grants: list[TenantGrant] | None = None,
    *,
    user_id: str = "user-1",
) -> ResolvedIdentity:
    return ResolvedIdentity(
        user_id=user_id,
        tenant_grants=grants if grants is not None else [],
        audit_identity=AuditIdentity(
            sub=user_id, email=None, preferred_username=user_id
        ),
    )


def _patch_validator_and_resolver(
    *,
    validator_returns: tuple[dict, str] | None = None,
    validator_raises: Exception | None = None,
    resolver_returns: ResolvedIdentity | None = None,
    resolver_raises: Exception | None = None,
):
    """Combined patch context that mocks both the OIDC validator and the
    request.app.state.claim_resolver. Use as ``with`` body.

    Returns a tuple of (validator_mock, resolver_mock) so tests can
    assert on call args / counts.
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx(request: Request):
        validator = AsyncMock()
        if validator_raises is not None:
            validator.side_effect = validator_raises
        else:
            validator.return_value = validator_returns or ({"sub": "user-1"}, "user-1")

        resolver = MagicMock()
        resolver.resolve = AsyncMock()
        if resolver_raises is not None:
            resolver.resolve.side_effect = resolver_raises
        else:
            resolver.resolve.return_value = resolver_returns or _resolved_identity()

        request.app.state.claim_resolver = resolver

        with patch.object(middleware, "validate_oidc_token", validator):
            yield validator, resolver

    return _ctx


def _session_mock() -> AsyncSession:
    return MagicMock(spec=AsyncSession)


# ---------------------------------------------------------------------------
# Bearer extraction


class TestBearerExtraction:
    def test_missing_authorization_header_raises_401(self):
        request = _make_request(authorization=None)
        with pytest.raises(HTTPException) as exc:
            _bearer_token(request)
        assert exc.value.status_code == 401

    def test_non_bearer_scheme_raises_401(self):
        request = _make_request(authorization="Basic abc123")
        with pytest.raises(HTTPException) as exc:
            _bearer_token(request)
        assert exc.value.status_code == 401

    def test_empty_bearer_value_raises_401(self):
        request = _make_request(authorization="Bearer ")
        with pytest.raises(HTTPException) as exc:
            _bearer_token(request)
        assert exc.value.status_code == 401

    def test_well_formed_bearer_returns_token(self):
        request = _make_request(authorization="Bearer my.jwt.token")
        assert _bearer_token(request) == "my.jwt.token"


# ---------------------------------------------------------------------------
# JWT validation failure


@pytest.mark.asyncio
class TestValidatorFailures:
    async def test_oidc_catalog_error_yields_401(self):
        request = _make_request()
        ctx = _patch_validator_and_resolver(
            validator_raises=CatalogError("missing-iat")
        )
        with ctx(request) as (_validator, _resolver):
            with pytest.raises(HTTPException) as exc:
                await get_tenant_context(request, _session_mock())
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Empty grants → 403


@pytest.mark.asyncio
class TestEmptyGrants:
    async def test_no_grants_returns_403(self):
        request = _make_request()
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity(grants=[])
        )
        with ctx(request) as (_validator, _resolver):
            with pytest.raises(HTTPException) as exc:
                await get_tenant_context(request, _session_mock())
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Single-tenant selection


@pytest.mark.asyncio
class TestSingleTenant:
    async def test_no_header_auto_selects(self):
        request = _make_request()
        only = _grant("111", "admin")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([only])
        )
        with ctx(request) as (_validator, _resolver):
            with patch.object(
                middleware, "upsert_entitlement_actor", AsyncMock(return_value=uuid.uuid4())
            ):
                tc = await get_tenant_context(request, _session_mock())
        assert tc.tenant_id == only.tenant_id
        assert tc.roles == ["admin"]
        assert len(tc.tenant_memberships) == 1

    async def test_matching_header_accepted(self):
        request = _make_request(x_tenant_id="111")
        only = _grant("111", "admin")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([only])
        )
        with ctx(request) as (_validator, _resolver):
            with patch.object(
                middleware, "upsert_entitlement_actor", AsyncMock(return_value=uuid.uuid4())
            ):
                tc = await get_tenant_context(request, _session_mock())
        assert tc.tenant_id == only.tenant_id

    async def test_non_matching_header_rejected(self):
        request = _make_request(x_tenant_id="999")
        only = _grant("111", "admin")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([only])
        )
        with ctx(request) as (_validator, _resolver):
            with pytest.raises(HTTPException) as exc:
                await get_tenant_context(request, _session_mock())
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Multi-tenant selection


@pytest.mark.asyncio
class TestMultiTenant:
    async def test_no_header_returns_400_with_available(self):
        request = _make_request()
        a = _grant("111", "admin")
        b = _grant("222", "consumer")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([a, b])
        )
        with ctx(request) as (_validator, _resolver):
            with pytest.raises(HTTPException) as exc:
                await get_tenant_context(request, _session_mock())
        assert exc.value.status_code == 400
        # The error body should list the available tenant identifiers so
        # the caller can fix the request without inspecting the JWT.
        assert "111" in exc.value.detail["available_tenants"]
        assert "222" in exc.value.detail["available_tenants"]

    async def test_matching_header_selects_tenant(self):
        request = _make_request(x_tenant_id="222")
        a = _grant("111", "admin")
        b = _grant("222", "consumer")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([a, b])
        )
        with ctx(request) as (_validator, _resolver):
            with patch.object(
                middleware, "upsert_entitlement_actor", AsyncMock(return_value=uuid.uuid4())
            ):
                tc = await get_tenant_context(request, _session_mock())
        assert tc.tenant_id == b.tenant_id
        assert tc.roles == ["consumer"]

    async def test_non_matching_header_rejected(self):
        request = _make_request(x_tenant_id="999")
        a = _grant("111", "admin")
        b = _grant("222", "consumer")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([a, b])
        )
        with ctx(request) as (_validator, _resolver):
            with pytest.raises(HTTPException) as exc:
                await get_tenant_context(request, _session_mock())
        assert exc.value.status_code == 403

    async def test_full_membership_list_carried_through(self):
        """Even after selecting one tenant, all memberships are exposed
        on the TenantContext so multi-tenant-aware code can iterate."""
        request = _make_request(x_tenant_id="111")
        a = _grant("111", "admin")
        b = _grant("222", "consumer")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([a, b])
        )
        with ctx(request) as (_validator, _resolver):
            with patch.object(
                middleware, "upsert_entitlement_actor", AsyncMock(return_value=uuid.uuid4())
            ):
                tc = await get_tenant_context(request, _session_mock())
        slugs = {m.tenant_slug for m in tc.tenant_memberships}
        assert slugs == {"111", "222"}


# ---------------------------------------------------------------------------
# Resolver-error → HTTP-status mapping (every row of the failure-mode table)


@pytest.mark.asyncio
class TestResolverErrorMapping:
    @pytest.mark.parametrize(
        "exc, expected_status",
        [
            (entitlement_client.EntitlementAuthError(401), 401),
            (entitlement_client.EntitlementAuthError(403), 403),
            (entitlement_client.EntitlementNotFoundError(), 403),
            (entitlement_client.EntitlementRateLimitError(), 503),
            (entitlement_client.EntitlementMalformedError("bad"), 503),
            (entitlement_client.EntitlementServiceError("upstream 503"), 503),
        ],
    )
    async def test_typed_errors_map_to_expected_status(self, exc, expected_status):
        request = _make_request()
        ctx = _patch_validator_and_resolver(resolver_raises=exc)
        with ctx(request) as (_validator, _resolver):
            with pytest.raises(HTTPException) as raised:
                await get_tenant_context(request, _session_mock())
        assert raised.value.status_code == expected_status


# ---------------------------------------------------------------------------
# DisabledTenantError race in step 8


@pytest.mark.asyncio
class TestDisabledTenantRace:
    async def test_disabled_tenant_during_actor_upsert_returns_403(self):
        """The resolver pre-filters disabled tenants, but a tenant could
        be disabled between the resolver's lookup and the middleware's
        actor upsert. That race must surface as 403, not crash."""
        request = _make_request()
        only = _grant("111", "admin")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([only])
        )
        with ctx(request) as (_validator, _resolver):
            with patch.object(
                middleware,
                "upsert_entitlement_actor",
                AsyncMock(side_effect=DisabledTenantError("111")),
            ):
                with pytest.raises(HTTPException) as exc:
                    await get_tenant_context(request, _session_mock())
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Tenantless context


@pytest.mark.asyncio
class TestAuthenticatedContext:
    async def test_returns_tenantless_context(self):
        request = _make_request()
        a = _grant("111", "admin")
        b = _grant("222", "consumer")
        ctx = _patch_validator_and_resolver(
            resolver_returns=_resolved_identity([a, b])
        )
        with ctx(request) as (_validator, _resolver):
            tc = await get_authenticated_context(request)
        # Tenantless: no tenant selection, but full membership list
        # is available for caller introspection.
        assert tc.tenant_id == uuid.UUID(int=0)
        assert len(tc.tenant_memberships) == 2
        assert tc.oidc_subject == "user-1"

    async def test_propagates_resolver_errors(self):
        request = _make_request()
        ctx = _patch_validator_and_resolver(
            resolver_raises=entitlement_client.EntitlementAuthError(401)
        )
        with ctx(request) as (_validator, _resolver):
            with pytest.raises(HTTPException) as exc:
                await get_authenticated_context(request)
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Pure _select_tenant_grant tests (without spinning up the full pipeline)


class TestSelectTenantGrantPure:
    def test_single_grant_no_header(self):
        request = _make_request()
        only = _grant("111", "admin")
        result = _select_tenant_grant(request, [only])
        assert result is only

    def test_multi_grant_no_header_400(self):
        request = _make_request()
        with pytest.raises(HTTPException) as exc:
            _select_tenant_grant(request, [_grant("a"), _grant("b")])
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Resolver receives request_id when present


@pytest.mark.asyncio
class TestRequestIdForwarding:
    async def test_x_request_id_forwarded_to_resolver_via_claims(self):
        request = _make_request(request_id="req-xyz")
        only = _grant("111", "admin")
        ctx = _patch_validator_and_resolver(
            validator_returns=({"sub": "user-1"}, "user-1"),
            resolver_returns=_resolved_identity([only]),
        )
        with ctx(request) as (_validator, resolver):
            with patch.object(
                middleware, "upsert_entitlement_actor", AsyncMock(return_value=uuid.uuid4())
            ):
                await get_tenant_context(request, _session_mock())
        # resolver.resolve was called with claims that include the
        # underscore-prefixed request_id forwarded from the header.
        called_claims = resolver.resolve.await_args.args[0]
        assert called_claims["__request_id"] == "req-xyz"
        assert called_claims["__raw_token"] == "dummy.jwt.token"
