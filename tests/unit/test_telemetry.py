"""Telemetry tests — verify the documented metric instruments increment
on the documented behaviors.

Tests don't assert absolute counter values (the global Prometheus
registry is shared across the test suite, so other tests may have
already incremented). They take a before/after delta on each assertion,
which is the canonical respx-style technique.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from registry.api.auth import oidc as oidc_mod
from registry.api.middleware import tenant as middleware
from registry.auth.entitlements import client as entitlement_client
from registry.auth.entitlements.actor_store import DisabledTenantError
from registry.auth.entitlements.resolver import EntitlementResolver, _CACHE_TOTAL
from registry.auth.resolver import AuditIdentity, ResolvedIdentity, TenantGrant
from registry.config import Settings


# ---------------------------------------------------------------------------
# Test scaffolding


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test/test",
        pgbouncer_url="postgresql+asyncpg://test/test",
        scheduler_jobstore_url="postgresql+asyncpg://test/test",
        entitlement_service_url="https://entitlement.test.local",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={"ADMIN": "admin"},
    )


def _session_factory_mock() -> MagicMock:
    session = AsyncMock()

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    execute_result = MagicMock()
    execute_result.first = MagicMock(return_value=("display-name", None))
    session.execute = AsyncMock(return_value=execute_result)
    session.commit = AsyncMock()

    outer_cm = AsyncMock()
    outer_cm.__aenter__ = AsyncMock(return_value=session)
    outer_cm.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=outer_cm)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": "user-1",
        "iat": now,
        "exp": now + 900,
    }
    payload.update(overrides)
    return payload


def _make_resolver(fetcher: AsyncMock) -> EntitlementResolver:
    return EntitlementResolver(
        settings=_settings(),
        session_factory=_session_factory_mock(),
        fetcher=fetcher,
    )


def _patch_upserts():
    return patch.multiple(
        "registry.auth.entitlements.resolver",
        upsert_entitlement_tenant=AsyncMock(return_value=uuid.uuid4()),
        upsert_entitlement_actor=AsyncMock(return_value=uuid.uuid4()),
    )


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCacheMetric:
    async def test_hit_increments_cache_total_hit(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=["111_REGISTRY_ADMIN"])
            resolver = _make_resolver(fetcher)
            claims = _claims(jti="cache-hit-test")

            # Prime the cache.
            await resolver.resolve(claims)

            before = _CACHE_TOTAL.labels(result="hit")._value.get()
            await resolver.resolve(claims)
            after = _CACHE_TOTAL.labels(result="hit")._value.get()

            assert after - before == 1

    async def test_miss_increments_cache_total_miss(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=["111_REGISTRY_ADMIN"])
            resolver = _make_resolver(fetcher)

            before = _CACHE_TOTAL.labels(result="miss")._value.get()
            await resolver.resolve(_claims(jti="miss-test-1"))
            after = _CACHE_TOTAL.labels(result="miss")._value.get()

            assert after - before == 1

    async def test_5xx_with_warm_cache_increments_fallback(self):
        """The middleware doesn't see a fallback because the resolver
        serves it transparently. The increment happens inside
        _handle_cacheable_failure when a non-expired cache entry is
        used to bridge an upstream cacheable failure."""
        with _patch_upserts():
            # First call succeeds and primes the cache.
            fetcher = AsyncMock(return_value=["111_REGISTRY_ADMIN"])
            resolver = _make_resolver(fetcher)
            claims = _claims(jti="fallback-test")
            await resolver.resolve(claims)

            # Replace fetcher with one that raises a cacheable error;
            # force the entry's expires_at to a value that's still
            # future-relative (so the failure handler's "if entry.expires_at
            # > now" branch is taken).
            resolver._fetcher = AsyncMock(
                side_effect=entitlement_client.EntitlementServiceError("upstream 503")
            )
            for entry in list(resolver._cache.values()):
                # Push expires_at into the future so the failure handler
                # sees a still-valid entry; bypass the fast-path TTL
                # check by constructing a NEW claims dict (different
                # cache key) — actually no, we want the SAME cache key
                # so the failure path runs against the existing entry.
                # Trick: invalidate the FAST path by setting expires_at
                # to JUST past (so step 2 misses), but keep
                # _handle_cacheable_failure's "expires_at > now" branch
                # taken by re-bumping inside the handler. Easier path:
                # leave expires_at future, force a new resolve via a
                # different claims dict that hashes to a key that
                # doesn't exist, bypass cache. But that doesn't exercise
                # the fallback path.
                #
                # Cleanest trick: directly call _handle_cacheable_failure
                # with the entry so the metric is fired without the
                # fast-path bypass.
                pass

            # Directly exercise _handle_cacheable_failure since the
            # resolve() fast path would short-circuit before reaching it.
            from registry.auth.entitlements.resolver import (
                _ttl_from_jwt as _ttl,
            )
            del _ttl  # silence unused import

            entry = next(iter(resolver._cache.values()))
            entry.expires_at = time.monotonic() + 100  # still valid

            before = _CACHE_TOTAL.labels(result="fallback")._value.get()
            with patch.object(resolver, "_emit_stale_cache_event", AsyncMock()):
                await resolver._handle_cacheable_failure(
                    "test-key",
                    entry,
                    "user-1",
                    entitlement_client.EntitlementServiceError("upstream 503"),
                )
            after = _CACHE_TOTAL.labels(result="fallback")._value.get()

            assert after - before == 1


# ---------------------------------------------------------------------------


def _make_request(*, authorization: str = "Bearer dummy.jwt") -> Request:
    headers = [(b"authorization", authorization.encode())]
    settings = MagicMock()
    settings.oidc_discovery_url = "https://idp.example.com/.well-known/openid-configuration"
    app = MagicMock()
    app.state.settings = settings
    app.state.oidc_cache = MagicMock()
    return Request({"type": "http", "headers": headers, "app": app})


@pytest.mark.asyncio
class TestMiddlewareDroppedMetric:
    async def test_disabled_tenant_race_increments_dropped_disabled_tenant(self):
        request = _make_request()
        only = TenantGrant(
            tenant_id=uuid.uuid4(), tenant_external_id="111", catalog_role="admin"
        )
        resolved = ResolvedIdentity(
            user_id="user-1",
            tenant_grants=[only],
            audit_identity=AuditIdentity(sub="user-1", email=None, preferred_username="user-1"),
        )

        validator = AsyncMock(return_value=({"sub": "user-1"}, "user-1"))
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=resolved)
        request.app.state.claim_resolver = resolver

        before = middleware._DROPPED_ENTRIES.labels(reason="disabled_tenant")._value.get()
        with patch.object(middleware, "validate_oidc_token", validator):
            with patch.object(
                middleware,
                "upsert_entitlement_actor",
                AsyncMock(side_effect=DisabledTenantError("111")),
            ):
                with pytest.raises(HTTPException):
                    await middleware.get_tenant_context(request, MagicMock())
        after = middleware._DROPPED_ENTRIES.labels(reason="disabled_tenant")._value.get()
        assert after - before == 1


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestIdentityExtractionMetric:
    async def test_terminal_401_increments_identity_failures(self):
        from registry.exceptions import CatalogError

        settings = MagicMock(
            spec=Settings,
            oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
            oidc_issuer_allowlist=["https://idp.example.com"],
            oidc_client_id_allowlist=[],
            oidc_max_token_ttl_seconds=900,
            resource_uri_allowlist=["registry"],
            oidc_expected_audience=None,
        )

        # Patch the OIDC validator's discovery + JWKS path; supply a
        # claim payload that lacks both sub and winaccountname.
        from registry.api.auth.oidc import _OidcCache

        with (
            patch.object(
                _OidcCache,
                "get_discovery_doc",
                AsyncMock(return_value={"issuer": "https://idp.example.com", "jwks_uri": "x"}),
            ),
            patch.object(_OidcCache, "get_jwks", AsyncMock(return_value={"keys": []})),
            patch("registry.api.auth.oidc.JsonWebKey.import_key_set", MagicMock(return_value=MagicMock())),
            patch("registry.api.auth.oidc.JsonWebToken") as JwtCls,
        ):
            now = int(time.time())
            payload = {
                "iss": "https://idp.example.com",
                "aud": "registry",
                "iat": now,
                "exp": now + 600,
            }

            class _FakeClaims:
                def __init__(self, p):
                    self._p = p
                    self.options = {}

                def validate(self):
                    return None

                def __iter__(self):
                    return iter(self._p)

                def keys(self):
                    return self._p.keys()

                def __getitem__(self, k):
                    return self._p[k]

                def get(self, k, d=None):
                    return self._p.get(k, d)

            JwtCls.return_value.decode = MagicMock(return_value=_FakeClaims(payload))

            before = oidc_mod._IDENTITY_EXTRACTION_FAILURES._value.get()
            with pytest.raises(CatalogError, match="missing-identity-claim"):
                await oidc_mod.validate_oidc_token("h.p.s", settings)
            after = oidc_mod._IDENTITY_EXTRACTION_FAILURES._value.get()

            assert after - before == 1


# ---------------------------------------------------------------------------


class TestClientCallMetric:
    """The 200 / status-class counter on entitlement_calls_total is
    exercised by tests/unit/test_entitlement_client.py — the metric is
    incremented inside fetch_entitlements per status class. Cross-
    referencing the existing test suite here keeps T20's deliverable
    self-contained without duplicating that test bed."""

    def test_metric_exists(self):
        from registry.auth.entitlements.client import _CALLS_TOTAL

        # Smoke check that the metric is registered with the documented
        # label set; an actual increment is exercised in
        # test_entitlement_client.py.
        assert _CALLS_TOTAL._name == "registry_entitlement_calls"
        assert "status_class" in _CALLS_TOTAL._labelnames
