"""TenantContext injection for FastAPI request handling.

Resolves `Authorization: Bearer <token>` to a `TenantContext`. The
`X-Tenant-Id` header is **explicitly ignored** on the standard OIDC path —
tenant identity is derived solely from the token so it cannot be forged by a
caller sending a crafted header.

In RSAM auth mode the behaviour is different:

- An IDA JWT authenticates the caller (signature and expiry only; no tenant
  claim is required or expected in the token).
- The OIDC validator returns a sentinel `TenantContext` carrying the verified
  subject but nil tenant/actor UUIDs.
- The RSAM resolver converts that subject into a `ResolvedIdentity` that
  carries zero or more `TenantGrant` entries (one per SEAL the caller holds).
- The tenant-selector step maps the grant list to a single `TenantContext`:
  - Zero grants → 403 ``no_tenant_grants``.
  - One grant + no header → auto-select (no header required).
  - Multiple grants + no header → 400 ``tenant_context_required``.
  - Header present and matches a grant → select that grant.
  - Header present but no matching grant → 403 ``tenant_not_authorized``.

Header names are configurable via `Settings.auth_tenant_id_header` (primary)
and `Settings.auth_seal_id_header_alias` (optional alias).  The primary header
wins when both are present in the same request.

Token routing (non-RSAM path):
- If the raw token has two dots (JWT format) **and** OIDC is configured,
  the OIDC path is tried first.  On any ``CatalogError`` from OIDC, fall
  back to the API-token path so opaque tokens that happen to contain dots
  still work.
- Otherwise go directly to the API-token path.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from registry.api.auth.tokens import validate_token
from registry.exceptions import CatalogError
from registry.types import Clock, SystemClock, TenantContext

_log = logging.getLogger(__name__)


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, raw = auth.partition(" ")
    if scheme.lower() != "bearer" or not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    return raw


def _looks_like_jwt(token: str) -> bool:
    """Return True if *token* has the three-part base64url dot structure of a JWT."""
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def get_clock() -> Clock:
    """Default Clock dependency. Tests override to inject a FakeClock."""
    return SystemClock()


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Resolve an AsyncSession from the app state and wrap the request in a transaction.

    Wrapping in ``session.begin()`` makes the per-request session commit on
    success and rollback on raised exceptions. Without this, writes made
    via the yielded session would roll back when the ``async with factory()``
    block exits — the autobegin transaction is never committed.
    """
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        yield session


# ---------------------------------------------------------------------------
# RSAM tenant-selector logic


def _select_entitlement_tenant(request: Request, resolved_identity: Any) -> TenantContext:
    """Apply the per-request tenant-selector rules to a ``ResolvedIdentity``.

    Returns a fully-populated ``TenantContext`` on success, or raises
    ``HTTPException`` with a structured JSON body on every failure path.

    Selection rules (in evaluation order):
    1. Zero grants → 403.
    2. Exactly one grant → auto-select; the caller does not need to send a
       header.
    3. Multiple grants, no header → 400 listing available tenant identifiers.
    4. Multiple grants, header present and matching a grant → select that grant.
    5. Multiple grants, header present but no grant matches → 403.
    """
    from registry.auth.resolver import ResolvedIdentity  # noqa: PLC0415

    identity: ResolvedIdentity = resolved_identity
    grants = identity.tenant_grants

    # Rule 1: no grants at all
    if not grants:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "no_tenant_grants", "message": "caller holds no tenant grants"},
        )

    # Rule 2: exactly one grant — auto-select, no header needed
    if len(grants) == 1:
        grant = grants[0]
        return TenantContext(
            tenant_id=grant.tenant_id,
            actor_id=uuid.UUID(int=0),  # resolved from DB by downstream; sentinel here
            roles=[grant.catalog_role],
        )

    # Multiple grants: try to read header
    settings = getattr(request.app.state, "settings", None)
    primary_header = "X-Tenant-ID" if settings is None else settings.auth_tenant_id_header
    alias_header: str | None = "X-SEAL-ID" if settings is None else settings.auth_seal_id_header_alias

    header_value: str | None = request.headers.get(primary_header)
    if header_value is None and alias_header is not None:
        header_value = request.headers.get(alias_header)

    # Rule 3: multiple grants, no header
    if header_value is None:
        available = [g.tenant_external_id for g in grants]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "tenant_context_required",
                "message": "multiple tenant grants; send the tenant identifier in the request header",
                "available_tenant_ids": available,
            },
        )

    # Rule 4 / 5: header present — find a matching grant
    matched = next((g for g in grants if g.tenant_external_id == header_value), None)
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "tenant_not_authorized", "message": "requested tenant not in caller's grant set"},
        )

    return TenantContext(
        tenant_id=matched.tenant_id,
        actor_id=uuid.UUID(int=0),  # resolved from DB by downstream; sentinel here
        roles=[matched.catalog_role],
    )


