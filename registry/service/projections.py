"""ProjectionService — provider and consumer graph projections.

RBAC-scoped views over the entity/edge graph. There is no separate
"projection store" — both projections are SQL-time assemblies that pass
cross-tenant entity results through ``VisibilityService.filter_entities``
to ensure only visible capabilities appear in the output.

Provider projection (what does my tenant ship?)
-----------------------------------------------
- ``nodes``: entities owned by ``ctx.tenant_id``.
- ``edges``: internal composition edges + every ``provides_to`` edge
  whose ``src_entity_id`` is one of my capabilities (these encode the
  consumers that adopted my capabilities).

Consumer projection (what does my tenant consume?)
--------------------------------------------------
- ``nodes``: own capabilities + provider capabilities adopted via an
  active ``adoption_events`` row (visibility-filtered).
- ``edges``: own outgoing ``depends_on``/``requires``/``integrates_with``
  edges + the ``provides_to`` edges of adopted provider capabilities.

Both projections use keyset pagination on (created_at DESC, entity_id DESC).
Pass an opaque ``cursor`` dict (decoded from ``api/cursor.py``); an empty dict
starts from the first page. ``next_cursor`` in the result is None when no further
pages exist.

Consumer projection — adopted cap fetch:
The adopted-cap query is SQL-limited to the number of remaining page slots
and uses a keyset cursor on (t_valid_from DESC, adoption_id DESC) so that
tenants with thousands of adoptions never load the full list into Python.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.service.visibility import VisibilityService
from registry.types import Clock, EdgeRef, EntityRef, TenantContext

_log = logging.getLogger(__name__)

_MAX_PAGE_SIZE = 500

# Edge relations a consumer projection counts as "outgoing dependencies".
_CONSUMER_DEP_RELS: tuple[str, ...] = (
    "depends_on",
    "requires",
    "integrates_with",
)

# Edge relations a provider projection counts as "internal composition".
_PROVIDER_INTERNAL_RELS: tuple[str, ...] = (
    "composes",
    "instance_of",
    "concept_of",
    "operation_of",
)


@dataclass
class Projection:
    """Result of a projection query.

    ``nodes`` and ``edges`` are the paginated slice (nodes only — edges are
    returned in full because they follow the node selection).

    ``next_cursor`` is None when no further pages exist; otherwise it is an
    opaque payload ready to pass to ``encode_cursor`` from ``api/cursor.py``.
    """

    nodes: list[EntityRef]
    edges: list[EdgeRef]
    next_cursor: dict[str, str] | None


class ProjectionService:
    """Provider + consumer projection assembly."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        visibility: VisibilityService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._visibility = visibility

    # ------------------------------------------------------------------
    # Provider projection
    # ------------------------------------------------------------------

    async def get_provider_projection(
        self,
        ctx: TenantContext,
        as_of: datetime.datetime | None = None,
        cursor: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> Projection:
        """Return the provider's view: own entities + provides_to edges out.

        Uses keyset pagination on (created_at DESC, entity_id DESC). Pass the
        decoded cursor dict from ``api/cursor.py``; None or ``{}`` starts from
        the first page.
        """
        cursor = cursor or {}
        page_size = _clamp_page_size(page_size)

        async with self._session_factory() as session:
            nodes, next_cursor = await self._fetch_own_entities_keyset(session, ctx.tenant_id, cursor, page_size)
            node_ids = [n.entity_id for n in nodes]
            internal_edges = await self._fetch_internal_edges(session, ctx.tenant_id, node_ids, _PROVIDER_INTERNAL_RELS)
            provides_edges = await self._fetch_provides_to_edges_outgoing(session, ctx.tenant_id, node_ids)

        return Projection(
            nodes=nodes,
            edges=internal_edges + provides_edges,
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------
    # Consumer projection
    # ------------------------------------------------------------------

    async def get_consumer_projection(
        self,
        ctx: TenantContext,
        as_of: datetime.datetime | None = None,
        cursor: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> Projection:
        """Return the consumer's view: own entities + adopted provider caps.

        Uses keyset pagination on (created_at DESC, entity_id DESC) for own
        entities. Pass the decoded cursor dict from ``api/cursor.py``; None or
        ``{}`` starts from the first page.

        Adopted caps fill remaining page slots. The adopted-cap fetch is
        SQL-limited to exactly the number of remaining slots using a keyset
        cursor on (t_valid_from DESC, adoption_id DESC) — tenants with large
        adoption sets never load more rows than the page requires.
        """
        cursor = cursor or {}
        page_size = _clamp_page_size(page_size)

        # Separate cursor halves: own-entity position and adopted-cap position.
        # The combined cursor encodes both so that follow-up pages resume each
        # sub-query at the right keyset boundary.
        own_cursor = {k: v for k, v in cursor.items() if not k.startswith("adp_")}
        adopted_cursor = (
            {
                "ts": cursor["adp_ts"],
                "id": cursor["adp_id"],
            }
            if "adp_ts" in cursor and "adp_id" in cursor
            else {}
        )

        async with self._session_factory() as session:
            own_nodes, own_next_cursor = await self._fetch_own_entities_keyset(
                session, ctx.tenant_id, own_cursor, page_size
            )

            # Fill remaining slots in the page from adopted caps via SQL LIMIT.
            # No Python slicing — only the needed rows are fetched.
            remaining = page_size - len(own_nodes)
            adopted_cap_ids, adopted_next_cursor = await self._fetch_adopted_provider_caps(
                session, ctx.tenant_id, limit=remaining, cursor=adopted_cursor
            )
            adopted_nodes = (
                await self._fetch_entities_by_ids_visible(session, ctx, adopted_cap_ids) if adopted_cap_ids else []
            )

            own_node_ids = [n.entity_id for n in own_nodes]
            outgoing = (
                await self._fetch_outgoing_dep_edges(session, ctx.tenant_id, own_node_ids) if own_node_ids else []
            )
            inbound_provides = (
                await self._fetch_provides_to_edges_for_caps(session, [n.entity_id for n in adopted_nodes])
                if adopted_nodes
                else []
            )

        # Build a combined next_cursor that encodes both keyset positions so
        # the next page can resume both sub-queries at the right boundary.
        next_cursor: dict[str, str] | None = None
        if own_next_cursor is not None or adopted_next_cursor is not None:
            combined: dict[str, str] = {}
            if own_next_cursor:
                combined.update(own_next_cursor)
            if adopted_next_cursor:
                combined["adp_ts"] = adopted_next_cursor["ts"]
                combined["adp_id"] = adopted_next_cursor["id"]
            next_cursor = combined if combined else None

        return Projection(
            nodes=own_nodes + adopted_nodes,
            edges=outgoing + inbound_provides,
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------
    # SQL helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _count_own_entities(session: AsyncSession, tenant_id: uuid.UUID) -> int:
        result = await session.execute(
            text("SELECT COUNT(*) FROM entities " "WHERE tenant_id = :tid AND is_active = TRUE"),
            {"tid": tenant_id},
        )
        return int(result.scalar() or 0)

    @staticmethod
    async def _fetch_own_entities_keyset(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        cursor: dict[str, Any],
        page_size: int,
    ) -> tuple[list[EntityRef], dict[str, str] | None]:
        """Fetch own entities using keyset pagination.

        Fetches ``page_size + 1`` rows; if the extra row is present the caller
        knows more pages exist and we build a ``next_cursor`` from the last
        returned row's position. Returns ``(items, next_cursor_payload)``.
        """
        if page_size <= 0:
            return [], None

        params: dict[str, Any] = {"tid": tenant_id, "lim": page_size + 1}
        keyset_clause = ""
        if cursor:
            keyset_clause = "AND (created_at, entity_id) < (:cursor_ts, :cursor_id)"
            params["cursor_ts"] = datetime.datetime.fromisoformat(cursor["ts"])
            params["cursor_id"] = cursor["id"]

        result = await session.execute(
            text(
                f"""
                SELECT entity_id, tenant_id, entity_type, name,
                       external_id, is_active, created_at
                FROM entities
                WHERE tenant_id = :tid AND is_active = TRUE
                {keyset_clause}
                ORDER BY created_at DESC, entity_id DESC
                LIMIT :lim
                """
            ),
            params,
        )
        rows = result.mappings().all()
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        items = [_row_to_entity_ref(r) for r in page_rows]

        next_cursor: dict[str, str] | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = {
                "ts": last.created_at.isoformat(),
                "id": str(last.entity_id),
            }

        return items, next_cursor

    async def _fetch_entities_by_ids_visible(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        entity_ids: list[uuid.UUID],
    ) -> list[EntityRef]:
        """Fetch entities by id, filtered through VisibilityService."""
        if not entity_ids:
            return []
        visible_ids = await self._visibility.filter_entities(ctx, entity_ids)
        if not visible_ids:
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
            {"ids": list(visible_ids)},
        )
        return [_row_to_entity_ref(r) for r in result.mappings().all()]

    @staticmethod
    async def _fetch_internal_edges(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        node_ids: list[uuid.UUID],
        rels: tuple[str, ...],
    ) -> list[EdgeRef]:
        if not node_ids:
            return []
        result = await session.execute(
            text(
                """
                SELECT edge_id, tenant_id, src_entity_id, rel, dst_entity_id,
                       properties, t_valid_from, t_valid_to,
                       t_ingested_at, t_invalidated_at
                FROM edges
                WHERE tenant_id = :tid
                  AND rel = ANY(:rels)
                  AND src_entity_id = ANY(:ids)
                  AND t_invalidated_at IS NULL
                """
            ),
            {"tid": tenant_id, "rels": list(rels), "ids": node_ids},
        )
        return [_row_to_edge_ref(r) for r in result.mappings().all()]

    @staticmethod
    async def _fetch_outgoing_dep_edges(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        node_ids: list[uuid.UUID],
    ) -> list[EdgeRef]:
        if not node_ids:
            return []
        result = await session.execute(
            text(
                """
                SELECT edge_id, tenant_id, src_entity_id, rel, dst_entity_id,
                       properties, t_valid_from, t_valid_to,
                       t_ingested_at, t_invalidated_at
                FROM edges
                WHERE tenant_id = :tid
                  AND rel = ANY(:rels)
                  AND src_entity_id = ANY(:ids)
                  AND t_invalidated_at IS NULL
                """
            ),
            {
                "tid": tenant_id,
                "rels": list(_CONSUMER_DEP_RELS),
                "ids": node_ids,
            },
        )
        return [_row_to_edge_ref(r) for r in result.mappings().all()]

    @staticmethod
    async def _fetch_provides_to_edges_outgoing(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        node_ids: list[uuid.UUID],
    ) -> list[EdgeRef]:
        """provides_to edges where src is one of my capabilities."""
        if not node_ids:
            return []
        result = await session.execute(
            text(
                """
                SELECT edge_id, tenant_id, src_entity_id, rel, dst_entity_id,
                       properties, t_valid_from, t_valid_to,
                       t_ingested_at, t_invalidated_at
                FROM edges
                WHERE tenant_id = :tid
                  AND rel = 'provides_to'
                  AND src_entity_id = ANY(:ids)
                  AND t_invalidated_at IS NULL
                """
            ),
            {"tid": tenant_id, "ids": node_ids},
        )
        return [_row_to_edge_ref(r) for r in result.mappings().all()]

    @staticmethod
    async def _fetch_provides_to_edges_for_caps(
        session: AsyncSession,
        cap_ids: list[uuid.UUID],
    ) -> list[EdgeRef]:
        """provides_to edges whose src is in *cap_ids* (cross-tenant)."""
        if not cap_ids:
            return []
        result = await session.execute(
            text(
                """
                SELECT edge_id, tenant_id, src_entity_id, rel, dst_entity_id,
                       properties, t_valid_from, t_valid_to,
                       t_ingested_at, t_invalidated_at
                FROM edges
                WHERE rel = 'provides_to'
                  AND src_entity_id = ANY(:ids)
                  AND t_invalidated_at IS NULL
                """
            ),
            {"ids": cap_ids},
        )
        return [_row_to_edge_ref(r) for r in result.mappings().all()]

    @staticmethod
    async def _fetch_adopted_provider_caps(
        session: AsyncSession,
        consumer_tenant_id: uuid.UUID,
        *,
        limit: int,
        cursor: dict[str, Any] | None = None,
    ) -> tuple[list[uuid.UUID], dict[str, str] | None]:
        """Return provider_capability_ids actively adopted by the consumer.

        Fetches exactly ``limit + 1`` rows via SQL LIMIT so the caller never
        loads more adopted-cap rows than the page requires. Keyset cursor on
        (t_valid_from DESC, adoption_id DESC) lets subsequent pages resume
        at the right boundary without scanning from the start.

        Returns ``(cap_ids, next_cursor_payload)`` where ``next_cursor_payload``
        is None when no further adopted-cap rows exist beyond this page.
        """
        if limit <= 0:
            return [], None

        params: dict[str, Any] = {"ctid": consumer_tenant_id, "lim": limit + 1}
        keyset_clause = ""
        if cursor:
            keyset_clause = "AND (t_valid_from, adoption_id) < (:cursor_ts, :cursor_id)"
            params["cursor_ts"] = datetime.datetime.fromisoformat(cursor["ts"])
            params["cursor_id"] = cursor["id"]

        result = await session.execute(
            text(
                f"""
                SELECT adoption_id, provider_capability_id, t_valid_from
                FROM adoption_events
                WHERE consumer_tenant_id = :ctid
                  AND t_invalidated_at IS NULL
                  {keyset_clause}
                ORDER BY t_valid_from DESC, adoption_id DESC
                LIMIT :lim
                """
            ),
            params,
        )
        rows = result.all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        cap_ids = [row.provider_capability_id for row in page_rows]

        next_cursor: dict[str, str] | None = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = {
                "ts": last.t_valid_from.isoformat(),
                "id": str(last.adoption_id),
            }

        return cap_ids, next_cursor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_page_size(page_size: int) -> int:
    """Clamp ``page_size`` to the range (0, _MAX_PAGE_SIZE]."""
    if page_size <= 0:
        page_size = 100
    if page_size > _MAX_PAGE_SIZE:
        page_size = _MAX_PAGE_SIZE
    return page_size


def _row_to_entity_ref(row: Any) -> EntityRef:
    return EntityRef(
        entity_id=row["entity_id"],
        tenant_id=row["tenant_id"],
        entity_type=row["entity_type"],
        name=row["name"],
        external_id=row["external_id"],
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


def _row_to_edge_ref(row: Any) -> EdgeRef:
    return EdgeRef(
        edge_id=row["edge_id"],
        tenant_id=row["tenant_id"],
        src_entity_id=row["src_entity_id"],
        rel=row["rel"],
        dst_entity_id=row["dst_entity_id"],
        properties=row["properties"],
        t_valid_from=row["t_valid_from"],
        t_valid_to=row["t_valid_to"],
        t_ingested_at=row["t_ingested_at"],
        t_invalidated_at=row["t_invalidated_at"],
    )


__all__ = [
    "Projection",
    "ProjectionService",
]
