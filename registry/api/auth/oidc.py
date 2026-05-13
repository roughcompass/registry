"""OIDC JWT parsing and validation.

Public entry point:

    async def validate_oidc_token(
        raw_token: str,
        settings: Settings,
        db_session: AsyncSession,
        cache: _OidcCache | None = None,
    ) -> TenantContext

Raises ``CatalogError`` on every failure so the caller sees a uniform
error type and can map it to 401/403 as appropriate.  The token value is
never logged.

Discovery doc and JWKS are held in an ``_OidcCache`` instance with a TTL
of ``_CACHE_TTL_S`` seconds (default 300).  The cache is refreshed lazily
on first use after expiry.  An ``asyncio.Lock`` inside the cache serialises
concurrent expiry detections so exactly one upstream fetch fires per cache
miss — preventing dual-fetch during JWKS rotation.

In a FastAPI deployment the cache lives on ``app.state.oidc_cache``
(constructed in the lifespan startup).  Callers that do not have a FastAPI
request (MCP tool handlers, CLI scripts) use ``get_default_cache()`` which
returns a process-scoped singleton with the same shape.

Non-goals: OIDC user provisioning (actors must pre-exist), refresh tokens.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any

import httpx
from authlib.jose import JsonWebKey, JsonWebToken  # type: ignore[import-untyped]
from authlib.jose.errors import JoseError  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry.config import Settings
from registry.exceptions import CatalogError
from registry.storage.models import Actor, ActorRole, Role
from registry.types import TenantContext

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache TTL (seconds)
# ---------------------------------------------------------------------------

_CACHE_TTL_S: int = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Cache dataclass — one instance per app/process; never module-level globals
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _OidcCache:
    """Holds the cached OIDC discovery doc and JWKS with per-instance locking.

    The ``refresh_lock`` property serialises concurrent cache-miss paths so
    only one upstream HTTP fetch fires when multiple requests detect TTL expiry
    at the same moment.  Without the lock, concurrent expiry detections trigger
    parallel fetches and the last writer wins — harmless normally but
    potentially problematic during JWKS key rotation when the upstream may
    serve mixed key sets to concurrent requests.

    The lock is created lazily on first use.  ``asyncio.Lock`` binds to the
    running event loop when it is first awaited; constructing it eagerly (e.g.
    via ``default_factory=asyncio.Lock``) would bind it to whichever loop is
    active at construction time and raise "attached to a different loop" when
    the cache object outlives that loop (e.g. between test runs that each get
    a fresh event loop).
    """

    discovery_doc: dict[str, Any] | None = dataclasses.field(default=None)
    discovery_fetched_at: float = dataclasses.field(default=0.0)
    jwks_data: dict[str, Any] | None = dataclasses.field(default=None)
    jwks_fetched_at: float = dataclasses.field(default=0.0)
    _lock: asyncio.Lock | None = dataclasses.field(default=None, init=False, repr=False, compare=False)

    @property
    def refresh_lock(self) -> asyncio.Lock:
        """Return the per-instance lock, creating it on first access.

        Lazy initialisation binds the lock to whichever event loop is running
        when the first coroutine acquires it — not to the loop that was active
        when the cache object was constructed.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def invalidate(self) -> None:
        """Force-expire this cache instance. Safe to call from tests."""
        self.discovery_doc = None
        self.discovery_fetched_at = 0.0
        self.jwks_data = None
        self.jwks_fetched_at = 0.0

    async def get_discovery_doc(self, discovery_url: str) -> dict[str, Any]:
        """Return the cached discovery document, fetching if expired."""
        now = time.monotonic()
        # Fast path: cache is warm — no lock needed.
        if self.discovery_doc is not None and (now - self.discovery_fetched_at) < _CACHE_TTL_S:
            return self.discovery_doc

        # Slow path: acquire lock so only one coroutine fetches upstream.
        async with self.refresh_lock:
            # Re-check under lock in case another coroutine already refreshed.
            now = time.monotonic()
            if self.discovery_doc is not None and (now - self.discovery_fetched_at) < _CACHE_TTL_S:
                return self.discovery_doc

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(discovery_url)
                resp.raise_for_status()
                doc: dict[str, Any] = resp.json()

            self.discovery_doc = doc
            self.discovery_fetched_at = now
            return doc

    async def get_jwks(self, jwks_uri: str) -> dict[str, Any]:
        """Return the cached JWKS, fetching if expired."""
        now = time.monotonic()
        # Fast path: cache is warm — no lock needed.
        if self.jwks_data is not None and (now - self.jwks_fetched_at) < _CACHE_TTL_S:
            return self.jwks_data

        # Slow path: acquire lock so only one coroutine fetches upstream.
        async with self.refresh_lock:
            # Re-check under lock in case another coroutine already refreshed.
            now = time.monotonic()
            if self.jwks_data is not None and (now - self.jwks_fetched_at) < _CACHE_TTL_S:
                return self.jwks_data

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(jwks_uri)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()

            self.jwks_data = data
            self.jwks_fetched_at = now
            return data


# ---------------------------------------------------------------------------
# Process-scoped default cache (for non-FastAPI callers)
# ---------------------------------------------------------------------------

# Lazily initialised; reset between tests by assigning None before each test.
_default_cache: _OidcCache | None = None


def get_default_cache() -> _OidcCache:
    """Return the process-scoped default cache instance.

    Used by callers that do not have a FastAPI request object (e.g. MCP
    tool handlers running outside the HTTP middleware stack, CLI scripts).
    The instance is created on first call and reused for the process lifetime.

    FastAPI HTTP paths should prefer ``request.app.state.oidc_cache`` which
    is constructed in the lifespan startup and torn down cleanly.
    """
    global _default_cache  # noqa: PLW0603
    if _default_cache is None:
        _default_cache = _OidcCache()
    return _default_cache


