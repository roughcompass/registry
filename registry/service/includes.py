"""IncludeService — bounded sub-resource expansion for capability detail responses.

The four expand_* methods on this service power the ``?include=`` query
parameter on ``GET /v1/capabilities/{id}`` and the ``include`` argument on
the MCP ``get_capability`` tool.  Consolidating the logic here means both
surfaces stay in sync without duplicating the DB queries or the
visibility-chokepoint call.

Each method:
- takes a TenantContext + entity_id (the already-resolved UUID form),
- enforces the visibility chokepoint (filter_entities) where applicable,
- returns the Pydantic response-model shape directly so callers can attach
  it to the response without a separate mapping step,
- truncates at *cap* (default 200) and signals overflow via truncated=True
  + a ``next`` URL pointing at the dedicated endpoint.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from registry.api.schemas import (
    EntityCollectionExpansion,
    ExternalIdItem,
    ExternalIdsExpansion,
    IncludedEntityItem,
    InterfaceExpansion,
)
from registry.exceptions import CatalogError
from registry.service.visibility import VisibilityService
from registry.types import TenantContext

_log = logging.getLogger(__name__)

# Per-include result cap.  When the result set hits this, the response carries
# truncated=True plus a ``next`` URL pointing at the dedicated endpoint.
# Raised to 200 in ERG-T07 to cover real-world fan-out (design systems
# with 100+ components, services with 50+ dependencies).
_INCLUDE_CAP: int = 200


class IncludeService:
    """Expand bounded sub-resources for a capability detail response.

    Dependencies are injected at construction time so each method is purely a
    coroutine with no global-state reads.

    Args:
        session_factory: SQLAlchemy async session factory — used by
            expand_components / expand_depends_on / expand_external_ids.
        visibility: VisibilityService — used to filter_entities so every
            entity in the expansion passes the tenant-isolation chokepoint.
        interface_storage: InterfaceStorageService — used by expand_interface
            to fetch the canonical surface and raw source.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,  # type: ignore[type-arg]
        visibility: VisibilityService,
        interface_storage: Any,  # InterfaceStorageService — avoid circular import
    ) -> None:
        self._session_factory = session_factory
        self._visibility = visibility
        self._interface_storage = interface_storage

    # ------------------------------------------------------------------
    # Entity-collection expansions (components + depends_on)
    # ------------------------------------------------------------------

    async def expand_components(
        self,
        ctx: TenantContext,
        entity_id: object,
        *,
        handle_for_next: str,
        cap: int = _INCLUDE_CAP,
    ) -> EntityCollectionExpansion:
        """Expand the ``composes`` out-edges of *entity_id* into entity items.

        Returns an EntityCollectionExpansion capped at *cap*.  When the full
        result set exceeds the cap, ``truncated=True`` and ``next`` points at
        the dependencies endpoint for the complete list.
        """
        return await self._expand_entity_collection(
            ctx,
            entity_id,
            rel="composes",
            handle_for_next=handle_for_next,
            cap=cap,
        )

    async def expand_depends_on(
        self,
        ctx: TenantContext,
        entity_id: object,
        *,
        handle_for_next: str,
        cap: int = _INCLUDE_CAP,
    ) -> EntityCollectionExpansion:
        """Expand the ``depends_on`` out-edges of *entity_id* into entity items.

        Returns an EntityCollectionExpansion capped at *cap*.  When the full
        result set exceeds the cap, ``truncated=True`` and ``next`` points at
        the dependencies endpoint for the complete list.
        """
        return await self._expand_entity_collection(
            ctx,
            entity_id,
            rel="depends_on",
            handle_for_next=handle_for_next,
            cap=cap,
        )

    async def _expand_entity_collection(
        self,
        ctx: TenantContext,
        src_entity_id: object,
        rel: str,
        handle_for_next: str,
        cap: int = _INCLUDE_CAP,
    ) -> EntityCollectionExpansion:
        """Fetch entities reachable via outgoing edges of *rel*; visibility-filter; load attrs.

        Internal helper shared by expand_components and expand_depends_on.
        Fetches one more than *cap* so truncation can be signalled correctly,
        then passes surviving IDs through the visibility chokepoint.
        """
        from sqlalchemy import select as sa_select  # noqa: PLC0415

        from registry.storage.models import Attribute, Edge, Entity  # noqa: PLC0415

        fetch_limit = cap + 1
        async with self._session_factory() as session:
            rows = await session.execute(
                sa_select(Edge.dst_entity_id)
                .where(
                    Edge.tenant_id == ctx.tenant_id,
                    Edge.src_entity_id == src_entity_id,
                    Edge.rel == rel,
                    Edge.t_invalidated_at.is_(None),
                    Edge.t_valid_to.is_(None),
                )
                .order_by(Edge.t_valid_from)
                .limit(fetch_limit)
            )
            dst_ids: list[object] = [r[0] for r in rows.all()]

        truncated = len(dst_ids) > cap
        if truncated:
            dst_ids = dst_ids[:cap]

        # Every cross-tenant query must pass through filter_entities.
        # Bypassing this call is how data leaks between tenants happen.
        visible_ids = await self._visibility.filter_entities(ctx, dst_ids)  # type: ignore[arg-type]
        if not visible_ids:
            return EntityCollectionExpansion(items=[], truncated=truncated, next=None)

        async with self._session_factory() as session:
            entity_rows = (
                (await session.execute(sa_select(Entity).where(Entity.entity_id.in_(visible_ids)))).scalars().all()
            )
            attr_rows = (
                (
                    await session.execute(
                        sa_select(Attribute).where(
                            Attribute.entity_id.in_(visible_ids),
                            Attribute.t_invalidated_at.is_(None),
                            Attribute.t_valid_to.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

        attrs_by_entity: dict[object, dict[str, object]] = {}
        for a in attr_rows:
            attrs_by_entity.setdefault(a.entity_id, {})[a.key] = a.value

        entity_by_id = {e.entity_id: e for e in entity_rows}
        items: list[IncludedEntityItem] = []
        for eid in visible_ids:
            e = entity_by_id.get(eid)
            if e is None:
                continue
            items.append(
                IncludedEntityItem(
                    entity_id=e.entity_id,
                    tenant_id=e.tenant_id,
                    entity_type=e.entity_type,
                    name=e.name,
                    external_id=e.external_id,
                    is_active=e.is_active,
                    created_at=e.created_at,
                    attributes=attrs_by_entity.get(eid, {}),
                )
            )

        next_url = f"/v1/capabilities/{handle_for_next}/dependencies?depth=1" if truncated else None
        return EntityCollectionExpansion(items=items, truncated=truncated, next=next_url)

    # ------------------------------------------------------------------
    # External IDs expansion
    # ------------------------------------------------------------------

    async def expand_external_ids(
        self,
        ctx: TenantContext,
        entity_id: object,
        cap: int = _INCLUDE_CAP,
    ) -> ExternalIdsExpansion:
        """Fetch entity_external_ids rows for *entity_id*.

        Results are ordered by (external_system_slug, external_id) and capped
        at *cap*.  Overflow is signalled via ``truncated=True``; no ``next``
        URL is emitted (inline pagination on ``?include=`` is deferred).
        """
        from sqlalchemy import text  # noqa: PLC0415

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT external_system_slug, external_id, url, metadata_jsonb "
                        "FROM entity_external_ids "
                        "WHERE tenant_id = :tid AND entity_id = :eid "
                        "ORDER BY external_system_slug, external_id "
                        "LIMIT :lim"
                    ),
                    {"tid": ctx.tenant_id, "eid": entity_id, "lim": cap + 1},
                )
            ).all()
        truncated = len(rows) > cap
        items = [
            ExternalIdItem(
                external_system_slug=r[0],
                external_id=r[1],
                url=r[2],
                metadata=r[3],
            )
            for r in rows[:cap]
        ]
        return ExternalIdsExpansion(items=items, truncated=truncated)

    # ------------------------------------------------------------------
    # Interface expansion
    # ------------------------------------------------------------------

    async def expand_interface(
        self,
        ctx: TenantContext,
        entity_id: object,
        as_of: object | None = None,
    ) -> InterfaceExpansion:
        """Fetch the latest interface surface for *entity_id*.

        Delegates to InterfaceStorageService.get_interface.  Returns an empty
        InterfaceExpansion (all fields None) when no surface is registered or
        the lookup raises a CatalogError (e.g. not found / no interface yet).
        """
        try:
            record = await self._interface_storage.get_interface(ctx, entity_id, as_of=as_of)
        except CatalogError:
            return InterfaceExpansion(surface=None, raw=None, format=None, version=None)
        if record is None or record.interface_canonical is None:
            return InterfaceExpansion(surface=None, raw=None, format=None, version=None)

        canonical = dataclasses.asdict(record.interface_canonical)
        return InterfaceExpansion(
            surface=canonical,
            raw=record.interface_source,
            format=record.interface_format,
            version=canonical.get("version"),
        )
