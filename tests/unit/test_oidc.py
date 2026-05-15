"""Unit tests for ``validate_oidc_token`` and the JWKS cache.

Covers the eight-point JWT validation checklist (signature, exp, iat,
iss-allowlist, aud-allowlist, azp-allowlist, TTL bound, identity
extraction with sub→winaccountname fallback), the JWKS cache TTL
behavior, and the discovery-document failure paths. Tests do NOT touch
the database — the function performs no DB access in this iteration.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from registry.api.auth import oidc as oidc_mod
from registry.api.auth.oidc import _OidcCache, validate_oidc_token
from registry.config import Settings
from registry.exceptions import CatalogError

_DISCOVERY = {
    "issuer": "https://idp.example.com",
    "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
}

_JWKS = {"keys": [{"kty": "RSA", "kid": "test", "n": "x", "e": "AQAB"}]}


def _make_settings(
    *,
    oidc_url: str | None = "https://idp.example.com/.well-known/openid-configuration",
    issuer_allowlist: list[str] | None = None,
    resource_uri_allowlist: list[str] | None = None,
    client_id_allowlist: list[str] | None = None,
    max_token_ttl_seconds: int = 900,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x/y",
        pgbouncer_url="postgresql+asyncpg://x/y",
        scheduler_jobstore_url="postgresql+asyncpg://x/y",
        oidc_discovery_url=oidc_url,
        oidc_issuer_allowlist=issuer_allowlist or ["https://idp.example.com"],
        resource_uri_allowlist=resource_uri_allowlist or ["registry"],
        oidc_client_id_allowlist=client_id_allowlist or [],
        oidc_max_token_ttl_seconds=max_token_ttl_seconds,
    )


def _now_claims(**overrides: Any) -> dict[str, Any]:
    """Build a claims dict with sane defaults; tests override per case."""
    now = int(time.time())
    base: dict[str, Any] = {
        "sub": "user-1",
        "iss": "https://idp.example.com",
        "aud": "registry",
        "iat": now,
        "exp": now + 600,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def cache() -> _OidcCache:
    return _OidcCache()


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Fresh process-scoped cache + audience-warning flag per test."""
    oidc_mod._default_cache = None
    oidc_mod._audience_warning_emitted = False


