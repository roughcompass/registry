"""RSAM-backed claim-source resolver.

Resolves tenant grants for IDA-authenticated callers via an external authority
list rather than token claims. IDA tokens authenticate the caller; RSAM provides
the tenant-scope grants. The two concerns are separated: token validity is
checked by the OIDC validator; tenant grants are resolved here.

The `fetch_authorities` callable is the sole I/O boundary for the authority
list. Production code uses the default stub (which raises `NotImplementedError`)
until the live HTTP call is wired. Tests inject a lambda or `AsyncMock` at
construction time — no module-level patching is needed or permitted.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Per-process grant cache
#
# The cache stores resolved grants per subject (JWT `sub`) so that repeated
# requests from the same caller within the TTL window do not issue a
# `fetch_authorities` call. The stale-on-failure path serves cached grants
# when `fetch_authorities` raises, up to `auth_stale_ceiling_seconds` from
# the original cache write. After the ceiling the call fails-closed.
#
# Invalidation: TTL expiry only. An explicit per-actor invalidation endpoint
# (POST /v1/admin/actors/{id}:refresh) is deferred — add a TODO comment in
# the admin router when that endpoint is wired; this class would expose a
# `invalidate(subject)` method at that point.
#
# The cache is per-process (in-memory dict). Do not share with other services.
from registry.auth.resolver import (
    AuditIdentity,
    ClaimResolverBase,
    ResolvedIdentity,
    TenantGrant,
)
from registry.auth.rsam import grammar
from registry.auth.rsam.tenant_store import upsert_rsam_actor, upsert_rsam_tenant
from registry.config import Settings

_log = logging.getLogger(__name__)

# Minimum TTL clamp — prevents operators from setting a value so low that it
# produces pathological cache churn and defeats the single-flight guarantee.
_MIN_TTL_SECONDS = 30


# ---------------------------------------------------------------------------
# Default stub — production fail-closed
#
# The live HTTP call to the external authority endpoint is not yet wired because
# the endpoint contract (URL path, response schema, caller-auth mechanism) has
# not been confirmed with the upstream team. Until that confirmation arrives and
# the call is implemented, production code that reaches `fetch_authorities`
# without an injected callable fails loudly with NotImplementedError — an
# immediate, unambiguous signal rather than a silent empty-grant return.

async def _default_stub(subject: str) -> list[str]:
    raise NotImplementedError(
        "RSAM fetch_authorities is not yet wired — inject a callable to enable "
        "this code path. See the auth/rsam/claim_source module docstring for details."
    )


# ---------------------------------------------------------------------------
# Cache entry dataclass

@dataclass
class _GrantCacheEntry:
    """One cached slot for a subject's resolved grants.

    `grants` and `audit_identity` are stored together so a stale-serve can
    reconstruct a full ResolvedIdentity without hitting the database.

    `cached_at` is a monotonic timestamp (from time.monotonic()) — it is
    only used for age comparisons, never formatted or logged as wall-clock time.

    `lock` serialises concurrent refreshes for the same subject (single-flight).
    A separate dict-structure lock (`_cache_load_lock`) protects the insertion of
    new entries into the dict; once an entry exists its own lock takes over.
    """

    grants: list[TenantGrant]
    audit_identity: AuditIdentity
    cached_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# Resolver implementation

class RsamClaimSource(ClaimResolverBase):
    """ClaimResolverBase implementation for IDA+RSAM deployments.

    IDA tokens authenticate the caller (iss/aud/exp/sig checks run in the OIDC
    validator before this class is reached); RSAM provides the tenant-scope
    grants. The two concerns are separated so token validity and grant resolution
    can evolve independently.

    Constructor parameters
    ----------------------
    settings:
        Service settings. `auth_mode` is checked in `is_in_scope`; cache
        TTL and stale-on-failure settings are read from the same object.
    session_factory:
        Callable that returns an `AsyncSession`. Used by `upsert_rsam_tenant`
        to materialise JIT tenant rows. Each SEAL in the authority list gets
        its own session call so a failure on one SEAL does not roll back others.
    fetch_authorities:
        Async callable `(subject: str) -> list[str]`. Returns the raw authority
        strings for the given subject. Defaults to `_default_stub`, which raises
        `NotImplementedError` loudly rather than silently returning empty grants.
        Inject a real callable or an `AsyncMock` for testing.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        fetch_authorities: Callable[[str], Awaitable[list[str]]] = _default_stub,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._fetch_authorities = fetch_authorities

        # Cache configuration — TTL is clamped to a minimum to prevent pathological churn.
        self._ttl_seconds: int = max(_MIN_TTL_SECONDS, settings.auth_claim_cache_ttl_seconds)
        self._stale_ceiling_seconds: int = settings.auth_stale_ceiling_seconds
        self._serve_stale: bool = settings.auth_serve_stale_on_failure

        # Per-process grant cache. Key: JWT `sub`. Value: _GrantCacheEntry.
        self._cache: dict[str, _GrantCacheEntry] = {}
        # Protects structural mutations to self._cache (insertion of new entries).
        # Once an entry exists, per-entry entry.lock serialises refreshes for that subject.
        self._cache_load_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # ClaimResolverBase interface

    def is_in_scope(self, claims: dict) -> bool:
        """Return True when the service is running in RSAM auth mode.

        The mode check lives in Settings; claims content is not inspected
        because IDA tokens may not carry a mode discriminator.
        """
        return self.settings.auth_mode == "rsam"

    async def resolve(self, claims: dict) -> ResolvedIdentity:
        """Resolve RSAM authorities into a `ResolvedIdentity`.

        Cache wrap algorithm:
        1. Extract subject from `claims["sub"]`.
        2. Fast path: if a cache entry exists and is within TTL, return it immediately.
        3. Acquire dict-structure lock to find or create the per-subject entry.
        4. Acquire per-subject lock (single-flight). Re-check cache inside the lock.
        5. On cache miss: call the fetch + parse + JIT + audit emission flow.
           On success, store the result. Return the resolved identity.
        6. On fetch failure:
           - stale-on-failure disabled → propagate exception (fail-closed).
           - stale-on-failure enabled + entry within ceiling → emit
             `auth.stale_cache.served` audit event and return cached identity.
           - stale-on-failure enabled but no entry or ceiling exceeded → raise
             HTTP 503 (service unavailable, retry after TTL).
        """
        subject: str = claims["sub"]

        # Step 2: fast path — check TTL without acquiring any lock.
        now = time.monotonic()
        existing = self._cache.get(subject)
        if existing is not None and (now - existing.cached_at) < self._ttl_seconds:
            return ResolvedIdentity(
                user_id=subject,
                tenant_grants=existing.grants,
                audit_identity=existing.audit_identity,
            )

        # Step 3: acquire dict-structure lock to ensure the per-subject entry exists.
        async with self._cache_load_lock:
            if subject not in self._cache:
                self._cache[subject] = _GrantCacheEntry(
                    grants=[],
                    audit_identity=AuditIdentity(sub=subject, email=None, preferred_username=subject),
                    cached_at=0.0,  # age=0 means never populated; TTL check will fail.
                )
            entry = self._cache[subject]

        # Step 4: acquire per-subject lock — single-flight for concurrent callers.
        async with entry.lock:
            # Re-check: another coroutine may have refreshed the entry while we waited.
            now = time.monotonic()
            if entry.cached_at > 0.0 and (now - entry.cached_at) < self._ttl_seconds:
                return ResolvedIdentity(
                    user_id=subject,
                    tenant_grants=entry.grants,
                    audit_identity=entry.audit_identity,
                )

            # Step 5: cache miss — run the full fetch + parse + JIT flow.
            try:
                grants, audit_identity = await self._fetch_and_resolve(subject)
            except Exception as exc:
                # Step 6: failure path.
                return await self._handle_fetch_failure(subject, entry, exc)

            # Success: populate cache entry.
            entry.grants = grants
            entry.audit_identity = audit_identity
            entry.cached_at = time.monotonic()

            return ResolvedIdentity(
                user_id=subject,
                tenant_grants=grants,
                audit_identity=audit_identity,
            )

    async def _handle_fetch_failure(
        self,
        subject: str,
        entry: _GrantCacheEntry,
        exc: Exception,
    ) -> ResolvedIdentity:
        """Apply the stale-on-failure policy when `fetch_authorities` raises.

        If stale-on-failure is disabled, or there is no cached entry, or the
        cache is older than `stale_ceiling_seconds`, raises an HTTP 503 (or
        propagates the original exception when stale-on-failure is disabled).

        When a valid stale entry is available and the operator has opted in,
        emits `auth.stale_cache.served` and returns the cached identity.
        """
        if not self._serve_stale:
            raise exc

        now = time.monotonic()
        stale_age = now - entry.cached_at if entry.cached_at > 0.0 else None

        if stale_age is not None and stale_age < self._stale_ceiling_seconds:
            # Stale-serve: emit audit event and return cached result.
            tenant_id = entry.grants[0].tenant_id if entry.grants else None
            await self._emit_stale_cache_event(subject, tenant_id, stale_age)
            _log.warning(
                "RSAM fetch_authorities failed; serving stale grant cache for subject=%s "
                "stale_age_seconds=%d",
                subject,
                int(stale_age),
            )
            return ResolvedIdentity(
                user_id=subject,
                tenant_grants=entry.grants,
                audit_identity=entry.audit_identity,
            )

        # No usable stale entry (never populated or ceiling exceeded).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The RSAM authority service is temporarily unavailable. "
                f"Retry after {self._ttl_seconds} seconds."
            ),
            headers={"Retry-After": str(self._ttl_seconds)},
        )

    async def _fetch_and_resolve(
        self, subject: str
    ) -> tuple[list[TenantGrant], AuditIdentity]:
        """Run the full fetch + parse + JIT + audit emission flow.

        This is the core resolution path extracted from `resolve()` so the cache
        wrap can call it cleanly. Returns (grants, audit_identity). Raises on
        any upstream failure — the caller decides how to handle stale data.
        """
        # Fetch raw authority strings — may raise on upstream failure.
        # Measure wall-clock latency around the I/O call for the audit payload.
        _t0 = time.monotonic()
        raw_authorities: list[str] = await self._fetch_authorities(subject)
        _latency_ms = int((time.monotonic() - _t0) * 1000)

        # Parse, discard non-matching strings
        parsed = [grammar.parse_authority(a) for a in raw_authorities]
        valid = [p for p in parsed if p is not None]

        # Group by seal_id, collect roles per SEAL
        roles_by_seal: dict[str, list[str]] = defaultdict(list)
        for authority in valid:
            role = grammar.verb_to_role(authority.verb)
            roles_by_seal[authority.seal_id].append(role)

        # Upsert tenant, upsert actor (one per tenant), build grants
        tenant_grants: list[TenantGrant] = []
        for seal_id, roles in roles_by_seal.items():
            best_role = grammar.highest_role(roles)
            async with self._session_factory() as session, session.begin():
                tenant_uuid = await upsert_rsam_tenant(session, seal_id)
                # Actor row is guaranteed for downstream AuditIdentity SELECT.
                await upsert_rsam_actor(session, tenant_uuid, subject)
            tenant_grants.append(
                TenantGrant(
                    tenant_id=tenant_uuid,
                    tenant_external_id=seal_id,
                    catalog_role=best_role,
                )
            )

        # Populate AuditIdentity from actors table.
        # When there are zero grants, no actor row exists — fall back to subject-only
        # identity (the resolver layer translates zero grants to 403 separately).
        if tenant_grants:
            audit_identity = await self._build_audit_identity(
                subject, tenant_grants[0].tenant_id
            )
        else:
            audit_identity = AuditIdentity(
                sub=subject, email=None, preferred_username=subject
            )

        # Log RSAM authority resolution summary so operators can track resolver
        # activity, latency, and authority counts without a dedicated audit row.
        # The audit_log schema requires non-null target_type and target_id which
        # are not meaningful for a cross-tenant auth event; structured logging is
        # the appropriate observability surface here.
        _log.info(
            "auth.claim_source.invoked subject=%s source=rsam latency_ms=%d authority_count=%d",
            subject,
            _latency_ms,
            len(raw_authorities),
        )

        return tenant_grants, audit_identity

    async def _emit_stale_cache_event(
        self,
        subject: str,
        tenant_id: uuid.UUID | None,
        stale_age: float,
    ) -> None:
        """Write the `auth.stale_cache.served` audit row.

        Payload keys:
          - tenant_id: first grant's tenant UUID as a string, or null when the
            cached identity has no grants.
          - stale_age_seconds: integer seconds since the cache entry was written.

        The write is best-effort: a failure here does not block the stale-serve
        response. Log and swallow any exception so the caller still gets its
        (stale) identity.
        """
        tenant_id_str = str(tenant_id) if tenant_id is not None else "null"
        stale_age_int = int(stale_age)
        now = datetime.datetime.now(tz=datetime.UTC)
        try:
            async with self._session_factory() as _audit_session:
                await _audit_session.execute(
                    text(
                        "INSERT INTO audit_log "
                        "(audit_id, tenant_id, actor_id, action, target_type, "
                        " target_id, before_jsonb, after_jsonb, ts, request_id, error_code) "
                        "VALUES "
                        "(:audit_id, NULL, NULL, 'auth.stale_cache.served', NULL, "
                        " NULL, NULL, CAST(:after_jsonb AS jsonb), :ts, NULL, NULL)"
                    ),
                    {
                        "audit_id": uuid.uuid4(),
                        "after_jsonb": (
                            '{"tenant_id": '
                            + (f'"{tenant_id_str}"' if tenant_id is not None else "null")
                            + f', "stale_age_seconds": {stale_age_int}}}'
                        ),
                        "ts": now,
                    },
                )
                await _audit_session.commit()
        except Exception:  # noqa: BLE001
            _log.exception(
                "Failed to emit auth.stale_cache.served audit event for subject=%s", subject
            )

    async def _build_audit_identity(
        self, subject: str, tenant_id: uuid.UUID
    ) -> AuditIdentity:
        """Fetch the actor row for (tenant_id, oidc_subject=subject) and build the
        full AuditIdentity. The actor row is guaranteed to exist because Step 5a
        upserted it; a miss is a programming error and raises RuntimeError.

        Field rules:
          - sub: always the JWT subject.
          - email: actor.email when non-NULL; None otherwise (admins can populate
            later via the actors admin path).
          - preferred_username: actor.display_name when non-NULL; subject as
            last-resort fallback (display_name is NOT NULL in the schema, so this
            is defensive only).
        """
        async with self._session_factory() as session:
            row = await session.execute(
                text(
                    "SELECT display_name, email FROM actors "
                    "WHERE tenant_id = :tenant_id AND oidc_subject = :oidc_subject"
                ),
                {"tenant_id": tenant_id, "oidc_subject": subject},
            )
            actor = row.first()
        if actor is None:
            raise RuntimeError(
                "actor row missing after JIT upsert — programming error"
            )
        display_name, email = actor
        return AuditIdentity(
            sub=subject,
            email=email or None,
            preferred_username=display_name or subject,
        )


__all__ = ["RsamClaimSource", "_default_stub"]