# Module-level flag — fires the audience-disabled warning at most once per
# process. Reset between tests by toggling oidc._audience_warning_emitted.
_audience_warning_emitted: bool = False


def _warn_audience_unconfigured_once() -> None:
    """Log a one-time warning when OIDC is on but no expected audience is set."""
    global _audience_warning_emitted
    if _audience_warning_emitted:
        return
    _audience_warning_emitted = True
    _log.warning(
        "OIDC audience validation is disabled; set OIDC_EXPECTED_AUDIENCE to "
        "restrict token acceptance to this service."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def validate_oidc_token(
    raw_token: str,
    settings: Settings,
    db_session: AsyncSession,
    cache: _OidcCache | None = None,
) -> TenantContext:
    """Parse and validate *raw_token* as an OIDC JWT; return a ``TenantContext``.

    Steps:
    1. Raise ``CatalogError`` if OIDC is not configured.
    2. Fetch discovery doc → JWKS URI → key set (cached in *cache* with TTL).
    3. Decode + verify JWT signature and claims (``exp``, ``iss``).
    4. Extract ``sub`` claim → query ``actors WHERE oidc_subject = :sub``
       (tenant-scoped by the ``tenant_id`` embedded in the token's ``aud``
       or a custom ``tenant_id`` claim; falls back to iss-based lookup).
    5. Load roles from ``actor_roles`` → return ``TenantContext``.

    The *cache* parameter accepts the instance attached to ``app.state.oidc_cache``
    in FastAPI deployments.  When ``None``, ``get_default_cache()`` is used so
    the function remains callable from non-HTTP contexts without any wiring.

    Raises ``CatalogError`` on every failure (signature failure, expired
    token, actor not found, etc.).  The plaintext token is never logged.
    """
    if settings.oidc_discovery_url is None:
        raise CatalogError("OIDC not configured")

    _cache = cache if cache is not None else get_default_cache()

    # --- Fetch discovery doc + JWKS -----------------------------------------
    try:
        discovery = await _cache.get_discovery_doc(settings.oidc_discovery_url)
        jwks_uri: str = discovery["jwks_uri"]
        issuer: str = discovery["issuer"]
        jwks_raw = await _cache.get_jwks(jwks_uri)
    except (KeyError, httpx.HTTPError) as exc:
        _log.warning("oidc_discovery_failed: %s", type(exc).__name__)
        raise CatalogError("OIDC discovery failed") from exc

    # --- Decode + verify JWT ------------------------------------------------
    try:
        key_set = JsonWebKey.import_key_set(jwks_raw)
        jwt = JsonWebToken(["RS256", "ES256", "RS384", "ES384", "RS512"])
        claims = jwt.decode(raw_token, key_set)
        claims.options = {
            "iss": {"essential": True, "value": issuer},
            "exp": {"essential": True},
        }
        if settings.oidc_expected_audience:
            claims.options["aud"] = {
                "essential": True,
                "value": settings.oidc_expected_audience,
            }
        else:
            _warn_audience_unconfigured_once()
        claims.validate()
    except JoseError as exc:
        raise CatalogError(f"invalid OIDC token: {exc}") from exc
    except Exception as exc:
        raise CatalogError("OIDC token processing error") from exc

    subject: str | None = claims.get("sub")
    if not subject:
        raise CatalogError("OIDC token missing sub claim")

    # ``tenant_id`` comes from a custom claim if present; otherwise we require
    # it in the token so we can scope the DB lookup correctly.
    # When the service runs in RSAM auth mode, IDA tokens carry no tenant claim —
    # tenant scope is resolved by the downstream claim-source resolver instead of
    # the token. Skip the tenant-claim check and actor DB lookup in that mode so
    # IDA tokens are not rejected here; the resolver factory takes over grant
    # resolution after this function returns.
    tenant_id_str: str | None = claims.get("tenant_id") or claims.get("tid")
    if not tenant_id_str:
        if settings.auth_mode != "rsam":
            raise CatalogError("OIDC token missing tenant_id/tid claim")
        # RSAM mode: JWT signature + sub are valid; grant resolution happens in
        # the claim-source resolver. Return a sentinel context that carries the
        # verified subject. The nil UUIDs are intentional — this TenantContext
        # is only ever consumed by the resolver factory, which replaces it with
        # the fully-resolved identity before any service code is reached.
        import uuid  # noqa: PLC0415

        return TenantContext(
            tenant_id=uuid.UUID(int=0),
            actor_id=uuid.UUID(int=0),
            roles=[subject],
        )

    import uuid  # noqa: PLC0415

    try:
        tenant_id = uuid.UUID(tenant_id_str)
    except ValueError as exc:
        raise CatalogError("OIDC token tenant_id is not a valid UUID") from exc

    # --- Resolve actor -------------------------------------------------------
    result = await db_session.execute(
        select(Actor).where(
            Actor.tenant_id == tenant_id,
            Actor.oidc_subject == subject,
        )
    )
    actor: Actor | None = result.scalar_one_or_none()
    if actor is None:
        raise CatalogError("OIDC actor not found")

    # --- Resolve roles -------------------------------------------------------
    roles_result = await db_session.execute(
        select(Role.name)
        .join(ActorRole, ActorRole.role_id == Role.role_id)
        .where(
            ActorRole.tenant_id == tenant_id,
            ActorRole.actor_id == actor.actor_id,
        )
    )
    role_names: list[str] = [row[0] for row in roles_result.all()]

    return TenantContext(
        tenant_id=tenant_id,
        actor_id=actor.actor_id,
        roles=role_names,
    )


__all__ = ["_OidcCache", "get_default_cache", "validate_oidc_token"]
