"""Identity resolution — shared payload assembly for whoami surfaces.

Both the REST ``GET /v1/whoami`` handler and the MCP ``whoami`` tool need
the same three-select payload (Actor, Tenant, ApiToken) serialised into the
same nine-field shape.  This module is the single source of truth so a new
field is added in one place and both wire formats pick it up automatically.

Serialisation is intentionally left to the callers:
- REST: adapts ``WhoamiPayload`` into ``WhoAmIResponse`` (Pydantic) and
  appends ``_links`` (HTTP-shape concern).
- MCP: serialises ``WhoamiPayload`` to ``json.dumps(dict)`` (MCP-shape
  concern).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.storage.models import Actor, ApiToken, Tenant
from registry.types import TenantContext


@dataclass
class WhoamiPayload:
    """Typed intermediate representation of the whoami payload.

    Callers convert this to their own wire format (Pydantic model, JSON
    dict, …).  The fields here are the canonical nine-field contract;
    adding a tenth field to this dataclass automatically propagates to
    every caller.
    """

    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_display_name: str
    actor_id: uuid.UUID
    actor_display_name: str | None
    actor_email: str | None
    token_id: uuid.UUID | None
    token_expires_at: datetime.datetime | None
    roles: list[str]


async def resolve_whoami(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
) -> WhoamiPayload:
    """Assemble the whoami payload from three sequential selects.

    Fetches Actor, Tenant, and the most-recent non-revoked ApiToken for the
    calling actor within the calling tenant.  All three lookups are nullable
    — a valid token can resolve even if the actor row was soft-deleted; the
    callers handle ``None`` fields gracefully.

    Args:
        session_factory: Async session maker — the same factory the REST
            request and MCP tool already hold.
        ctx: Tenant context resolved by the auth layer for the current
            request.  Never constructed by this function.

    Returns:
        A ``WhoamiPayload`` populated from the three rows.  Fields that
        depend on rows that may not exist (actor, token) are ``None`` when
        the row is absent.
    """
    async with session_factory() as session:
        actor = (await session.execute(select(Actor).where(Actor.actor_id == ctx.actor_id))).scalar_one_or_none()

        tenant = (await session.execute(select(Tenant).where(Tenant.tenant_id == ctx.tenant_id))).scalar_one_or_none()

        token_row: ApiToken | None = (
            await session.execute(
                select(ApiToken)
                .where(
                    ApiToken.tenant_id == ctx.tenant_id,
                    ApiToken.actor_id == ctx.actor_id,
                    ApiToken.revoked_at.is_(None),
                )
                .order_by(ApiToken.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    return WhoamiPayload(
        tenant_id=ctx.tenant_id,
        tenant_slug=tenant.slug if tenant else "",
        tenant_display_name=tenant.display_name if tenant else "",
        actor_id=ctx.actor_id,
        actor_display_name=actor.display_name if actor else None,
        actor_email=actor.email if actor else None,
        token_id=token_row.token_id if token_row else None,
        token_expires_at=token_row.expires_at if token_row else None,
        roles=list(ctx.roles),
    )


__all__ = ["WhoamiPayload", "resolve_whoami"]
