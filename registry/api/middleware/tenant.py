"""TenantContext injection for FastAPI request handling.

Resolves ``Authorization: Bearer <token>`` to a ``TenantContext`` via the
nine-step pipeline defined in the auth ADR §5:

1. Extract bearer token (401 if missing).
2. Validate JWT via ``validate_oidc_token`` (401 on any CatalogError).
3. Tuck the raw token + request id into the claims dict so the resolver
   can forward them to the entitlement service.
4. Call ``EntitlementResolver.resolve(claims)`` — the resolver handles
   cache check, single-flight, the upstream fetch, parser, JIT tenant
   upsert, and stale-on-failure semantics. Each typed
   ``EntitlementClientError`` from the upstream maps to a specific HTTP
   status (steps 4a–4f below).
5. (resolver internal) parse entitlement strings and JIT-upsert tenants.
6. Empty grants → 403.
7. Resolve tenant via the ``X-Tenant-ID`` header:
   - Single grant + no header → auto-select.
   - Single + matching header → select.
   - Single + non-matching header → 403.
   - Multiple + no header → 400.
   - Multiple + matching header → select.
   - Multiple + non-matching header → 403.
8. Idempotent JIT actor upsert for the selected tenant — surfaces the
   specific ``actor_id`` for use in audit logs.
9. Construct ``TenantContext`` with ``oidc_subject``,
   ``tenant_memberships``, ``selected_tenant_id`` (canonical),
   ``tenant_id`` (legacy alias), ``actor_id``, and the selected
   tenant's role set.

``get_authenticated_context`` is the tenantless variant for endpoints
that need the caller identified but not tenant-scoped (e.g.
``/v1/whoami``). It runs steps 1–6 and returns a TenantContext with
empty memberships and ``actor_id=None``.

Failure-to-status mapping (step 4):
- ``EntitlementAuthError(401)`` → 401 ``authentication required``
- ``EntitlementAuthError(403)`` → 403 ``access denied``
- ``EntitlementNotFoundError`` → 403
- ``EntitlementRateLimitError`` → 503
- ``EntitlementMalformedError`` → 503
- ``EntitlementServiceError`` → 503 (resolver may have served stale
  cache before raising; if so, the request reached step 7 instead.)

The middleware never touches the api_token table. Opaque-token auth was
removed in this iteration.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession

from registry.api.auth.oidc import validate_oidc_token
from registry.auth.entitlements import client as entitlement_client
from registry.auth.entitlements.actor_store import (
    DisabledTenantError,
    upsert_entitlement_actor,
)
from registry.auth.resolver import ResolvedIdentity, TenantGrant
from registry.exceptions import CatalogError
from registry.types import Clock, SystemClock, TenantContext, TenantMembership

_log = logging.getLogger(__name__)


# Counter for entitlement entries that the middleware drops downstream
# of the parser. The parser has its own counter for shape failures
# (other_namespace, malformed string, unknown role); this counter
# covers the middleware-layer drop reasons: a tenant the operator has
# disabled, a tenant the resolver looked up but can't materialize, etc.
_DROPPED_ENTRIES = Counter(
    "registry_entitlement_dropped_entries_total",
    "Entitlement entries dropped by the middleware before reaching the route handler.",
    ["reason"],
)


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, raw = auth.partition(" ")
    if scheme.lower() != "bearer" or not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )
    return raw


def get_clock() -> Clock:
    """Default Clock dependency. Tests override to inject a FakeClock."""
    return SystemClock()


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Per-request AsyncSession wrapped in a transaction.

    Wrapping in ``session.begin()`` makes the per-request session commit
    on success and rollback on raised exceptions. Without this, writes
    made via the yielded session would roll back when the
    ``async with factory()`` block exits — the autobegin transaction is
    never committed.
    """
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        yield session


