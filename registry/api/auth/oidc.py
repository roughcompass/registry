"""OIDC JWT parsing and validation.

Public entry point:

    async def validate_oidc_token(
        raw_token: str,
        settings: Settings,
        cache: _OidcCache | None = None,
    ) -> tuple[dict[str, Any], str]

Returns ``(claims_payload, resolved_identity)``. The caller is
responsible for everything after JWT validation: tenant scope, actor
lookup, role resolution. Raises ``CatalogError`` on every failure so
the caller sees a uniform error type and can map it to 401/403 as
appropriate. The token value is never logged.

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
from prometheus_client import Counter

from registry.config import Settings
from registry.exceptions import CatalogError

_log = logging.getLogger(__name__)


# Counter for identity-extraction failures — alerts on tokens that pass
# signature validation but lack both `sub` and `winaccountname` (an IDP
# misconfiguration that would otherwise produce 401s without a clear
# diagnostic signal).
_IDENTITY_EXTRACTION_FAILURES = Counter(
    "registry_identity_extraction_failures_total",
    "JWTs that passed signature validation but lacked both 'sub' and 'winaccountname' claims.",
)

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
    cache: _OidcCache | None = None,
) -> tuple[dict[str, Any], str]:
    """Parse and validate ``raw_token`` as an OIDC JWT.

    Returns ``(claims_payload, resolved_identity)``. The caller is
    responsible for everything after JWT validation: tenant scope,
    actor lookup, role resolution. This function performs only the
    eight-point checklist defined in the auth ADR:

    1. Signature: signed by a key in the JWKS published at
       ``settings.oidc_discovery_url``.
    2. ``exp``: not expired (authlib's ``claims.validate()`` enforces).
    3. ``iat``: present.
    4. ``iss``: in ``settings.oidc_issuer_allowlist``.
    5. ``aud``: at least one element in ``settings.resource_uri_allowlist``
       (tokens carry the resource URI as audience under ADFS-style flows).
    6. ``azp`` / ``client_id``: in ``settings.oidc_client_id_allowlist``
       when that allowlist is non-empty (an empty allowlist skips this
       check entirely — the operator opted out of service-token gating).
    7. TTL bound: ``exp - iat`` ≤ ``settings.oidc_max_token_ttl_seconds``.
    8. Identity extraction: ``sub`` if present and non-empty; else
       ``winaccountname`` (Windows-AD fallback). Both absent raises.

    The ``cache`` parameter accepts the instance attached to
    ``app.state.oidc_cache``. When ``None``, ``get_default_cache()`` is
    used — useful from non-HTTP contexts and tests.

    Raises ``CatalogError`` on every failure path. The plaintext token
    is never logged.
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

    # --- Decode + verify signature + standard claims ------------------------
    try:
        key_set = JsonWebKey.import_key_set(jwks_raw)
        jwt = JsonWebToken(["RS256", "ES256", "RS384", "ES384", "RS512"])
        claims = jwt.decode(raw_token, key_set)
        # Authlib's claims.validate() enforces exp + iss-essential.
        # iss VALUE matching is enforced separately below against the
        # allowlist (which supersedes the discovery document's single
        # issuer).
        claims.options = {"exp": {"essential": True}}
        claims.validate()
    except JoseError as exc:
        raise CatalogError(f"invalid OIDC token: {exc}") from exc
    except Exception as exc:
        raise CatalogError("OIDC token processing error") from exc

    # Convert to a plain dict so the rest of the function does not depend
    # on authlib's claims object behavior.
    claims_payload: dict[str, Any] = dict(claims)

    # --- ADR §1 step 4: iss allowlist --------------------------------------
    iss = claims_payload.get("iss")
    issuer_allowlist = settings.oidc_issuer_allowlist or []
    if not issuer_allowlist:
        # Empty allowlist falls back to the discovery document's issuer
        # (legacy behavior). Operators should populate the allowlist in
        # production deployments — this fallback exists so existing
        # single-issuer deployments do not require a config change at
        # the same time as the auth-consolidation ship.
        if iss != issuer:
            raise CatalogError("iss-not-allowed")
    elif iss not in issuer_allowlist:
        raise CatalogError("iss-not-allowed")

    # --- ADR §1 step 5: aud allowlist --------------------------------------
    aud_claim = claims_payload.get("aud")
    aud_list: list[str]
    if aud_claim is None:
        aud_list = []
    elif isinstance(aud_claim, str):
        aud_list = [aud_claim]
    elif isinstance(aud_claim, list):
        aud_list = [str(a) for a in aud_claim]
    else:
        raise CatalogError("aud-not-allowed")

    resource_allowlist = settings.resource_uri_allowlist or []
    if resource_allowlist:
        if not any(a in resource_allowlist for a in aud_list):
            raise CatalogError("aud-not-allowed")
    elif settings.oidc_expected_audience:
        # Legacy fallback: single expected audience configured the old way.
        if settings.oidc_expected_audience not in aud_list:
            raise CatalogError("aud-not-allowed")
    else:
        _warn_audience_unconfigured_once()

    # --- ADR §1 step 6: azp / client_id allowlist --------------------------
    client_allowlist = settings.oidc_client_id_allowlist or []
    if client_allowlist:
        # Accept either azp (RFC 8176 / 7519) or client_id (commonly emitted
        # by ADFS for client_credentials grants).
        client_principal = claims_payload.get("azp") or claims_payload.get("client_id")
        if client_principal is None or client_principal not in client_allowlist:
            raise CatalogError("azp-not-allowed")
    # Empty allowlist → check skipped intentionally (operator-controlled).

    # --- ADR §1 steps 3 + 7: iat presence and TTL bound ---------------------
    iat_claim = claims_payload.get("iat")
    if iat_claim is None:
        raise CatalogError("missing-iat")
    exp_claim = claims_payload.get("exp")
    if exp_claim is not None and (
        int(exp_claim) - int(iat_claim) > settings.oidc_max_token_ttl_seconds
    ):
        raise CatalogError("token-ttl-exceeded")

    # --- ADR §1 step 8: identity extraction --------------------------------
    sub = claims_payload.get("sub")
    if sub:
        return claims_payload, str(sub)
    win = claims_payload.get("winaccountname")
    if win:
        return claims_payload, str(win)

    _IDENTITY_EXTRACTION_FAILURES.inc()
    raise CatalogError("missing-identity-claim")


__all__ = ["_OidcCache", "get_default_cache", "validate_oidc_token"]
