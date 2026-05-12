"""IntegrationLookupService — pair-discoverability lookup for integration capabilities.

Surfaces the denormalized ``integration_pairs`` index as a public
read-only lookup:

  "What integrations connect capability A to capability B?"

Implementation note
-------------------
The trigger that populates ``integration_pairs`` (migration 0009)
fires per-edge, so a single integration with two member edges yields
two rows: one for ``(integration, member_A)`` and one for ``(integration,
member_B)``. The lookup therefore self-joins to find integrations that
appear paired with **both** queried capabilities.

Visibility is enforced at the service layer: the trigger writes without
a visibility filter; this service filters the candidate integration IDs
through :class:`VisibilityService` before returning entity refs. This
keeps the cross-tenant chokepoint in one place rather than embedded in
the trigger.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.service.visibility import VisibilityService
from registry.types import EntityRef, TenantContext

_log = logging.getLogger(__name__)


class IntegrationLookupService:
    """Pair-discoverability lookup for integration capabilities."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        visibility: VisibilityService,
    ) -> None:
        self._session_factory = session_factory
        self._visibility = visibility

    async def find_integrations_connecting(
        self,
        ctx: TenantContext,
        cap_a_id: uuid.UUID,
        cap_b_id: uuid.UUID,
    ) -> list[EntityRef]:
        """Return integrations that touch both *cap_a_id* and *cap_b_id*.

        Pair shape: the trigger writes rows where one of ``capability_a_id``
        / ``capability_b_id`` is the integration's entity id and the
        other is the touched member; this self-join finds integrations
        with at least one member-edge to each queried capability.
        """
        async with self._session_factory() as session:
            candidates = await self._fetch_candidate_integration_ids(session, cap_a_id, cap_b_id)
            if not candidates:
                return []
            visible = await self._visibility.filter_entities(ctx, candidates)
            if not visible:
                return []
            return await self._fetch_entity_refs(session, visible)

    @staticmethod
    async def _fetch_candidate_integration_ids(
        session: AsyncSession,
        cap_a_id: uuid.UUID,
        cap_b_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """Self-join ``integration_pairs`` to find integrations linking A and B.

        Pre-filter both halves of the join by "the *other* column equals the
        queried cap"; the trigger stores integration_entity_id in one of
        ``capability_a_id`` / ``capability_b_id`` (the canonical ordering),
        so we look at both.
        """
        result = await session.execute(
            text(
                """
                SELECT DISTINCT p1.integration_entity_id
                FROM integration_pairs p1
                JOIN integration_pairs p2
                  ON p2.integration_entity_id = p1.integration_entity_id
                 AND p2.tenant_id = p1.tenant_id
                WHERE (
                        (p1.capability_a_id = :a AND p1.capability_b_id =
                            p1.integration_entity_id)
                     OR (p1.capability_b_id = :a AND p1.capability_a_id =
                            p1.integration_entity_id)
                      )
                  AND (
                        (p2.capability_a_id = :b AND p2.capability_b_id =
                            p2.integration_entity_id)
                     OR (p2.capability_b_id = :b AND p2.capability_a_id =
                            p2.integration_entity_id)
                      )
                """
            ),
            {"a": cap_a_id, "b": cap_b_id},
        )
        return [row.integration_entity_id for row in result.all()]

    @staticmethod
    async def _fetch_entity_refs(session: AsyncSession, entity_ids: list[uuid.UUID]) -> list[EntityRef]:
        if not entity_ids:
            return []
        result = await session.execute(
            text(
                """
                SELECT entity_id, tenant_id, entity_type, name,
                       external_id, is_active, created_at
                FROM entities
                WHERE entity_id = ANY(:ids) AND is_active = TRUE
                ORDER BY created_at DESC, entity_id
                """
            ),
            {"ids": entity_ids},
        )
        return [
            EntityRef(
                entity_id=row["entity_id"],
                tenant_id=row["tenant_id"],
                entity_type=row["entity_type"],
                name=row["name"],
                external_id=row["external_id"],
                is_active=row["is_active"],
                created_at=row["created_at"],
            )
            for row in result.mappings().all()
        ]


__all__ = ["IntegrationLookupService"]