async def get_tenant_context(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    clock: Clock = Depends(get_clock),
) -> TenantContext:
    """FastAPI dependency: resolve bearer token to TenantContext or raise 401/403.

    If the token looks like a JWT and the app has OIDC configured, the OIDC
    path is attempted first.  On failure it falls back to the API-token path.

    When auth_mode is 'rsam', the OIDC validator returns a sentinel context
    carrying the verified subject.  This function then calls the RSAM claim
    resolver to convert the subject to a ``ResolvedIdentity`` and applies the
    per-request tenant-selector rules before returning a fully-resolved
    ``TenantContext``.
    """
    raw = _bearer_token(request)

    if _looks_like_jwt(raw):
        settings = getattr(request.app.state, "settings", None)
        if settings is not None and settings.oidc_discovery_url is not None:
            from registry.api.auth.oidc import validate_oidc_token  # noqa: PLC0415

            oidc_cache = getattr(request.app.state, "oidc_cache", None)
            try:
                claims_payload, resolved_identity = await validate_oidc_token(
                    raw, settings, cache=oidc_cache
                )
            except CatalogError:
                # Fall through to API-token path.
                _log.debug("oidc_validation_failed; falling back to api_token path")
                sentinel = None
            else:
                # Bridge to the existing middleware shape until the
                # middleware pipeline is rewritten in a follow-on task.
                # The sentinel carries the resolved identity in roles[0]
                # so the downstream RSAM branch can pull it back out;
                # the nil UUIDs are intentional placeholders that the
                # claim-source resolver overwrites before any service
                # code is reached.
                import uuid as _uuid  # noqa: PLC0415
                sentinel = TenantContext(
                    tenant_id=_uuid.UUID(int=0),
                    actor_id=_uuid.UUID(int=0),
                    roles=[resolved_identity],
                )

            if sentinel is not None:
                # In RSAM mode the OIDC validator returns a sentinel with nil UUIDs
                # and roles=[subject].  The RSAM grant resolver takes over from here.
                if settings.auth_mode == "rsam":
                    # Extract subject from sentinel (stored in roles[0]).
                    subject = sentinel.roles[0] if sentinel.roles else None
                    if not subject:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid token: missing subject",
                        )

                    resolver = getattr(request.app.state, "claim_resolver", None)
                    if resolver is None:
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="claim resolver not configured",
                        )

                    try:
                        resolved = await resolver.resolve({"sub": subject})
                    except Exception as exc:
                        _log.warning("entitlement_resolve_failed: %s", type(exc).__name__)
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="claim source unavailable",
                        ) from exc

                    return _select_entitlement_tenant(request, resolved)

                # Non-RSAM OIDC path: sentinel is the real TenantContext.
                return sentinel

    try:
        return await validate_token(session, raw, clock)
    except CatalogError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token") from exc


__all__ = ["get_clock", "get_db_session", "get_tenant_context"]
