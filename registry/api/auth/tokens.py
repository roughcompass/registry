"""API token validation.

Validates `Authorization: Bearer <token>` against the `api_tokens` table.
Resolves a valid hash + non-revoked + non-expired row to a `TenantContext`.
Plaintext tokens never leave this module — they are SHA-256 hashed before
the lookup and never appear in logs.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry.exceptions import CatalogError
from registry.storage.models import ApiToken
from registry.types import Clock, TenantContext


def hash_token(raw_token: str) -> str:
    """SHA-256 hex of the plaintext token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def validate_token(
    db_session: AsyncSession,
    raw_token: str,
    clock: Clock,
) -> TenantContext:
    """Resolve a bearer token to a TenantContext, or raise CatalogError on any failure.

    Failure modes mapped to a single CatalogError so the auth middleware can
    return a uniform 401 without leaking which leg failed (defense against
    enumeration). The plaintext token is never logged.
    """
    if not raw_token:
        msg = "missing token"
        raise CatalogError(msg)

    token_hash = hash_token(raw_token)
    now = clock.now()

    result = await db_session.execute(select(ApiToken).where(ApiToken.token_hash == token_hash))
    token: ApiToken | None = result.scalar_one_or_none()
    if token is None:
        msg = "token not found"
        raise CatalogError(msg)
    if token.revoked_at is not None:
        msg = "token revoked"
        raise CatalogError(msg)
    if token.expires_at is not None and token.expires_at <= now:
        msg = "token expired"
        raise CatalogError(msg)

    return TenantContext(
        tenant_id=token.tenant_id,
        actor_id=token.actor_id,
        roles=list(token.roles),
    )


__all__ = ["hash_token", "validate_token"]
