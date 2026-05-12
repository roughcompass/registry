"""LifecycleService — alpha → beta → ga → deprecated → retired.

When ``successor`` is a UUID, the transition delegates edge creation to
``CatalogService.create_edge()`` after the attribute commit so the public
edge API (vocabulary validation, idempotent upsert) is always the write
path.  The attribute and edge commits are sequential but both scoped to
the same logical operation.

When ``successor`` is the sentinel string ``"none"`` the entity is
deprecated without a replacement.

``CatalogService`` is optional so the service remains usable in contexts
where only the attribute write is needed (e.g. tests, CLI tools).
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import LifecycleError, ValidationError
from registry.service.temporal import normalize_utc
from registry.storage.models import Attribute, Edge, Entity
from registry.types import Clock, TenantContext

# Edge rels that satisfy the integration capability composition constraint.
# An integration entity must connect to at least two other capabilities via
# ``composes`` or ``depends_on`` edges before it can leave the draft
# (``alpha``) lifecycle state.
INTEGRATION_QUALIFYING_RELS: frozenset[str] = frozenset({"composes", "depends_on"})
INTEGRATION_MIN_EDGES: int = 2

if TYPE_CHECKING:
    from registry.service.catalog import CatalogService

VALID_TRANSITIONS: dict[str, set[str]] = {
    "alpha": {"beta", "deprecated", "retired"},
    "beta": {"ga", "deprecated", "retired"},
    "ga": {"deprecated", "retired"},
    "deprecated": {"retired"},
    "retired": set(),
}


class LifecycleService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        catalog: CatalogService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._catalog = catalog

    async def transition(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        new_state: str,
        *,
        successor: uuid.UUID | Literal["none"],
        valid_from: datetime.datetime | None = None,
    ) -> None:
        """Apply a lifecycle transition. Raises LifecycleError on policy violations.

        ``successor`` encodes the three-way deprecation choice as a single
        parameter:
        - Pass a UUID to name the entity that replaces this one.
        - Pass ``"none"`` to explicitly deprecate without a successor.

        When ``successor`` is a UUID and a ``CatalogService`` was injected, the
        ``replaced_by`` edge is created via ``CatalogService.create_edge()``
        after the attribute commit.  If no ``CatalogService`` is available,
        the direct ORM write path (``_upsert_replaced_by_edge``) is used as
        fallback so the service is never a no-op.
        """
        if new_state not in VALID_TRANSITIONS:
            msg = f"unknown lifecycle state: {new_state!r}"
            raise LifecycleError(msg)

        # Resolve the typed successor value once so the rest of the method is
        # branch-free: replaced_by is either a UUID or None.
        replaced_by: uuid.UUID | None = None if successor == "none" else successor

        now = self._clock.now()
        valid_from = normalize_utc(valid_from) if valid_from is not None else now

        async with self._session_factory() as session, session.begin():
            await self._enforce_transition(session, ctx, entity_id, new_state)
            await self._enforce_integration_edge_constraint(session, ctx, entity_id, new_state)
            await self._write_attribute(session, ctx, entity_id, new_state, valid_from, now)
            if replaced_by is not None and self._catalog is None:
                # Fallback: write edge directly when CatalogService is not injected.
                await self._upsert_replaced_by_edge(session, ctx, entity_id, replaced_by, valid_from, now)

        # After the attribute commit, create the replaced_by edge via the public API.
        # This keeps vocabulary validation and the CatalogService write path intact.
        if replaced_by is not None and self._catalog is not None:
            await self._catalog.create_edge(
                ctx,
                entity_id,
                "replaced_by",
                replaced_by,
                valid_from=valid_from,
            )

    async def _enforce_transition(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        new_state: str,
    ) -> None:
        from sqlalchemy import select

        result = await session.execute(
            select(Attribute)
            .where(
                Attribute.tenant_id == ctx.tenant_id,
                Attribute.entity_id == entity_id,
                Attribute.key == "lifecycle",
                Attribute.t_invalidated_at.is_(None),
                Attribute.t_valid_to.is_(None),
            )
            .order_by(Attribute.t_valid_from.desc())
            .limit(1)
        )
        current = result.scalar_one_or_none()
        if current is None:
            # First lifecycle write — only `alpha` is a legal entry state.
            if new_state != "alpha":
                msg = f"first lifecycle state must be 'alpha', got {new_state!r}"
                raise LifecycleError(msg)
            return
        current_state = str(current.value) if not isinstance(current.value, dict) else str(current.value.get("state"))
        allowed = VALID_TRANSITIONS.get(current_state, set())
        if new_state not in allowed:
            msg = f"invalid transition: {current_state!r} -> {new_state!r}"
            raise LifecycleError(msg)
        # Close the open interval on the previous row.
        current.t_valid_to = normalize_utc(self._clock.now())

    async def _write_attribute(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        new_state: str,
        valid_from: datetime.datetime,
        now: datetime.datetime,
    ) -> None:
        session.add(
            Attribute(
                attr_id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                entity_id=entity_id,
                key="lifecycle",
                value={"state": new_state},
                t_valid_from=valid_from,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
                created_by=ctx.actor_id,
            )
        )

    async def promote_from_draft(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        new_state: str = "beta",
        valid_from: datetime.datetime | None = None,
    ) -> None:
        """Promote an entity out of the ``alpha`` (draft) state.

        Thin convenience wrapper around :meth:`transition` that documents the
        intent.  For ``entity_type='integration'`` entities, the underlying
        ``transition`` call enforces the integration edge constraint: the entity
        must have at least two active ``composes`` or ``depends_on`` outbound
        edges, otherwise :class:`ValidationError` is raised (HTTP 422).

        ``new_state`` defaults to ``beta`` since that is the canonical
        first promotion target, but any state legal from ``alpha`` is accepted.
        Promotions do not set a successor; ``successor="none"`` is passed through
        as an explicit no-op value since the successor parameter is only meaningful
        for deprecated transitions.
        """
        await self.transition(ctx, entity_id, new_state, successor="none", valid_from=valid_from)

    async def _enforce_integration_edge_constraint(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        new_state: str,
    ) -> None:
        """If the entity is an integration capability, require ≥ 2 qualifying edges.

        Fires only when leaving the ``alpha`` (draft) state — i.e. when
        ``new_state`` is one of ``beta``, ``ga``, ``deprecated``, or
        ``retired``.  Non-integration entities pass through unconditionally.

        Qualifying edges are active (open-interval) ``composes`` or
        ``depends_on`` edges with the entity as source.  Raises
        :class:`ValidationError` (mapped to HTTP 422) when the constraint
        is not met.
        """
        if new_state == "alpha":
            return

        from sqlalchemy import func, select  # noqa: PLC0415

        entity = await session.get(Entity, entity_id)
        if entity is None or entity.tenant_id != ctx.tenant_id:
            # Don't second-guess tenant isolation here — the bi-temporal
            # attribute write below would also fail.  The state-machine
            # check in _enforce_transition is the gatekeeper for missing
            # entities; this method is a no-op when the entity is absent.
            return
        if entity.entity_type != "integration":
            return

        count_result = await session.execute(
            select(func.count())
            .select_from(Edge)
            .where(
                Edge.tenant_id == ctx.tenant_id,
                Edge.src_entity_id == entity_id,
                Edge.rel.in_(tuple(INTEGRATION_QUALIFYING_RELS)),
                Edge.t_invalidated_at.is_(None),
                Edge.t_valid_to.is_(None),
            )
        )
        count = int(count_result.scalar_one() or 0)
        if count < INTEGRATION_MIN_EDGES:
            msg = (
                f"integration capability {entity_id} cannot be promoted to "
                f"{new_state!r}: requires at least {INTEGRATION_MIN_EDGES} "
                f"active 'composes' or 'depends_on' edges, found {count}"
            )
            raise ValidationError(msg)

    async def _upsert_replaced_by_edge(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        replaced_by: uuid.UUID,
        valid_from: datetime.datetime,
        now: datetime.datetime,
    ) -> None:
        from sqlalchemy import select

        result = await session.execute(
            select(Edge).where(
                Edge.tenant_id == ctx.tenant_id,
                Edge.src_entity_id == entity_id,
                Edge.rel == "replaced_by",
                Edge.t_invalidated_at.is_(None),
                Edge.t_valid_to.is_(None),
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            existing.t_valid_to = now
        properties: dict[str, Any] = {}
        session.add(
            Edge(
                edge_id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                src_entity_id=entity_id,
                rel="replaced_by",
                dst_entity_id=replaced_by,
                properties=properties,
                is_authoritative=True,
                sync_run_id=None,
                t_valid_from=valid_from,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
                created_by=ctx.actor_id,
            )
        )


__all__ = [
    "INTEGRATION_MIN_EDGES",
    "INTEGRATION_QUALIFYING_RELS",
    "LifecycleService",
    "VALID_TRANSITIONS",
]
