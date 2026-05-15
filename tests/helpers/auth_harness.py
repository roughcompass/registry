"""Two-tenant entitlement-auth test harness.

Reusable scaffolding for tests that drive the live FastAPI app against
a testcontainers Postgres while bypassing the IDP (no OIDC discovery,
no JWKS fetch) and the entitlement service (the resolver's fetcher is
mocked).

The new auth pipeline has two pluggable boundaries:

1. ``validate_oidc_token`` — patched per-request to return a synthetic
   claims dict + identity, skipping JWT signature verification.
2. ``app.state.claim_resolver.fetcher`` — an ``AsyncMock`` returning
   raw entitlement strings (e.g. ``["t-a_REGISTRY_ADMIN"]``); the
   resolver parses, caches, and JIT-materializes tenant rows.

Together these let a test pretend to be any actor in any tenant without
real cryptographic signing or upstream service contact.

Public surface
--------------
- ``EntitlementAuthHarness`` — context-managed wrapper around the app
  + mocked fetcher + a roster of configured tenants/actors.
- ``TenantPersona`` — per-tenant configuration the harness exposes.
- ``patch_validator_for_actor`` — context manager that patches the
  middleware's ``validate_oidc_token`` to return a chosen actor's
  identity for the duration of a request batch.
- ``bearer_headers`` — convenience for building Authorization +
  X-Tenant-ID headers.
"""

from __future__ import annotations

import contextlib
import datetime
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.auth.entitlements.resolver import EntitlementResolver
from registry.config import Settings
from registry.main import create_app


@dataclass
class TenantPersona:
    """A configured tenant + actor that the harness can authenticate as.

    ``slug`` is the tenant's slug (becomes the tenant the JIT path
    materializes). ``actor_id`` is a stable UUID used as the OIDC
    subject; the actor row is created lazily by the entitlement
    resolver's actor_store on first request.

    ``roles`` is the internal-role set the resolver should attach
    (after grammar parsing). Entitlement strings are constructed by
    ``EntitlementAuthHarness.set_personas`` from these.
    """

    slug: str
    actor_id: uuid.UUID = field(default_factory=uuid.uuid4)
    roles: list[str] = field(default_factory=lambda: ["producer", "consumer"])

    @property
    def oidc_subject(self) -> str:
        return f"oidc-sub-{self.slug}-{self.actor_id.hex[:8]}"


def default_settings(pg_url: str) -> Settings:
    """Build Settings appropriate for a harness-driven app.

    Mirrors the settings used by the integration entitlement-auth tests
    so behaviour is consistent across the suite.
    """
    return Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        rate_limit_enabled=False,
        oidc_discovery_url="https://idp.test.local/.well-known/openid-configuration",
        oidc_issuer_allowlist=["https://idp.test.local"],
        resource_uri_allowlist=["registry"],
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


