"""CatalogService — thin facade over EntityService and FactService.

The full business logic lives in the focused sub-services:

- ``service/entity.py`` — EntityService: create/get/update/delete entity,
  resolve_entity_handle, seed_default_roles.
- ``service/facts.py`` — FactService: create/update/delete fact,
  create_fact_from_sync, upsert_synced_facts, get_full_capability.
- ``service/catalog.py`` (this file) — CatalogService: edge operations
  (create_edge, delete_edge) which sit between the two sub-services and
  the thin facade delegates for everything else.

Every public method signature is byte-identical to the original so route
handlers and the MCP server continue to call ``request.app.state.catalog``
without modification. A future phase can migrate call sites to import the
specific sub-service directly.

Every cross-tenant query path that returns entity rows still funnels through
visibility / tenant-assertion guards — those live in EntityService and
FactService respectively and are delegated to here unchanged.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import NotFoundError, TenantIsolationError, ValidationError
from registry.service.entity import EntityService, _validate_semver_attribute
from registry.service.facts import FactService, _edge_to_ref
from registry.service.schema import SchemaService
from registry.service.version_predicates import validate_version_predicate
from registry.service.vocabulary import VocabularyService
from registry.storage.models import Edge, Entity
from registry.types import CapabilityRecord, Clock, EdgeRef, EntityRef, FactRef, SyncWriteResult, TenantContext
from registry.workers.closure_refresh import enqueue_closure_refresh

if TYPE_CHECKING:
    from registry.service.visibility import VisibilityService

_log = logging.getLogger(__name__)


class CatalogService:
    """Facade that preserves the original public surface while delegating to sub-services.

    Constructor accepts the same five parameters the original class did so
    ``main.py`` requires only minimal wiring changes. Internally it constructs
    EntityService and FactService and keeps them as ``_entity`` / ``_fact``.

    Edge methods (create_edge, delete_edge) remain on this class because they
    orchestrate cross-service logic (adoption check, closure-cache refresh).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        vocabulary: VocabularyService,
        schema: SchemaService,
        visibility: VisibilityService | None = None,
        # Optional DI override — pass pre-built sub-services for testing.
        entity_service: EntityService | None = None,
        fact_service: FactService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._vocabulary = vocabulary
        self._schema = schema
        self._visibility = visibility

        self._entity: EntityService = entity_service or EntityService(
            session_factory=session_factory,
            clock=clock,
            vocabulary=vocabulary,
            schema=schema,
            visibility=visibility,
        )
        self._fact: FactService = fact_service or FactService(
            session_factory=session_factory,
            clock=clock,
            vocabulary=vocabulary,
            entity_service=self._entity,
        )

    # ---- entities (delegated to EntityService) -----------------------------

    async def create_entity(
        self,
        ctx: TenantContext,
        entity_type: str,
        name: str,
        external_id: str | None = None,
        capability_type: str | None = None,
        attributes: dict[str, Any] | None = None,
        valid_from: datetime.datetime | None = None,
    ) -> EntityRef:
        return await self._entity.create_entity(
            ctx,
            entity_type=entity_type,
            name=name,
            external_id=external_id,
            capability_type=capability_type,
            attributes=attributes,
            valid_from=valid_from,
        )

    async def get_entity(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        as_of: datetime.datetime | None = None,
    ) -> EntityRef:
        return await self._entity.get_entity(ctx, entity_id, as_of=as_of)

    async def resolve_entity_handle(
        self,
        ctx: TenantContext,
        handle: str,
        as_of: datetime.datetime | None = None,
    ) -> EntityRef:
        return await self._entity.resolve_entity_handle(ctx, handle, as_of=as_of)

    async def update_entity(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        updates: dict[str, Any],
        valid_from: datetime.datetime | None = None,
    ) -> EntityRef:
        return await self._entity.update_entity(ctx, entity_id, updates, valid_from=valid_from)

    async def delete_entity(self, ctx: TenantContext, entity_id: uuid.UUID) -> None:
        return await self._entity.delete_entity(ctx, entity_id)

    async def seed_default_roles(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> None:
        return await self._entity.seed_default_roles(session, tenant_id)

    # ---- facts (delegated to FactService) ----------------------------------

    async def create_fact(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        category: str,
        body: str,
        valid_from: datetime.datetime | None = None,
        is_authoritative: bool = True,
        sync_run_id: uuid.UUID | None = None,
        title: str | None = None,
        body_format: str = "markdown",
    ) -> FactRef:
        return await self._fact.create_fact(
            ctx,
            entity_id=entity_id,
            category=category,
            body=body,
            valid_from=valid_from,
            is_authoritative=is_authoritative,
            sync_run_id=sync_run_id,
            title=title,
            body_format=body_format,
        )

    async def update_fact(
        self,
        ctx: TenantContext,
        fact_id: uuid.UUID,
        new_body: str,
        valid_from: datetime.datetime | None = None,
    ) -> FactRef:
        return await self._fact.update_fact(ctx, fact_id, new_body, valid_from=valid_from)

    async def delete_fact(self, ctx: TenantContext, fact_id: uuid.UUID) -> None:
        return await self._fact.delete_fact(ctx, fact_id)

    async def create_fact_from_sync(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        category: str,
        body: str,
        sync_run_id: uuid.UUID,
        source_id: uuid.UUID,
        valid_from: datetime.datetime | None = None,
    ) -> FactRef:
        return await self._fact.create_fact_from_sync(
            ctx,
            entity_id=entity_id,
            category=category,
            body=body,
            sync_run_id=sync_run_id,
            source_id=source_id,
            valid_from=valid_from,
        )

    async def upsert_synced_facts(
        self,
        ctx: TenantContext,
        facts: list[Any],
        sync_run_id: uuid.UUID,
        source: Any,
    ) -> SyncWriteResult:
        """Delegate to FactService bulk upsert (O(1) transactions per call)."""
        return await self._fact.upsert_synced_facts(ctx, facts, sync_run_id, source)

    # ---- aggregates (delegated to FactService) ----------------------------

    async def get_full_capability(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        as_of: datetime.datetime | None = None,
    ) -> CapabilityRecord:
        return await self._fact.get_full_capability(ctx, entity_id, as_of=as_of)

    # ---- edges (remain on CatalogService — orchestrate adoption check) ----

    async def create_edge(
        self,
        ctx: TenantContext,
        src_entity_id: uuid.UUID,
        rel: str,
        dst_entity_id: uuid.UUID,
        properties: dict[str, Any] | None = None,
        valid_from: datetime.datetime | None = None,
    ) -> EdgeRef:
        from registry.service.temporal import normalize_utc  # noqa: PLC0415

        await self._vocabulary.validate_value(ctx, "edge_rel", rel)

        # Validate edge.properties.version predicate at write-time.
        if properties is not None:
            version_pred = properties.get("version")
            if version_pred is not None:
                if not isinstance(version_pred, str) or not validate_version_predicate(version_pred):
                    msg = (
                        f"edge properties.version {version_pred!r} is not a valid semver range "
                        f"(accepted: >=2.0,<3.0 / ^1.4 / ~2.3.4 / bare version / empty string)"
                    )
                    raise ValidationError(msg)

        now = self._clock.now()
        valid_from = normalize_utc(valid_from) if valid_from is not None else now

        # Cross-tenant gate: `provides_to` may never be created directly by clients.
        # Only AdoptionService is permitted to write this edge type, because an adoption
        # event must be recorded and the consumer must have an active adoption before
        # the edge can exist.
        if rel == "provides_to":
            msg = (
                "provides_to edges may not be created directly. "
                "Use POST /v1/capabilities/{id}/adoptions to establish a cross-tenant adoption."
            )
            raise PermissionError(msg)

        edge = Edge(
            edge_id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            src_entity_id=src_entity_id,
            rel=rel,
            dst_entity_id=dst_entity_id,
            properties=properties,
            is_authoritative=True,
            sync_run_id=None,
            t_valid_from=valid_from,
            t_valid_to=None,
            t_ingested_at=now,
            t_invalidated_at=None,
            created_by=ctx.actor_id,
        )
        async with self._session_factory() as session, session.begin():
            src_entity = await session.get(Entity, src_entity_id)
            if src_entity is None or src_entity.tenant_id != ctx.tenant_id:
                msg = f"entity {src_entity_id} not found for tenant"
                raise NotFoundError(msg)
            dst_entity = await session.get(Entity, dst_entity_id)
            if dst_entity is None:
                msg = f"entity {dst_entity_id} not found"
                raise NotFoundError(msg)

            # Cross-tenant edge gate: applies when src and dst belong to different tenants.
            if dst_entity.tenant_id != ctx.tenant_id:
                if rel in ("depends_on", "requires", "integrates_with"):
                    # Visibility check: dst must be visible to the calling tenant.
                    if self._visibility is not None:
                        await self._visibility.assert_visible(ctx, dst_entity_id)

                    # Adoption check: consumer must have an active adoption event for
                    # the provider capability. Inline SQL so AdoptionService can
                    # later replace this with a proper service call.
                    result = await session.execute(
                        text(
                            "SELECT 1 FROM adoption_events "
                            "WHERE consumer_tenant_id = :consumer_tid "
                            "  AND provider_capability_id = :cap_id "
                            "  AND t_invalidated_at IS NULL "
                            "LIMIT 1"
                        ),
                        {
                            "consumer_tid": ctx.tenant_id,
                            "cap_id": dst_entity_id,
                        },
                    )
                    if result.first() is None:
                        _log.info(
                            "cross_tenant_edge_rejected rel=%s consumer=%s provider_cap=%s",
                            rel,
                            ctx.tenant_id,
                            dst_entity_id,
                        )
                        msg = (
                            f"Cross-tenant edge rejected: no active adoption event for "
                            f"capability {dst_entity_id}. "
                            f"POST /v1/capabilities/{dst_entity_id}/adoptions first."
                        )
                        raise PermissionError(msg)
                else:
                    # Any other cross-tenant rel (e.g. composes, conflicts_with) is not
                    # gated by adoption — but the dst must still exist.
                    pass

            session.add(edge)
            await session.flush()
            await enqueue_closure_refresh(session, ctx.tenant_id, edge.edge_id, now)
        return _edge_to_ref(edge)

    async def delete_edge(self, ctx: TenantContext, edge_id: uuid.UUID) -> None:
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            edge = await session.get(Edge, edge_id)
            if edge is None:
                msg = f"edge {edge_id} not found"
                raise NotFoundError(msg)
            self._assert_tenant(ctx, edge.tenant_id)
            edge.t_valid_to = now
            edge.t_invalidated_at = now
            await enqueue_closure_refresh(session, ctx.tenant_id, edge_id, now)

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _assert_tenant(ctx: TenantContext, row_tenant_id: uuid.UUID) -> None:
        if row_tenant_id != ctx.tenant_id:
            msg = "row belongs to a different tenant"
            raise TenantIsolationError(msg)


# Re-export the private helper so existing import sites that do
#   from registry.service.catalog import _validate_semver_attribute
# continue to work without modification.
__all__ = ["CatalogService", "_validate_semver_attribute"]
