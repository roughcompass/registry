"""VisibilityService — single cross-tenant chokepoint.

Every query path that returns entity data MUST pass through this module.
Raw-SQL paths that return entity rows without calling ``filter_entities``
or ``assert_visible`` are prohibited; ``test_cross_tenant_isolation.py``
in the integration suite enforces this invariant on every PR.

Visibility vocabulary (closed, enforced by CHECK constraint on entities.visibility):
- ``private``         — owner tenant only.
- ``tenant-shared``   — owner tenant + tenants listed in
                        ``attributes.shared_with_tenants``.
- ``public``          — all tenants in the fabric.

``set_visibility`` writes the ``visibility`` column directly on the ``Entity``
row and, for ``tenant-shared``, upserts the ``shared_with_tenants`` attribute
via bi-temporal supersession (same pattern as ``CatalogService.update_entity``).
This keeps the ACL co-located with the entity in the attributes table and
avoids a separate ACL table.

Chokepoint discipline
---------------------
``service/temporal.py`` returns predicate *fragments* only and never emits
entity-touching queries itself.  ``service/visibility.py`` is the ONLY place
outside ``service/catalog.py`` and ``service/retrieval.py`` that may issue
SELECT statements against ``entities`` or ``attributes``.  Any new service
module that needs to evaluate visibility must call into this module — it must
not copy the visibility logic inline.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import NotFoundError, ValidationError
from registry.storage.models import Attribute, Entity
from registry.types import Clock, TenantContext

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Closed vocabulary — matches CHECK constraint on entities.visibility
# ---------------------------------------------------------------------------

VISIBILITY_PRIVATE = "private"
VISIBILITY_TENANT_SHARED = "tenant-shared"
VISIBILITY_PUBLIC = "public"

_VALID_VISIBILITY: frozenset[str] = frozenset({VISIBILITY_PRIVATE, VISIBILITY_TENANT_SHARED, VISIBILITY_PUBLIC})

_SHARED_WITH_TENANTS_KEY = "shared_with_tenants"


class VisibilityService:
    """Cross-tenant visibility enforcement — single chokepoint for the API.

    Parameters
    ----------
    session_factory:
        The same ``async_sessionmaker`` injected into every other service.
    clock:
        UTC clock; used to timestamp bi-temporal attribute writes in
        ``set_visibility``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def filter_entities(
        self,
        ctx: TenantContext,
        entity_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """Return the subset of *entity_ids* visible to ``ctx.tenant_id``.

        Result is a strict subset (or equal set) of *entity_ids* in the
        **same order** as the input; invisible IDs are silently dropped.

        Visibility rules
        ----------------
        - ``private``           → only the owning tenant.
        - ``tenant-shared``     → owning tenant + tenants listed in the
                                  current ``shared_with_tenants`` attribute.
        - ``public``            → all tenants.

        Raw-SQL bypass of this method is prohibited — leaks between
        tenants are how cross-tenant data exposure happens, so every
        entity-returning query path must funnel here.
        """
        if not entity_ids:
            return []

        async with self._session_factory() as session:
            entities = await self._fetch_entities(session, entity_ids)

        entity_map: dict[uuid.UUID, Entity] = {e.entity_id: e for e in entities}

        async with self._session_factory() as session:
            acl_map = await self._fetch_shared_with_tenants(session, entity_ids)

        visible: list[uuid.UUID] = []
        for eid in entity_ids:
            entity = entity_map.get(eid)
            if entity is None:
                # Unknown entity — silently excluded (not an authorization leak).
                continue
            if self._is_visible(ctx, entity, acl_map.get(eid, [])):
                visible.append(eid)

        _log.debug(
            "filter_entities: caller=%s checked=%d visible=%d",
            ctx.tenant_id,
            len(entity_ids),
            len(visible),
        )
        return visible

    async def assert_visible(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
    ) -> None:
        """Raise ``PermissionError`` (→ HTTP 403) if *entity_id* is not visible.

        Also raises ``NotFoundError`` if the entity does not exist at all,
        so callers do not learn of the existence of private entities that
        belong to other tenants.
        """
        async with self._session_factory() as session:
            entities = await self._fetch_entities(session, [entity_id])
            if not entities:
                msg = f"entity {entity_id} not found"
                raise NotFoundError(msg)
            entity = entities[0]
            acl = await self._fetch_shared_with_tenants_one(session, entity_id)

        if not self._is_visible(ctx, entity, acl):
            _log.info(
                "assert_visible: denied entity=%s caller=%s owner=%s visibility=%s",
                entity_id,
                ctx.tenant_id,
                entity.tenant_id,
                entity.visibility,
            )
            msg = (
                f"entity {entity_id} is not visible to tenant {ctx.tenant_id}. "
                f"The owner tenant must set visibility to 'tenant-shared' or "
                f"'public' and include your tenant in shared_with_tenants."
            )
            raise PermissionError(msg)

    async def set_visibility(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        visibility: str,
        shared_with_tenants: list[uuid.UUID] | None = None,
    ) -> None:
        """Update the visibility of an entity owned by ``ctx.tenant_id``.

        Constraints
        -----------
        - Caller must own the entity (enforced by SELECT WHERE tenant_id = ctx.tenant_id).
        - *visibility* must be one of the closed vocabulary values.
        - ``tenant-shared`` requires *shared_with_tenants* to be non-empty.
        - Writes ``entities.visibility`` directly (not bi-temporal; visibility
          is a current-state column not tracked in attributes).
        - Writes ``attributes.shared_with_tenants`` via bi-temporal supersession
          (closes old row, inserts new row) so the ACL is auditable over time.
        """
        _validate_visibility_input(visibility, shared_with_tenants)

        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            # Ownership check: only the owning tenant may change visibility.
            result = await session.execute(
                select(Entity).where(
                    Entity.entity_id == entity_id,
                    Entity.tenant_id == ctx.tenant_id,
                )
            )
            entity = result.scalar_one_or_none()
            if entity is None:
                msg = f"entity {entity_id} not found for tenant {ctx.tenant_id}"
                raise NotFoundError(msg)

            # Update the visibility column.
            entity.visibility = visibility

            # Manage shared_with_tenants attribute (bi-temporal supersession).
            await _upsert_shared_with_tenants(
                session=session,
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                entity_id=entity_id,
                shared_with_tenants=shared_with_tenants,
                now=now,
            )

        _log.info(
            "set_visibility: entity=%s tenant=%s visibility=%s shared_with=%s",
            entity_id,
            ctx.tenant_id,
            visibility,
            [str(t) for t in (shared_with_tenants or [])],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_visible(
        ctx: TenantContext,
        entity: Entity,
        acl: list[uuid.UUID],
    ) -> bool:
        """Pure visibility predicate — no I/O."""
        if entity.visibility == VISIBILITY_PUBLIC:
            return True
        if entity.tenant_id == ctx.tenant_id:
            # Owning tenant always sees their own entity.
            return True
        if entity.visibility == VISIBILITY_TENANT_SHARED:
            return ctx.tenant_id in acl
        # private (or unknown) — only own tenant, which was checked above.
        return False

    @staticmethod
    async def _fetch_entities(
        session: AsyncSession,
        entity_ids: list[uuid.UUID],
    ) -> list[Entity]:
        result = await session.execute(select(Entity).where(Entity.entity_id.in_(entity_ids)))
        return list(result.scalars().all())

    @staticmethod
    async def _fetch_shared_with_tenants(
        session: AsyncSession,
        entity_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """Return ``{entity_id: [tenant_uuid, ...]}`` for all *entity_ids*."""
        result = await session.execute(
            select(Attribute).where(
                Attribute.entity_id.in_(entity_ids),
                Attribute.key == _SHARED_WITH_TENANTS_KEY,
                Attribute.t_invalidated_at.is_(None),
                Attribute.t_valid_to.is_(None),
            )
        )
        out: dict[uuid.UUID, list[uuid.UUID]] = {}
        for attr in result.scalars().all():
            out[attr.entity_id] = _parse_shared_with_tenants(attr.value)
        return out

    @staticmethod
    async def _fetch_shared_with_tenants_one(
        session: AsyncSession,
        entity_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        result = await session.execute(
            select(Attribute).where(
                Attribute.entity_id == entity_id,
                Attribute.key == _SHARED_WITH_TENANTS_KEY,
                Attribute.t_invalidated_at.is_(None),
                Attribute.t_valid_to.is_(None),
            )
        )
        attr = result.scalar_one_or_none()
        if attr is None:
            return []
        return _parse_shared_with_tenants(attr.value)


# ---------------------------------------------------------------------------
# Module-level helpers (not instance methods — easier to unit-test in isolation)
# ---------------------------------------------------------------------------


def _validate_visibility_input(
    visibility: str,
    shared_with_tenants: list[uuid.UUID] | None,
) -> None:
    """Raise ``ValidationError`` for invalid inputs."""
    if visibility not in _VALID_VISIBILITY:
        msg = f"invalid visibility {visibility!r}. " f"Must be one of: {sorted(_VALID_VISIBILITY)!r}."
        raise ValidationError(msg)
    if visibility == VISIBILITY_TENANT_SHARED and not shared_with_tenants:
        msg = (
            "'tenant-shared' visibility requires a non-empty shared_with_tenants list. "
            "Provide the UUIDs of tenants that should have read access."
        )
        raise ValidationError(msg)


def _parse_shared_with_tenants(value: Any) -> list[uuid.UUID]:
    """Convert the JSONB attribute value to a list of UUIDs.

    The stored value is a JSON array of UUID strings, e.g.:
    ``["550e8400-e29b-41d4-a716-446655440000"]``.
    Non-string or unparseable entries are silently skipped (defensive read).
    """
    if not isinstance(value, list):
        return []
    result: list[uuid.UUID] = []
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            result.append(uuid.UUID(item))
        except ValueError:
            _log.warning("shared_with_tenants: skipping unparseable UUID %r", item)
    return result


async def _upsert_shared_with_tenants(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID,
    shared_with_tenants: list[uuid.UUID] | None,
    now: Any,
) -> None:
    """Bi-temporal supersession of the shared_with_tenants attribute.

    Closes any open attribute row for this key, then inserts a new row if
    *shared_with_tenants* is not None.  Passing ``None`` only closes the
    old row without creating a new one (used when switching away from
    ``tenant-shared`` to ``private`` or ``public``).
    """
    # Close any open row.
    existing = await session.execute(
        select(Attribute).where(
            Attribute.tenant_id == tenant_id,
            Attribute.entity_id == entity_id,
            Attribute.key == _SHARED_WITH_TENANTS_KEY,
            Attribute.t_invalidated_at.is_(None),
            Attribute.t_valid_to.is_(None),
        )
    )
    old = existing.scalar_one_or_none()
    if old is not None:
        old.t_valid_to = now

    if shared_with_tenants is not None:
        new_value = [str(t) for t in shared_with_tenants]
        session.add(
            Attribute(
                attr_id=uuid.uuid4(),
                tenant_id=tenant_id,
                entity_id=entity_id,
                key=_SHARED_WITH_TENANTS_KEY,
                value=new_value,
                t_valid_from=now,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
                created_by=actor_id,
            )
        )


__all__ = [
    "VisibilityService",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_TENANT_SHARED",
    "VISIBILITY_PUBLIC",
    "_VALID_VISIBILITY",
    "_validate_visibility_input",
    "_parse_shared_with_tenants",
]
