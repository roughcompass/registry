"""TenantContext injection for FastAPI request handling.

Resolves `Authorization: Bearer <token>` to a `TenantContext`. The
`X-Tenant-Id` header is **explicitly ignored** — tenant identity is
derived solely from the token so it cannot be forged by a caller sending
a crafted header.

Token routing:
- If the raw token has two dots (JWT format) **and** OIDC is configured,
  the OIDC path is tried first.  On any ``CatalogError`` from OIDC, fall
  back to the API-token path so opaque tokens that happen to contain dots
  still work.
- Otherwise go directly to the API-token path.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

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
    """Resolve an AsyncSession from the app state. The app factory wires `state.session_factory`."""
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


async def get_tenant_context(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    clock: Clock = Depends(get_clock),
) -> TenantContext:
    """FastAPI dependency: resolve bearer token to TenantContext or raise 401.

    If the token looks like a JWT and the app has OIDC configured, the OIDC
    path is attempted first.  On failure it falls back to the API-token path.
    """
    raw = _bearer_token(request)

    if _looks_like_jwt(raw):
        settings = getattr(request.app.state, "settings", None)
        if settings is not None and settings.oidc_discovery_url is not None:
            from registry.api.auth.oidc import validate_oidc_token  # noqa: PLC0415

            oidc_cache = getattr(request.app.state, "oidc_cache", None)
            try:
                return await validate_oidc_token(raw, settings, session, cache=oidc_cache)
            except CatalogError:
                # Fall through to API-token path.
                _log.debug("oidc_validation_failed; falling back to api_token path")

    try:
        return await validate_token(session, raw, clock)
    except CatalogError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token") from exc


__all__ = ["get_clock", "get_db_session", "get_tenant_context"]