def _enrich_claims_for_resolver(
    request: Request, claims: dict[str, Any], raw_token: str
) -> dict[str, Any]:
    """Tuck the raw JWT and request id into the claims dict so the
    resolver's fetcher can forward them to the entitlement service.

    These two underscore-prefixed keys are the documented contract
    between the middleware and the resolver — the OIDC validator never
    produces these names, so collisions are impossible.
    """
    claims["__raw_token"] = raw_token
    request_id = request.headers.get("X-Request-ID")
    if request_id:
        claims["__request_id"] = request_id
    return claims


async def _resolve_entitlements(
    request: Request, claims: dict[str, Any]
) -> ResolvedIdentity:
    """Call the resolver and translate every typed upstream error into
    the appropriate ``HTTPException``.

    The resolver's typed exception hierarchy is the failure-mode
    contract: each subclass has a single defined response. Mapping them
    to HTTP statuses here keeps that contract single-sourced and makes
    the call sites in the pipeline read linearly.
    """
    resolver = getattr(request.app.state, "claim_resolver", None)
    if resolver is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="claim resolver not configured",
        )

    try:
        return await resolver.resolve(claims)  # type: ignore[no-any-return]
    except entitlement_client.EntitlementAuthError as exc:
        # 401 → authentication; 403 → forbidden. Upstream's
        # authoritative answer; cache MUST NOT be consulted (the
        # resolver enforces this — never serves stale on auth errors).
        if exc.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="access denied"
        ) from exc
    except entitlement_client.EntitlementNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="access denied"
        ) from exc
    except entitlement_client.EntitlementRateLimitError as exc:
        # 429-after-retry from upstream → 503 to client. Cache MUST NOT
        # be consulted (the resolver enforces this).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service unavailable",
        ) from exc
    except entitlement_client.EntitlementMalformedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service unavailable",
        ) from exc
    except entitlement_client.EntitlementServiceError as exc:
        # 5xx / timeout / network → 503. The resolver will have already
        # served stale cache transparently if a non-expired entry
        # exists; reaching here means cold cache.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service unavailable",
        ) from exc


def _select_tenant_grant(
    request: Request, grants: list[TenantGrant]
) -> TenantGrant:
    """Apply the X-Tenant-ID header selection rules to a non-empty list
    of tenant grants. Raises ``HTTPException`` on every failure path.

    Selection rules (in evaluation order):
    - Single grant + no header → auto-select.
    - Single grant + header that matches → select.
    - Single grant + header that does NOT match → 403.
    - Multiple grants + no header → 400 listing available tenants.
    - Multiple grants + matching header → select.
    - Multiple grants + non-matching header → 403.
    """
    header_value = request.headers.get("X-Tenant-ID")

    if len(grants) == 1:
        only = grants[0]
        if header_value is None or header_value == only.tenant_external_id:
            return only
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="access denied"
        )

    # Multiple grants — header is required.
    if header_value is None:
        available = [g.tenant_external_id for g in grants]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "tenant_required",
                "message": (
                    "multiple tenants available; specify X-Tenant-ID header"
                ),
                "available_tenants": available,
            },
        )

    matched = next(
        (g for g in grants if g.tenant_external_id == header_value), None
    )
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="access denied"
        )
    return matched


def _build_tenant_context(
    *,
    resolved_identity: str,
    actor_id: uuid.UUID,
    grants: list[TenantGrant],
    selected: TenantGrant,
) -> TenantContext:
    """Assemble the TenantContext returned to route handlers.

    Both names (``tenant_id`` legacy field and ``selected_tenant_id``
    forward alias) point at the same UUID — see ``registry.types``.
    """
    tenant_memberships = [
        TenantMembership(
            tenant_id=g.tenant_id,
            tenant_slug=g.tenant_external_id,
            roles=frozenset({g.catalog_role}),
        )
        for g in grants
    ]
    return TenantContext(
        tenant_id=selected.tenant_id,
        actor_id=actor_id,
        roles=[selected.catalog_role],
        oidc_subject=resolved_identity,
        tenant_memberships=tenant_memberships,
    )