class EntitlementAuthHarness:
    """Wraps a registry app with a mocked entitlement fetcher and a
    persona roster.

    Use as an async context manager so the FastAPI lifespan starts
    (which wires ``app.state.claim_resolver``) and so the engine is
    disposed on exit.

    The fetcher returns whatever ``set_personas`` configured for the
    most recently activated persona. Tests typically activate a
    persona via the ``patch_validator_for_actor`` context manager,
    then issue HTTP calls inside that context.
    """

    def __init__(self, pg_url: str, settings: Settings | None = None) -> None:
        self._pg_url = pg_url
        self._settings = settings or default_settings(pg_url)
        self.app: FastAPI = create_app(self._settings)
        self.fetcher: AsyncMock = AsyncMock()
        self._personas: dict[str, TenantPersona] = {}
        self._lifespan_cm: Any = None
        self._engine: Any = None

    async def __aenter__(self) -> EntitlementAuthHarness:
        # Start the FastAPI lifespan so app.state.claim_resolver exists.
        self._lifespan_cm = self.app.router.lifespan_context(self.app)
        await self._lifespan_cm.__aenter__()

        # Replace the resolver's fetcher with our mock so we can drive
        # entitlement responses per-test without making HTTP calls.
        self._engine = create_async_engine(
            self._pg_url, connect_args={"prepared_statement_cache_size": 0}
        )
        factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self.app.state.claim_resolver = EntitlementResolver(
            settings=self._settings,
            session_factory=factory,
            fetcher=self.fetcher,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        if self._lifespan_cm is not None:
            await self._lifespan_cm.__aexit__(exc_type, exc, tb)

    def add_persona(
        self,
        slug: str,
        *,
        roles: list[str] | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> TenantPersona:
        """Register a tenant+actor pair the harness can authenticate as."""
        persona = TenantPersona(
            slug=slug,
            actor_id=actor_id or uuid.uuid4(),
            roles=roles or ["producer", "consumer"],
        )
        self._personas[slug] = persona
        return persona

    def get(self, slug: str) -> TenantPersona:
        return self._personas[slug]

    def configure_fetcher_for(self, persona: TenantPersona) -> None:
        """Set ``fetcher.return_value`` to the entitlement strings the
        resolver will see for this persona on the next request.

        Builds strings in the configured grammar
        ``<slug>_<DISCRIMINATOR>_<ROLE_SUFFIX>``; uppercases the role
        name and looks it up in the inverse role mapping so the parser
        will accept it.
        """
        discriminator = self._settings.entitlement_service_discriminator
        # Invert the role mapping: internal name → upstream suffix.
        inverse = {v: k for k, v in self._settings.entitlement_role_mapping.items()}
        entries = [f"{persona.slug}_{discriminator}_{inverse[role]}" for role in persona.roles]
        self.fetcher.return_value = entries
        self.fetcher.side_effect = None


@contextlib.contextmanager
def patch_validator_for_actor(
    persona: TenantPersona,
    *,
    iat: int = 1,
    exp: int = 9999999999,
) -> Iterator[None]:
    """Patch ``validate_oidc_token`` to return ``persona``'s identity
    instead of decoding the Authorization header.

    The middleware module imports the function by name, so we patch on
    the middleware module — patching at the source module would be a
    no-op for the imported reference.
    """
    from registry.api.middleware import tenant as middleware

    claims = {"sub": persona.oidc_subject, "iat": iat, "exp": exp}
    with patch.object(
        middleware,
        "validate_oidc_token",
        AsyncMock(return_value=(claims, persona.oidc_subject)),
    ):
        yield


def bearer_headers(
    *, token: str = "harness.dummy.jwt", tenant_slug: str | None = None, **extra: str
) -> dict[str, str]:
    """Build Authorization + optional X-Tenant-ID headers."""
    headers = {"Authorization": f"Bearer {token}"}
    if tenant_slug is not None:
        headers["X-Tenant-ID"] = tenant_slug
    headers.update(extra)
    return headers


@contextlib.asynccontextmanager
async def two_tenant_harness(
    pg_url: str,
    *,
    tenant_a: str = "tenant-a",
    tenant_b: str = "tenant-b",
    a_roles: list[str] | None = None,
    b_roles: list[str] | None = None,
) -> AsyncIterator[tuple[EntitlementAuthHarness, TenantPersona, TenantPersona]]:
    """Convenience: build a harness with two pre-registered personas.

    Yields ``(harness, persona_a, persona_b)``. Use as::

        async with two_tenant_harness(pg_container) as (h, a, b):
            h.configure_fetcher_for(a)
            with patch_validator_for_actor(a):
                resp = await client.get("/v1/whoami", headers=bearer_headers())
    """
    async with EntitlementAuthHarness(pg_url) as harness:
        a = harness.add_persona(tenant_a, roles=a_roles or ["producer", "consumer", "admin"])
        b = harness.add_persona(tenant_b, roles=b_roles or ["producer", "consumer", "admin"])
        yield harness, a, b


# ---------------------------------------------------------------------------
# Datetime helper kept here so test modules don't keep re-importing
# ``datetime`` solely for a single ``now(tz=UTC)`` call.
# ---------------------------------------------------------------------------


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
