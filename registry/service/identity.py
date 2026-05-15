"""Identity resolution — shared payload assembly for whoami surfaces.

Both the REST ``GET /v1/whoami`` handler and the MCP ``whoami`` tool need
the same payload shape: actor + tenant identity plus the role set the
caller holds for the selected tenant. This module assembles it from two
selects (Actor, Tenant). The wire format's ``token_id`` /
``token_expires_at`` fields are preserved for response-shape stability
but are always ``None`` — no auth path populates them.

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

from registry.storage.models import Actor, Tenant
from registry.types import TenantContext


@dataclass
class WhoamiPayload:
    """Typed intermediate representation of the whoami payload.

    Callers convert this to their own wire format (Pydantic model, JSON
    dict, …). The ``token_id`` / ``token_expires_at`` fields are
    preserved for response-shape stability but are always ``None`` —
    no auth path populates them.
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
    """Assemble the whoami payload from Actor + Tenant selects.

    Both lookups are nullable — the wire format gracefully reports
    blank fields when the rows are absent. Token fields are always
    ``None`` — no auth path populates them.
    """
    async with session_factory() as session:
        actor = (
            await session.execute(select(Actor).where(Actor.actor_id == ctx.actor_id))
        ).scalar_one_or_none()

        tenant = (
            await session.execute(select(Tenant).where(Tenant.tenant_id == ctx.tenant_id))
        ).scalar_one_or_none()

    return WhoamiPayload(
        tenant_id=ctx.tenant_id,
        tenant_slug=tenant.slug if tenant else "",
        tenant_display_name=tenant.display_name if tenant else "",
        actor_id=ctx.actor_id,
        actor_display_name=actor.display_name if actor else None,
        actor_email=actor.email if actor else None,
        token_id=None,
        token_expires_at=None,
        roles=list(ctx.roles),
    )


__all__ = ["WhoamiPayload", "resolve_whoami"]