async def get_tenant_context(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TenantContext:
    """FastAPI dependency: resolve bearer JWT to a fully-tenant-scoped
    ``TenantContext``. Implements the nine-step pipeline.

    Use this on routes that operate within a tenant. For routes that
    need the caller identified but not tenant-scoped (e.g. capability
    introspection), use ``get_authenticated_context`` instead.
    """
    raw = _bearer_token(request)

    settings = request.app.state.settings
    cache = getattr(request.app.state, "oidc_cache", None)

    # Step 2: validate JWT — every CatalogError surfaces as 401.
    try:
        claims, resolved_identity = await validate_oidc_token(
            raw, settings, cache=cache
        )
    except CatalogError as exc:
        _log.debug("oidc_validation_failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        ) from exc

    # Step 3: pass the raw token + request id through the claims dict
    # so the resolver's fetcher can use them.
    enriched_claims = _enrich_claims_for_resolver(request, dict(claims), raw)

    # Steps 4–6: delegate to the resolver. Cache + fetch + parse +
    # JIT-tenant-upsert all happen inside resolve(); typed exceptions
    # from the upstream entitlement client surface here as HTTP errors.
    resolved = await _resolve_entitlements(request, enriched_claims)

    if not resolved.tenant_grants:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="access denied"
        )

    # Step 7: tenant selection.
    selected_grant = _select_tenant_grant(request, resolved.tenant_grants)

    # Step 8: idempotent actor upsert for the selected tenant — surfaces
    # the specific actor_id used by audit log writes. The resolver's
    # internal per-grant upserts already created the row; this one
    # returns its actor_id (DO UPDATE RETURNING is idempotent).
    display_name = (
        resolved.audit_identity.preferred_username
        if resolved.audit_identity is not None
        else resolved_identity
    )
    try:
        actor_id = await upsert_entitlement_actor(
            session, selected_grant.tenant_id, resolved_identity, display_name
        )
    except DisabledTenantError as exc:
        # The resolver has already filtered disabled tenants; reaching
        # here means the operator disabled the tenant between the
        # resolver's tenant lookup and this actor upsert. Race; treat
        # as 403.
        _DROPPED_ENTRIES.labels(reason="disabled_tenant").inc()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="access denied"
        ) from exc

    # Step 9: assemble the TenantContext.
    return _build_tenant_context(
        resolved_identity=resolved_identity,
        actor_id=actor_id,
        grants=resolved.tenant_grants,
        selected=selected_grant,
    )


async def get_authenticated_context(request: Request) -> TenantContext:
    """FastAPI dependency for authenticated-but-tenantless routes.

    Runs steps 1–4 of the pipeline (bearer → validate JWT → resolve
    entitlements). Returns a TenantContext with the resolved identity
    and the full tenant_memberships list, but no selected tenant and
    no actor_id (those are tenant-scoped concepts).

    Use this on routes that need to know who the caller is (for
    introspection, listing accessible tenants, etc.) but don't operate
    inside any single tenant.
    """
    raw = _bearer_token(request)

    settings = request.app.state.settings
    cache = getattr(request.app.state, "oidc_cache", None)

    try:
        claims, resolved_identity = await validate_oidc_token(
            raw, settings, cache=cache
        )
    except CatalogError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        ) from exc

    enriched_claims = _enrich_claims_for_resolver(request, dict(claims), raw)

    resolved = await _resolve_entitlements(request, enriched_claims)

    tenant_memberships = [
        TenantMembership(
            tenant_id=g.tenant_id,
            tenant_slug=g.tenant_external_id,
            roles=frozenset({g.catalog_role}),
        )
        for g in resolved.tenant_grants
    ]

    # Tenantless: no tenant_id chosen, no actor_id available.
    # tenant_id is non-Optional in the legacy shape — use the nil UUID
    # as a sentinel that handlers should not consume.
    return TenantContext(
        tenant_id=uuid.UUID(int=0),
        actor_id=uuid.UUID(int=0),
        roles=[],
        oidc_subject=resolved_identity,
        tenant_memberships=tenant_memberships,
    )


__all__ = [
    "get_authenticated_context",
    "get_clock",
    "get_db_session",
    "get_tenant_context",
]
