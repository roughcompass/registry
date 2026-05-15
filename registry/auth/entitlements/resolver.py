"""Entitlement-service-backed claim resolver.

Identity is established by the OIDC validator before this class is reached
(``iss``, ``aud``, ``exp``, signature). Authorization is established here:
the resolver fetches the caller's entitlements from the upstream service,
parses them through the configurable grammar, and materializes tenant +
actor rows so the rest of the request handler can operate on the catalog's
internal types.

Cache wrap algorithm
--------------------
1. Compute the cache key. Prefer ``jti`` when the IDP mints one; otherwise
   fall back to ``sha256(resolved_identity:iat)``. The fallback uses the
   resolved identity (post sub→winaccountname fallback) — never raw
   ``sub`` — so two Windows-AD users sharing the same ``iat`` second do
   not collide.
2. Fast path: if a non-expired cache entry exists, return it immediately.
3. Acquire a per-entry lock (single-flight) so concurrent first-sightings
   for the same JWT issue exactly one upstream call.
4. Re-check inside the lock (another coroutine may have refreshed).
5. On miss: fetch entitlements from upstream, parse, JIT-upsert tenants
   and the actor, then store the entry with an expiry derived from the
   JWT's ``exp`` claim.
6. On a cacheable upstream failure (5xx/timeout/network): if a
   non-expired cache entry exists, serve it stale and emit an audit event;
   otherwise propagate the failure. Non-cacheable failures (401/403/429/
   malformed) always propagate without consulting the cache — those are
   authoritative answers from upstream and stale data must not override
   them.

The cache is per-process (in-memory). It is bounded by
``settings.entitlement_cache_max_entries`` with LRU eviction and a
per-entry TTL derived from JWT ``exp``.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import cachetools  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.auth.entitlements import client as entitlement_client
from registry.auth.entitlements import parser as entitlement_parser
from registry.auth.entitlements.actor_store import (
    DisabledTenantError,
    upsert_entitlement_actor,
    upsert_entitlement_tenant,
)
from registry.auth.resolver import (
    AuditIdentity,
    ClaimResolverBase,
    ResolvedIdentity,
    TenantGrant,
)
from registry.config import Settings

_log = logging.getLogger(__name__)


# Default fetcher signature used when no client / fetcher is injected.
# The signature mirrors the productiona ``client.fetch_entitlements`` call
# so swapping a stub for a real function in tests is a one-line change.
EntitlementFetcher = Callable[..., Awaitable[list[str]]]


async def _default_fetcher(**_kwargs: Any) -> list[str]:
    """Loud default — production code must inject a real fetcher.

    The middleware lifespan is responsible for wiring an
    ``httpx.AsyncClient`` and binding it to a callable that calls
    ``client.fetch_entitlements``. This stub raises rather than silently
    returning empty grants so a missing wire-up surfaces immediately.
    """
    raise NotImplementedError(
        "EntitlementResolver was constructed without a fetcher. "
        "The middleware lifespan must inject one — "
        "use functools.partial(client.fetch_entitlements, http_client) or equivalent."
    )


@dataclass
class _EntitlementCacheEntry:
    """One cached resolution for a single JWT.

    ``expires_at`` is bounded by the JWT's own ``exp`` claim (clamped to
    a sane minimum). ``lock`` serializes refreshes for the same cache key
    so concurrent first-misses issue exactly one upstream call.
    """

    grants: list[TenantGrant]
    audit_identity: AuditIdentity
    expires_at: float                     # absolute time.monotonic() value
    jwt_exp_monotonic: float              # absolute time.monotonic() of JWT exp
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Minimum cache TTL — protects against pathological churn when a JWT is
# already very close to expiry at the time of the first request.
_MIN_CACHE_TTL_SECONDS = 30


def _cache_key(claims: dict[str, Any], resolved_identity: str) -> str:
    """Derive a stable cache key for a JWT.

    ``jti`` is preferred when present — every IDP that mints unique
    per-token IDs guarantees no collisions. Falls back to a SHA-256 of
    ``resolved_identity:iat`` for tokens that don't include ``jti``.

    The fallback uses ``resolved_identity`` (post-sub/winaccountname
    fallback), not raw ``sub``: two Windows-AD users that share the same
    ``iat`` second but have different ``winaccountname`` values must
    produce distinct keys. Hashing also keeps the key length bounded
    when the resolved identity is a long DN-style string.
    """
    jti = claims.get("jti")
    if jti:
        return f"jti:{jti}"
    iat = claims.get("iat", "")
    digest = hashlib.sha256(f"{resolved_identity}:{iat}".encode()).hexdigest()
    return f"id-iat:{digest}"


def _ttl_from_jwt(claims: dict[str, Any]) -> float:
    """Compute cache TTL (seconds) bounded by the JWT's own ``exp`` claim.

    The JWT is the cache lifetime: once the token expires, any cached
    entry derived from it is stale and must not be served (even on
    upstream failure). Returns at least ``_MIN_CACHE_TTL_SECONDS`` to
    avoid pathological churn on near-expiry tokens.
    """
    exp = claims.get("exp")
    if exp is None:
        return float(_MIN_CACHE_TTL_SECONDS)
    remaining = float(exp) - time.time()
    return max(float(_MIN_CACHE_TTL_SECONDS), remaining)


class EntitlementResolver(ClaimResolverBase):
    """Resolves an authenticated caller's entitlements into a ``ResolvedIdentity``.

    The OIDC validator authenticates the caller (signature, ``iss``,
    ``aud``, ``exp``) before this class is reached. This resolver
    handles authorization: it asks the upstream entitlement service
    what tenants and roles the caller holds, JIT-materializes any
    new tenants and the actor row, and returns the catalog's internal
    representation.

    Constructor parameters
    ----------------------
    settings:
        Service settings. Cache size, timeouts, discriminator, and role
        mapping are all read from here.
    session_factory:
        Builds an ``AsyncSession`` for tenant/actor upserts. Each tenant
        gets its own session so a write failure on one does not roll back
        the others.
    fetcher:
        Async callable matching ``client.fetch_entitlements``'s signature.
        The middleware lifespan binds an ``httpx.AsyncClient`` and passes a
        ``functools.partial`` here. Defaults to a loud stub so a missing
        wire-up fails immediately at first call.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher: EntitlementFetcher = _default_fetcher,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._fetcher = fetcher

        # Bounded LRU cache. Per-entry TTL is enforced manually via the
        # ``expires_at`` field on each ``_EntitlementCacheEntry`` —
        # ``cachetools.TTLCache`` only supports a single global TTL,
        # which doesn't fit the JWT-exp-bounded model.
        max_entries = max(1, settings.entitlement_cache_max_entries)
        self._cache: cachetools.LRUCache[str, _EntitlementCacheEntry] = cachetools.LRUCache(
            maxsize=max_entries
        )
        # Protects structural mutations to the LRU (insertion of new
        # entries). Once an entry exists, its own ``lock`` serializes
        # per-key refreshes.
        self._cache_load_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # ClaimResolverBase interface

    async def resolve(self, claims: dict[str, Any]) -> ResolvedIdentity:
        """Resolve a caller's entitlements into a ``ResolvedIdentity``.

        See module docstring for the full cache wrap algorithm. The
        cache key is derived from ``claims`` and the caller's identity;
        the upstream call is single-flighted per cache key.
        """
        resolved_identity = self._resolved_identity_from_claims(claims)
        key = _cache_key(claims, resolved_identity)

        # Step 2: fast path — read without any lock.
        existing = self._cache.get(key)
        now = time.monotonic()
        if existing is not None and existing.expires_at > now:
            return ResolvedIdentity(
                user_id=resolved_identity,
                tenant_grants=list(existing.grants),
                audit_identity=existing.audit_identity,
            )

        # Step 3: ensure a per-key entry exists before we wait on its lock.
        async with self._cache_load_lock:
            entry = self._cache.get(key)
            if entry is None:
                entry = _EntitlementCacheEntry(
                    grants=[],
                    audit_identity=AuditIdentity(
                        sub=resolved_identity, email=None, preferred_username=resolved_identity
                    ),
                    expires_at=0.0,
                    jwt_exp_monotonic=0.0,
                )
                self._cache[key] = entry

        # Step 4: per-key single-flight.
        async with entry.lock:
            # Re-check inside the lock — another coroutine may have refreshed.
            now = time.monotonic()
            if entry.expires_at > now:
                return ResolvedIdentity(
                    user_id=resolved_identity,
                    tenant_grants=list(entry.grants),
                    audit_identity=entry.audit_identity,
                )

            try:
                grants, audit_identity = await self._fetch_and_resolve(
                    claims, resolved_identity
                )
            except entitlement_client.EntitlementServiceError as exc:
                # Cacheable failure (5xx / timeout / network) — serve stale
                # if a non-expired entry exists, otherwise propagate.
                return await self._handle_cacheable_failure(
                    key, entry, resolved_identity, exc
                )
            except entitlement_client.EntitlementClientError:
                # Non-cacheable failure (401/403/404/429/malformed) — never
                # serve stale; upstream's authoritative answer must propagate.
                raise

            # Success: populate cache entry with JWT-exp-bounded expiry.
            ttl = _ttl_from_jwt(claims)
            entry.grants = grants
            entry.audit_identity = audit_identity
            entry.expires_at = time.monotonic() + ttl
            entry.jwt_exp_monotonic = entry.expires_at

            return ResolvedIdentity(
                user_id=resolved_identity,
                tenant_grants=list(grants),
                audit_identity=audit_identity,
            )

    # ------------------------------------------------------------------
    # Helpers

    @staticmethod
    def _resolved_identity_from_claims(claims: dict[str, Any]) -> str:
        """Identity extraction with sub → winaccountname fallback.

        The OIDC validator should have populated one of these by the time
        the resolver runs; this helper just picks the right one. Empty
        strings are treated as missing (some IDPs emit ``sub=""`` rather
        than omitting the claim).
        """
        sub = claims.get("sub")
        if sub:
            return str(sub)
        win = claims.get("winaccountname")
        if win:
            return str(win)
        # Reaching here is a programming error — the OIDC validator should
        # have rejected the token before resolve() is called.
        raise ValueError(
            "EntitlementResolver.resolve called with a claim set lacking "
            "both 'sub' and 'winaccountname' — the OIDC validator should "
            "have rejected this token."
        )

    async def _fetch_and_resolve(
        self,
        claims: dict[str, Any],
        resolved_identity: str,
    ) -> tuple[list[TenantGrant], AuditIdentity]:
        """Fetch entitlements upstream, parse, and JIT-materialize tenants.

        Raises every exception type from ``client.py`` unchanged — the
        caller decides which are cacheable. Returns ``(grants,
        audit_identity)`` on success.
        """
        raw_jwt = claims.get("__raw_token", "")
        request_id = claims.get("__request_id")

        t0 = time.monotonic()
        raw_entitlements = await self._fetcher(
            resolved_identity=resolved_identity,
            raw_jwt=raw_jwt,
            settings=self.settings,
            request_id=request_id,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        parsed = entitlement_parser.parse_entitlements(raw_entitlements, self.settings)

        # Group by tenant_slug so a user holding multiple roles for the
        # same tenant collapses to a single grant.
        roles_by_slug: dict[str, list[str]] = defaultdict(list)
        for entry in parsed:
            roles_by_slug[entry.tenant_slug].append(entry.role)

        # Display name comes from the IDP's `name` claim when present;
        # falls back to the resolved identity. Same value drives both the
        # actor row's display_name and the AuditIdentity preferred_username.
        display_name = claims.get("name") or resolved_identity

        tenant_grants: list[TenantGrant] = []
        for tenant_slug, roles in roles_by_slug.items():
            best_role = self._highest_role(roles)
            try:
                async with self._session_factory() as session, session.begin():
                    tenant_uuid = await upsert_entitlement_tenant(session, tenant_slug)
                    await upsert_entitlement_actor(
                        session, tenant_uuid, resolved_identity, display_name
                    )
            except DisabledTenantError:
                # Operator has disabled this tenant — drop the tuple, log,
                # and continue with the rest of the entitlement set. The
                # entitlement service may legitimately grant access to
                # slugs the operator has explicitly offboarded; that is
                # the operator's authoritative override.
                _log.warning(
                    "entitlement_dropped_disabled_tenant slug=%s subject=%s",
                    tenant_slug,
                    resolved_identity,
                )
                continue
            tenant_grants.append(
                TenantGrant(
                    tenant_id=tenant_uuid,
                    tenant_external_id=tenant_slug,
                    catalog_role=best_role,
                )
            )

        # AuditIdentity is built directly from the claim-derived values —
        # no follow-up SELECT needed. Email is not surfaced by ADFS-style
        # identity tokens; the actors table no longer carries it either.
        audit_identity = AuditIdentity(
            sub=resolved_identity,
            email=None,
            preferred_username=display_name,
        )

        _log.info(
            "auth.entitlement.resolved subject=%s latency_ms=%d "
            "raw_entitlement_count=%d resolved_grant_count=%d",
            resolved_identity,
            latency_ms,
            len(raw_entitlements),
            len(tenant_grants),
        )

        return tenant_grants, audit_identity

    async def _handle_cacheable_failure(
        self,
        key: str,
        entry: _EntitlementCacheEntry,
        resolved_identity: str,
        exc: entitlement_client.EntitlementServiceError,
    ) -> ResolvedIdentity:
        """Serve stale on a cacheable upstream failure if possible.

        Stale-on-failure is mandatory in the new model — there is no
        operator toggle. The only gate is whether a non-expired cache
        entry exists for this JWT (a stale-but-not-expired-relative-to-
        the-token entry is still trustworthy because the upstream
        granted it within the token's lifetime).
        """
        now = time.monotonic()
        if entry.expires_at > now:
            stale_age = now - (entry.expires_at - _ttl_from_jwt({"exp": time.time()}))
            await self._emit_stale_cache_event(
                resolved_identity,
                entry.grants[0].tenant_id if entry.grants else None,
                max(0, int(stale_age)),
            )
            _log.warning(
                "entitlement_service unavailable; serving stale cache for key=%s reason=%s",
                key,
                exc.reason,
            )
            return ResolvedIdentity(
                user_id=resolved_identity,
                tenant_grants=list(entry.grants),
                audit_identity=entry.audit_identity,
            )
        # Cold cache or entry past its JWT-bounded expiry — propagate.
        raise exc

    @staticmethod
    def _highest_role(roles: list[str]) -> str:
        """Pick the highest-precedence internal role from a non-empty list.

        Precedence: admin > producer > consumer > auditor. Returns the
        first listed role if none match (defensive — the parser should
        have already filtered to known names).
        """
        precedence = ("admin", "producer", "consumer", "auditor")
        weights = {role: len(precedence) - i for i, role in enumerate(precedence)}
        return max(roles, key=lambda r: weights.get(r, 0))

    async def _emit_stale_cache_event(
        self,
        subject: str,
        tenant_id: uuid.UUID | None,
        stale_age_seconds: int,
    ) -> None:
        """Write the ``auth.entitlement_stale_cache_served`` audit row.

        Best-effort: a write failure here does not block the stale-serve
        response — the caller still gets its (stale) identity and the
        operational signal is the warning log emitted by the caller.
        """
        tenant_id_str = str(tenant_id) if tenant_id is not None else "null"
        now = datetime.datetime.now(tz=datetime.UTC)
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO audit_log "
                        "(audit_id, tenant_id, actor_id, action, target_type, "
                        " target_id, before_jsonb, after_jsonb, ts, request_id, error_code) "
                        "VALUES "
                        "(:audit_id, NULL, NULL, 'auth.entitlement_stale_cache_served', NULL, "
                        " NULL, NULL, CAST(:after_jsonb AS jsonb), :ts, NULL, NULL)"
                    ),
                    {
                        "audit_id": uuid.uuid4(),
                        "after_jsonb": (
                            '{"tenant_id": '
                            + (f'"{tenant_id_str}"' if tenant_id is not None else "null")
                            + f', "stale_age_seconds": {stale_age_seconds}}}'
                        ),
                        "ts": now,
                    },
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — best-effort audit
            _log.exception(
                "Failed to emit auth.entitlement_stale_cache_served audit for subject=%s",
                subject,
            )


__all__ = [
    "EntitlementResolver",
    "_cache_key",
    "_ttl_from_jwt",
    "_EntitlementCacheEntry",
    "EntitlementFetcher",
]