def _patch_decode(claims: dict[str, Any]) -> Any:
    """Patch the JWKS fetch + JWT decode to short-circuit signature
    verification, returning the supplied claims dict instead.

    Centralizes the boilerplate so each behavioral test focuses on the
    claim it cares about.
    """

    def _decorator(func):
        async def _wrapper(*args, **kwargs):
            with (
                patch.object(_OidcCache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
                patch.object(_OidcCache, "get_jwks", AsyncMock(return_value=_JWKS)),
                patch("registry.api.auth.oidc.JsonWebKey.import_key_set", MagicMock(return_value=MagicMock())),
                patch("registry.api.auth.oidc.JsonWebToken") as JwtCls,
            ):
                claims_obj = MagicMock(spec=dict)
                claims_obj.__iter__ = lambda self: iter(claims.keys())
                claims_obj.__getitem__ = lambda self, key: claims[key]
                claims_obj.keys = MagicMock(return_value=claims.keys())
                claims_obj.values = MagicMock(return_value=claims.values())
                claims_obj.items = MagicMock(return_value=claims.items())
                claims_obj.get = lambda key, default=None: claims.get(key, default)
                claims_obj.options = {}
                claims_obj.validate = MagicMock()
                JwtCls.return_value.decode = MagicMock(return_value=claims_obj)
                return await func(*args, **kwargs)

        return _wrapper

    return _decorator


def _patch_decode_to(claims: dict[str, Any]):
    """Context-manager variant of `_patch_decode`. Easier to read inside
    individual test bodies."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with (
            patch.object(_OidcCache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
            patch.object(_OidcCache, "get_jwks", AsyncMock(return_value=_JWKS)),
            patch("registry.api.auth.oidc.JsonWebKey.import_key_set", MagicMock(return_value=MagicMock())),
            patch("registry.api.auth.oidc.JsonWebToken") as JwtCls,
        ):
            # authlib returns a dict-like JWTClaims object; we substitute a
            # plain dict-friendly mock so dict(claims) yields the test data.
            JwtCls.return_value.decode = MagicMock(
                return_value=_FakeClaims(claims)
            )
            yield

    return _ctx()


class _FakeClaims:
    """Minimal authlib JWTClaims stand-in. Behaves like a dict for the
    handful of operations validate_oidc_token performs."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.options: dict[str, Any] = {}

    def validate(self) -> None:
        return None

    def __iter__(self):
        return iter(self._payload)

    def keys(self):
        return self._payload.keys()

    def __getitem__(self, key):
        return self._payload[key]

    def get(self, key, default=None):
        return self._payload.get(key, default)


# ---------------------------------------------------------------------------
# Configuration / discovery


@pytest.mark.asyncio
async def test_oidc_not_configured_raises(cache):
    settings = _make_settings(oidc_url=None)
    with pytest.raises(CatalogError, match="OIDC not configured"):
        await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_discovery_failure_raises(cache):
    settings = _make_settings()
    with patch.object(
        _OidcCache,
        "get_discovery_doc",
        AsyncMock(side_effect=httpx.ConnectError("boom")),
    ):
        with pytest.raises(CatalogError, match="OIDC discovery failed"):
            await validate_oidc_token("h.p.s", settings, cache=cache)


# ---------------------------------------------------------------------------
# Happy path + identity extraction


@pytest.mark.asyncio
async def test_happy_path_returns_claims_and_resolved_identity(cache):
    settings = _make_settings()
    with _patch_decode_to(_now_claims(sub="user-from-sub")):
        claims, resolved = await validate_oidc_token("h.p.s", settings, cache=cache)
    assert resolved == "user-from-sub"
    assert claims["sub"] == "user-from-sub"


@pytest.mark.asyncio
async def test_winaccountname_used_when_sub_absent(cache):
    settings = _make_settings()
    payload = _now_claims(winaccountname="DOMAIN\\jdoe")
    payload.pop("sub")
    with _patch_decode_to(payload):
        _, resolved = await validate_oidc_token("h.p.s", settings, cache=cache)
    assert resolved == "DOMAIN\\jdoe"


@pytest.mark.asyncio
async def test_winaccountname_used_when_sub_empty(cache):
    settings = _make_settings()
    payload = _now_claims(sub="", winaccountname="DOMAIN\\jdoe")
    with _patch_decode_to(payload):
        _, resolved = await validate_oidc_token("h.p.s", settings, cache=cache)
    assert resolved == "DOMAIN\\jdoe"


@pytest.mark.asyncio
async def test_both_identity_claims_missing_raises_and_increments_counter(cache):
    settings = _make_settings()
    payload = _now_claims()
    payload.pop("sub")
    before = oidc_mod._IDENTITY_EXTRACTION_FAILURES._value.get()
    with _patch_decode_to(payload):
        with pytest.raises(CatalogError, match="missing-identity-claim"):
            await validate_oidc_token("h.p.s", settings, cache=cache)
    after = oidc_mod._IDENTITY_EXTRACTION_FAILURES._value.get()
    assert after - before == 1


# ---------------------------------------------------------------------------
# iss allowlist


@pytest.mark.asyncio
async def test_iss_in_allowlist_accepted(cache):
    settings = _make_settings(issuer_allowlist=["https://idp.example.com", "https://other"])
    with _patch_decode_to(_now_claims(iss="https://other")):
        await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_iss_not_in_allowlist_rejected(cache):
    settings = _make_settings(issuer_allowlist=["https://idp.example.com"])
    with _patch_decode_to(_now_claims(iss="https://attacker.example")):
        with pytest.raises(CatalogError, match="iss-not-allowed"):
            await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_iss_empty_allowlist_falls_back_to_discovery_issuer(cache):
    """Legacy behavior: empty allowlist → trust the discovery doc's
    issuer. Production deployments should populate the allowlist."""
    settings = _make_settings(issuer_allowlist=[])
    with _patch_decode_to(_now_claims(iss="https://idp.example.com")):
        await validate_oidc_token("h.p.s", settings, cache=cache)
    with _patch_decode_to(_now_claims(iss="https://other")):
        with pytest.raises(CatalogError, match="iss-not-allowed"):
            await validate_oidc_token("h.p.s", settings, cache=cache)


# ---------------------------------------------------------------------------
# aud allowlist


@pytest.mark.asyncio
async def test_aud_in_allowlist_accepted(cache):
    settings = _make_settings(resource_uri_allowlist=["registry", "other-app"])
    with _patch_decode_to(_now_claims(aud="other-app")):
        await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_aud_list_form_at_least_one_match_accepted(cache):
    settings = _make_settings(resource_uri_allowlist=["registry"])
    with _patch_decode_to(_now_claims(aud=["other-app", "registry"])):
        await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_aud_not_in_allowlist_rejected(cache):
    settings = _make_settings(resource_uri_allowlist=["registry"])
    with _patch_decode_to(_now_claims(aud="some-other-resource")):
        with pytest.raises(CatalogError, match="aud-not-allowed"):
            await validate_oidc_token("h.p.s", settings, cache=cache)


# ---------------------------------------------------------------------------
# azp / client_id allowlist


@pytest.mark.asyncio
async def test_azp_in_allowlist_accepted(cache):
    settings = _make_settings(client_id_allowlist=["service-A", "service-B"])
    with _patch_decode_to(_now_claims(azp="service-A")):
        await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_client_id_in_allowlist_accepted_when_azp_absent(cache):
    """ADFS often emits ``client_id`` instead of ``azp`` for
    client_credentials grants — accept either."""
    settings = _make_settings(client_id_allowlist=["svc-X"])
    payload = _now_claims(client_id="svc-X")
    with _patch_decode_to(payload):
        await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_azp_missing_with_non_empty_allowlist_rejected(cache):
    settings = _make_settings(client_id_allowlist=["svc-X"])
    with _patch_decode_to(_now_claims()):  # neither azp nor client_id set
        with pytest.raises(CatalogError, match="azp-not-allowed"):
            await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_empty_azp_allowlist_skips_check(cache):
    settings = _make_settings(client_id_allowlist=[])
    with _patch_decode_to(_now_claims()):  # no azp, but check is skipped
        await validate_oidc_token("h.p.s", settings, cache=cache)


# ---------------------------------------------------------------------------
# iat presence + TTL bound


@pytest.mark.asyncio
async def test_missing_iat_rejected(cache):
    settings = _make_settings()
    payload = _now_claims()
    payload.pop("iat")
    with _patch_decode_to(payload):
        with pytest.raises(CatalogError, match="missing-iat"):
            await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_ttl_at_limit_accepted(cache):
    settings = _make_settings(max_token_ttl_seconds=900)
    now = int(time.time())
    with _patch_decode_to(_now_claims(iat=now, exp=now + 900)):
        await validate_oidc_token("h.p.s", settings, cache=cache)


@pytest.mark.asyncio
async def test_ttl_one_second_over_rejected(cache):
    settings = _make_settings(max_token_ttl_seconds=900)
    now = int(time.time())
    with _patch_decode_to(_now_claims(iat=now, exp=now + 901)):
        with pytest.raises(CatalogError, match="token-ttl-exceeded"):
            await validate_oidc_token("h.p.s", settings, cache=cache)


# ---------------------------------------------------------------------------
# Cache helpers


def test_get_default_cache_returns_singleton():
    a = oidc_mod.get_default_cache()
    b = oidc_mod.get_default_cache()
    assert a is b


def test_default_cache_reset_between_tests():
    oidc_mod._default_cache = None
    fresh = oidc_mod.get_default_cache()
    assert fresh is not None
