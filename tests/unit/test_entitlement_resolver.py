"""End-to-end behavioral tests for EntitlementResolver.

Cache-specific behaviors live in test_entitlement_cache.py. This file
covers the full resolve() path: identity extraction, fetcher invocation,
parser integration, JIT-upsert fan-out, ResolvedIdentity assembly, and
the role-precedence rule.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.auth.entitlements import client as entitlement_client
from registry.auth.entitlements.resolver import EntitlementResolver
from registry.config import Settings


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test/test",
        pgbouncer_url="postgresql+asyncpg://test/test",
        scheduler_jobstore_url="postgresql+asyncpg://test/test",
        auth_claim_source_url="https://entitlement.example.com",
        entitlement_service_url="https://entitlement.test.local",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        },
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


def _claims(
    *,
    sub: str | None = "user-abc",
    iat: int | None = None,
    exp: int | None = None,
    jti: str | None = None,
    winaccountname: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iat": iat if iat is not None else now,
        "exp": exp if exp is not None else now + 900,
    }
    if sub is not None:
        payload["sub"] = sub
    if jti is not None:
        payload["jti"] = jti
    if winaccountname is not None:
        payload["winaccountname"] = winaccountname
    return payload


def _make_resolver(fetcher: AsyncMock) -> EntitlementResolver:
    return EntitlementResolver(
        settings=_settings(),
        session_factory=_session_factory_mock(),
        fetcher=fetcher,
    )


def _patch_upserts(tenant_uuid: uuid.UUID | None = None):
    return patch.multiple(
        "registry.auth.entitlements.resolver",
        upsert_entitlement_tenant=AsyncMock(
            return_value=tenant_uuid or uuid.uuid4()
        ),
        upsert_entitlement_actor=AsyncMock(return_value=uuid.uuid4()),
    )


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEntitlementShape:
    async def test_zero_entitlements_yields_empty_grants(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(_claims())
            assert result.tenant_grants == []
            # Audit identity falls back to subject-only when there are
            # no grants — the actors-table SELECT is skipped.
            assert result.audit_identity.sub == "user-abc"

    async def test_single_tenant_single_role(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=["111_REGISTRY_ADMIN"])
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(_claims())
            assert len(result.tenant_grants) == 1
            grant = result.tenant_grants[0]
            assert grant.tenant_external_id == "111"
            assert grant.catalog_role == "admin"

    async def test_multi_tenant_two_distinct_grants(self):
        with _patch_upserts():
            fetcher = AsyncMock(
                return_value=["111_REGISTRY_ADMIN", "222_REGISTRY_PRODUCER"]
            )
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(_claims())
            external_ids = {g.tenant_external_id for g in result.tenant_grants}
            roles = {g.catalog_role for g in result.tenant_grants}
            assert external_ids == {"111", "222"}
            assert roles == {"admin", "producer"}

    async def test_unknown_role_suffix_dropped(self):
        with _patch_upserts():
            # Two entitlements, one with an unknown role.
            fetcher = AsyncMock(
                return_value=["111_REGISTRY_ADMIN", "222_REGISTRY_GHOST"]
            )
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(_claims())
            # Only the well-formed entitlement survives.
            assert len(result.tenant_grants) == 1
            assert result.tenant_grants[0].tenant_external_id == "111"

    async def test_multi_role_for_same_tenant_collapses_to_highest(self):
        with _patch_upserts():
            # Same tenant, two roles — admin wins by precedence.
            fetcher = AsyncMock(
                return_value=["111_REGISTRY_CONSUMER", "111_REGISTRY_ADMIN"]
            )
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(_claims())
            assert len(result.tenant_grants) == 1
            assert result.tenant_grants[0].catalog_role == "admin"


@pytest.mark.asyncio
class TestIdentityExtraction:
    async def test_uses_sub_when_present(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(_claims(sub="user-from-sub"))
            assert result.user_id == "user-from-sub"
            # Fetcher receives the resolved identity for the upstream call.
            fetcher.assert_awaited_once()
            assert fetcher.await_args.kwargs["resolved_identity"] == "user-from-sub"

    async def test_falls_back_to_winaccountname_when_sub_missing(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(
                _claims(sub=None, winaccountname="DOMAIN\\jdoe")
            )
            assert result.user_id == "DOMAIN\\jdoe"

    async def test_both_missing_raises(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            with pytest.raises(ValueError, match="lacking both"):
                await resolver.resolve(_claims(sub=None))

    async def test_empty_sub_falls_back_to_winaccountname(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(
                _claims(sub="", winaccountname="DOMAIN\\jdoe")
            )
            assert result.user_id == "DOMAIN\\jdoe"


@pytest.mark.asyncio
class TestUpstreamFailures:
    """All five upstream-failure rows from the documented failure-mode table."""

    async def test_401_propagates(self):
        with _patch_upserts():
            fetcher = AsyncMock(
                side_effect=entitlement_client.EntitlementAuthError(401)
            )
            resolver = _make_resolver(fetcher)
            with pytest.raises(entitlement_client.EntitlementAuthError):
                await resolver.resolve(_claims())

    async def test_403_propagates(self):
        with _patch_upserts():
            fetcher = AsyncMock(
                side_effect=entitlement_client.EntitlementAuthError(403)
            )
            resolver = _make_resolver(fetcher)
            with pytest.raises(entitlement_client.EntitlementAuthError):
                await resolver.resolve(_claims())

    async def test_404_propagates_as_not_found(self):
        with _patch_upserts():
            fetcher = AsyncMock(
                side_effect=entitlement_client.EntitlementNotFoundError()
            )
            resolver = _make_resolver(fetcher)
            with pytest.raises(entitlement_client.EntitlementNotFoundError):
                await resolver.resolve(_claims())

    async def test_429_propagates_as_rate_limit(self):
        with _patch_upserts():
            fetcher = AsyncMock(
                side_effect=entitlement_client.EntitlementRateLimitError()
            )
            resolver = _make_resolver(fetcher)
            with pytest.raises(entitlement_client.EntitlementRateLimitError):
                await resolver.resolve(_claims())

    async def test_5xx_with_cold_cache_propagates(self):
        with _patch_upserts():
            fetcher = AsyncMock(
                side_effect=entitlement_client.EntitlementServiceError(
                    "upstream 503"
                )
            )
            resolver = _make_resolver(fetcher)
            with pytest.raises(entitlement_client.EntitlementServiceError):
                await resolver.resolve(_claims())

    async def test_malformed_propagates(self):
        with _patch_upserts():
            fetcher = AsyncMock(
                side_effect=entitlement_client.EntitlementMalformedError(
                    "bad body"
                )
            )
            resolver = _make_resolver(fetcher)
            with pytest.raises(entitlement_client.EntitlementMalformedError):
                await resolver.resolve(_claims())


@pytest.mark.asyncio
class TestFetcherInvocation:
    async def test_passes_raw_jwt_when_present_in_claims(self):
        """The middleware tucks the raw token under ``__raw_token`` so the
        fetcher can forward it as the upstream Bearer."""
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            claims = _claims()
            claims["__raw_token"] = "the.user.jwt"
            await resolver.resolve(claims)
            assert fetcher.await_args.kwargs["raw_jwt"] == "the.user.jwt"

    async def test_passes_request_id_when_present(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            claims = _claims()
            claims["__request_id"] = "req-abc"
            await resolver.resolve(claims)
            assert fetcher.await_args.kwargs["request_id"] == "req-abc"

    async def test_passes_settings(self):
        with _patch_upserts():
            fetcher = AsyncMock(return_value=[])
            resolver = _make_resolver(fetcher)
            await resolver.resolve(_claims())
            assert fetcher.await_args.kwargs["settings"] is resolver.settings


@pytest.mark.asyncio
class TestRolePrecedence:
    """admin > producer > consumer > auditor."""

    @pytest.mark.parametrize(
        "roles, winner",
        [
            (["consumer", "producer"], "producer"),
            (["auditor", "consumer"], "consumer"),
            (["auditor", "admin", "consumer"], "admin"),
            (["producer", "admin"], "admin"),
            (["auditor"], "auditor"),
        ],
    )
    async def test_highest_role_wins_for_same_tenant(self, roles, winner):
        with _patch_upserts():
            entitlements = [
                f"111_REGISTRY_{role.upper()}" for role in roles
            ]
            fetcher = AsyncMock(return_value=entitlements)
            resolver = _make_resolver(fetcher)
            result = await resolver.resolve(_claims())
            assert len(result.tenant_grants) == 1
            assert result.tenant_grants[0].catalog_role == winner


# `is_in_scope` was removed from the abstract interface and from
# EntitlementResolver in the discriminator-removal task; the previous
# test class for it is gone. The factory in registry.auth.resolver now
# instantiates EntitlementResolver directly.
