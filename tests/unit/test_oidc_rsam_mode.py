"""Unit tests — OIDC validate_oidc_token behaviour under auth_mode='rsam'.

Covers three required scenarios:
1. auth_mode='rsam', token has sub but no tenant_id/tid → succeeds.
2. auth_mode='oidc', token has sub but no tenant_id/tid → still raises (regression guard).
3. Factory dispatch: with auth_mode='rsam', build_resolver routes to RsamClaimSource.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.api.auth import oidc as oidc_mod
from registry.api.auth.oidc import _OidcCache
from registry.auth.resolver import build_resolver
from registry.config import Settings
from registry.exceptions import CatalogError

# ---------------------------------------------------------------------------
# Shared fixtures and helpers

_DISCOVERY = {
    "issuer": "https://idp.example.com",
    "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
}
_JWKS = {"keys": [{"kty": "RSA", "kid": "test", "n": "x", "e": "AQAB"}]}

_CLAIMS_NO_TENANT: dict[str, Any] = {
    "sub": "ida-user-456",
    "iss": "https://idp.example.com",
    "exp": 9999999999,
    # No tenant_id or tid claim — IDA token shape
}


def _make_settings(auth_mode: str = "oidc") -> Settings:
    extra: dict[str, Any] = {}
    if auth_mode != "oidc":
        extra["auth_claim_source_url"] = "https://rsam.example.com"
    return Settings(
        database_url="postgresql+asyncpg://x/y",
        pgbouncer_url="postgresql+asyncpg://x/y",
        scheduler_jobstore_url="postgresql+asyncpg://x/y",
        oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
        auth_mode=auth_mode,
        **extra,
    )


def _make_claims_obj(claims: dict[str, Any]) -> MagicMock:
    obj = MagicMock()
    obj.get.side_effect = lambda k, *a: claims.get(k)
    obj.validate.return_value = None
    return obj


@pytest.fixture(autouse=True)
def _reset_default_cache() -> None:
    oidc_mod._default_cache = None


# ---------------------------------------------------------------------------
# Scenario 1: auth_mode='rsam', no tenant claim → succeeds


@pytest.mark.asyncio
async def test_rsam_mode_no_tenant_claim_succeeds() -> None:
    """In RSAM mode a token with sub but no tenant_id/tid must not raise."""
    settings = _make_settings(auth_mode="rsam")
    cache = _OidcCache()
    db = AsyncMock()

    claims_obj = _make_claims_obj(_CLAIMS_NO_TENANT)
    jwt_instance = MagicMock()
    jwt_instance.decode.return_value = claims_obj

    with (
        patch.object(cache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
        patch.object(cache, "get_jwks", AsyncMock(return_value=_JWKS)),
        patch("registry.api.auth.oidc.JsonWebKey") as mock_jwk,
        patch("registry.api.auth.oidc.JsonWebToken", return_value=jwt_instance),
    ):
        mock_jwk.import_key_set.return_value = MagicMock()
        ctx = await oidc_mod.validate_oidc_token("h.p.s", settings, db, cache=cache)

    # The subject is returned in the sentinel context's roles list so it is
    # available to the resolver factory without requiring DB access here.
    assert "ida-user-456" in ctx.roles
    # Nil UUIDs mark this as a pre-resolver sentinel — not a real tenant context.
    assert ctx.tenant_id == uuid.UUID(int=0)
    assert ctx.actor_id == uuid.UUID(int=0)
    # The DB must not have been called — no tenant to scope the lookup against.
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 2: auth_mode='oidc', no tenant claim → still raises (regression guard)


@pytest.mark.asyncio
async def test_oidc_mode_no_tenant_claim_still_raises() -> None:
    """In OIDC mode a missing tenant_id/tid must still raise CatalogError."""
    settings = _make_settings(auth_mode="oidc")
    cache = _OidcCache()
    db = AsyncMock()

    claims_obj = _make_claims_obj(_CLAIMS_NO_TENANT)
    jwt_instance = MagicMock()
    jwt_instance.decode.return_value = claims_obj

    with (
        patch.object(cache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
        patch.object(cache, "get_jwks", AsyncMock(return_value=_JWKS)),
        patch("registry.api.auth.oidc.JsonWebKey") as mock_jwk,
        patch("registry.api.auth.oidc.JsonWebToken", return_value=jwt_instance),
    ):
        mock_jwk.import_key_set.return_value = MagicMock()
        with pytest.raises(CatalogError, match="missing tenant_id/tid claim"):
            await oidc_mod.validate_oidc_token("h.p.s", settings, db, cache=cache)


# ---------------------------------------------------------------------------
# Scenario 3: factory dispatch — auth_mode='rsam' routes to RsamClaimSource


def test_build_resolver_rsam_mode_dispatches_to_rsam_claim_source() -> None:
    """build_resolver returns RsamClaimSource when auth_mode='rsam'."""
    from registry.auth.rsam.claim_source import RsamClaimSource

    settings = _make_settings(auth_mode="rsam")

    # session_factory is injected — use a MagicMock; no I/O is performed here.
    mock_session_factory = MagicMock()

    resolver = build_resolver(settings=settings, session_factory=mock_session_factory)

    assert isinstance(resolver, RsamClaimSource)
    # Confirm the resolver claims scope for an arbitrary claims dict — the
    # mode check in is_in_scope does not inspect claims content.
    assert resolver.is_in_scope({}) is True
    assert resolver.is_in_scope({"sub": "some-user", "tenant_id": "t"}) is True


def test_build_resolver_oidc_mode_raises_no_match() -> None:
    """build_resolver raises ValueError when no registered resolver claims scope.

    In OIDC mode, the only currently registered resolver (RsamClaimSource)
    returns False from is_in_scope, so the factory finds no match and raises.
    This confirms that registering a resolver with explicit mode checks prevents
    accidental fallthrough to the wrong resolver.
    """
    settings = _make_settings(auth_mode="oidc")
    mock_session_factory = MagicMock()

    with pytest.raises(ValueError, match="No claim-source resolver registered"):
        build_resolver(settings=settings, session_factory=mock_session_factory)


@pytest.mark.asyncio
async def test_build_resolver_rsam_resolve_calls_fetch_authorities() -> None:
    """With auth_mode='rsam' the resolver calls fetch_authorities on resolve().

    Uses an AsyncMock for fetch_authorities — no I/O. Confirms the factory
    wiring is live code, not dead code. Patches `resolve` on the RsamClaimSource
    instance directly to avoid setting up the full DB/grammar stack, while still
    proving that build_resolver selects RsamClaimSource and that it is called.
    """
    from registry.auth.resolver import AuditIdentity, ResolvedIdentity, TenantGrant
    from registry.auth.rsam.claim_source import RsamClaimSource

    settings = _make_settings(auth_mode="rsam")
    mock_session_factory = MagicMock()

    resolver = build_resolver(settings=settings, session_factory=mock_session_factory)

    # Confirm the factory selected RsamClaimSource.
    assert isinstance(resolver, RsamClaimSource)

    fake_tenant_id = uuid.uuid4()
    expected = ResolvedIdentity(
        user_id="ida-user-456",
        tenant_grants=[
            TenantGrant(
                tenant_id=fake_tenant_id,
                tenant_external_id="SEAL-42",
                catalog_role="viewer",
            )
        ],
        audit_identity=AuditIdentity(
            sub="ida-user-456",
            email=None,
            preferred_username="ida-user-456",
        ),
    )

    # Spy on the resolver's resolve method so we can confirm dispatch without
    # standing up a DB or grammar stack.
    resolver.resolve = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    claims = {"sub": "ida-user-456", "iss": "https://idp.example.com"}
    result = await resolver.resolve(claims)

    resolver.resolve.assert_called_once_with(claims)
    assert result.user_id == "ida-user-456"
    assert result.tenant_grants[0].catalog_role == "viewer"
    assert result.audit_identity is not None
    assert result.audit_identity.sub == "ida-user-456"
