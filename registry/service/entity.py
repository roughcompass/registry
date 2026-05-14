"""EntityService — CRUD for entity rows, attributes, and slug/UUID resolution.

Handles bi-temporal attribute writes: every `update_entity` call closes the
previous open attribute row and inserts a new one in the same transaction.
`delete_entity` cascades the soft-delete to attributes, facts, and edges.

`seed_default_roles` is grouped here because role seeding is part of tenant
initialisation, which is tightly coupled to entity/tenant provisioning logic.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.auth.context import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER
from registry.exceptions import NotFoundError, TenantIsolationError, ValidationError
from registry.service.progression import ProgressionService
from registry.service.schema import SchemaService
from registry.service.temporal import normalize_utc
from registry.service.vocabulary import VocabularyService
from registry.storage.models import Attribute, Edge, Entity, Fact
from registry.types import Clock, EntityRef, TenantContext

if TYPE_CHECKING:
    from registry.service.visibility import VisibilityService

_log = logging.getLogger(__name__)


@dataclass
class _ProgressionEntityView:
    """Lightweight entity view passed into ProgressionService.validate_transition.

    Carries only the fields the progression rule engine needs: identity,
    type, and the post-update merged attribute dict. This avoids passing
    the ORM Entity row (which has no loaded attributes dict) into the
    progression service while keeping the interface clean.
    """

    entity_id: uuid.UUID
    entity_type: str
    attributes: dict[str, Any]


def _validate_semver_attribute(attributes: dict[str, Any]) -> None:
    """Enforce semver 2.0.0 on `attributes['version']`.

    If `attributes` contains a non-None `version` key, parse it with
    `semver.Version.parse()`. Pre-release (``-alpha.1``) and build metadata
    (``+sha.deadbeef``) suffixes are accepted. Invalid values raise
    :class:`ValidationError` (mapped to HTTP 422 by the API layer) with the
    message form ``"'<value>' is not valid semver 2.0.0. ...``.
    """
    import semver  # noqa: PLC0415

    value = attributes.get("version")
    if value is None:
        return
    if not isinstance(value, str):
        msg = f"{value!r} is not valid semver 2.0.0. " f"Example: '2.4.1', '3.0.0-alpha.1'."
        raise ValidationError(msg)
    try:
        semver.Version.parse(value)
    except (ValueError, TypeError) as exc:
        msg = f"{value!r} is not valid semver 2.0.0. " f"Example: '2.4.1', '3.0.0-alpha.1'."
        raise ValidationError(msg) from exc


def _entity_to_ref(e: Entity) -> EntityRef:
    return EntityRef(
        entity_id=e.entity_id,
        tenant_id=e.tenant_id,
        entity_type=e.entity_type,
        name=e.name,
        external_id=e.external_id,
        is_active=e.is_active,
        created_at=e.created_at,
    )


class EntityService:
    """Focused service for entity, attribute, and role operations.

    Owns: `create_entity`, `get_entity`, `update_entity`, `delete_entity`,
    `resolve_entity_handle`, `seed_default_roles`.

    The session factory, clock, vocabulary, and schema dependencies mirror the
    constructor shape of the original god-service so DI wiring stays identical.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        vocabulary: VocabularyService,
        schema: SchemaService,
        visibility: VisibilityService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._vocabulary = vocabulary
        self._schema = schema
        self._visibility = visibility

    # ---- entity CRUD -------------------------------------------------------

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
        from registry.service.slugs import validate_slug  # noqa: PLC0415

        validate_slug(name, field="entity name")
        attributes = attributes or {}
        _validate_semver_attribute(attributes)
        await self._vocabulary.validate_value(ctx, "entity_type", entity_type)
        if capability_type is not None:
            await self._schema.validate_capability(ctx, capability_type, attributes)

        now = self._clock.now()
        valid_from = normalize_utc(valid_from) if valid_from is not None else now
        entity = Entity(
            entity_id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            entity_type=entity_type,
            name=name,
            external_id=external_id,
            is_active=True,
            created_at=now,
            created_by=ctx.actor_id,
        )

        async with self._session_factory() as session, session.begin():
            session.add(entity)
            await session.flush()
            for key, value in attributes.items():
                session.add(
                    Attribute(
                        attr_id=uuid.uuid4(),
                        tenant_id=ctx.tenant_id,
                        entity_id=entity.entity_id,
                        key=key,
                        value=value,
                        t_valid_from=valid_from,
                        t_valid_to=None,
                        t_ingested_at=now,
                        t_invalidated_at=None,
                        created_by=ctx.actor_id,
                    )
                )

        return _entity_to_ref(entity)

    async def get_entity(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        as_of: datetime.datetime | None = None,
    ) -> EntityRef:
        # Visibility-aware read: a `public` entity is reachable by any tenant,
        # a `tenant-shared` entity is reachable by tenants in its ACL, and a
        # `private` entity is reachable only by its owner tenant. The
        # visibility chokepoint (service/visibility.py) is the single source of
        # truth — do NOT add an `Entity.tenant_id == ctx.tenant_id` filter
        # here. That filter would hide every cross-tenant adoption /
        # subscription / interface lookup and re-introduce the regression
        # tracked by tests/integration/test_cross_tenant_isolation.py.
        async with self._session_factory() as session:
            result = await session.execute(select(Entity).where(Entity.entity_id == entity_id))
            entity = result.scalar_one_or_none()
        if entity is None:
            msg = f"entity {entity_id} not found"
            raise NotFoundError(msg)
        # assert_visible raises NotFoundError for missing rows and
        # PermissionError (→ 403) when the row exists but is not visible to
        # the caller. Both outcomes correctly hide private cross-tenant rows.
        # Some test contexts construct CatalogService without a
        # VisibilityService — fall back to the tenant-only check so those
        # paths remain functional. Production code paths always inject a
        # VisibilityService via main.py.
        if self._visibility is not None:
            await self._visibility.assert_visible(ctx, entity_id)
        elif entity.tenant_id != ctx.tenant_id:
            msg = f"entity {entity_id} not found"
            raise NotFoundError(msg)
        return _entity_to_ref(entity)

    async def resolve_entity_handle(
        self,
        ctx: TenantContext,
        handle: str,
        as_of: datetime.datetime | None = None,
    ) -> EntityRef:
        """Resolve a UUID OR a slug-form name to an EntityRef within the tenant.

        Order: try UUID parse first, fall through to (tenant_id, lower(name))
        lookup. NotFoundError if neither resolves. ValidationError if the
        non-UUID form isn't a valid slug — that's a 422, not a 404, so
        clients don't confuse "bad input" with "doesn't exist".
        """
        from registry.service.slugs import validate_slug  # noqa: PLC0415

        try:
            eid = uuid.UUID(handle)
        except ValueError:
            validate_slug(handle, field="capability handle")
            async with self._session_factory() as session:
                result = await session.execute(
                    select(Entity).where(
                        Entity.tenant_id == ctx.tenant_id,
                        func.lower(Entity.name) == handle.lower(),
                    )
                )
                entity = result.scalar_one_or_none()
            if entity is None:
                msg = f"entity with name {handle!r} not found"
                raise NotFoundError(msg) from None
            return _entity_to_ref(entity)
        else:
            return await self.get_entity(ctx, eid, as_of=as_of)

    async def update_entity(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        updates: dict[str, Any],
        valid_from: datetime.datetime | None = None,
    ) -> EntityRef:
        _validate_semver_attribute(updates)
        now = self._clock.now()
        valid_from = normalize_utc(valid_from) if valid_from is not None else now

        async with self._session_factory() as session, session.begin():
            entity = await session.get(Entity, entity_id)
            if entity is None:
                msg = f"entity {entity_id} not found"
                raise NotFoundError(msg)
            self._assert_tenant(ctx, entity.tenant_id)

            # Fetch ALL currently-open attribute rows for the entity in one
            # round-trip. The result serves two purposes:
            #   1. Build merged_attributes (existing values overlaid by updates)
            #      for schema validation before any writes happen.
            #   2. O(1) lookup during the supersede loop (same dict, no extra I/O).
            all_existing_result = await session.execute(
                select(Attribute).where(
                    Attribute.tenant_id == ctx.tenant_id,
                    Attribute.entity_id == entity_id,
                    Attribute.t_invalidated_at.is_(None),
                    Attribute.t_valid_to.is_(None),
                )
            )
            existing_by_key: dict[str, Attribute] = {row.key: row for row in all_existing_result.scalars()}

            # Validate the post-update attribute state against the entity_type
            # schema before writing any rows. Merges existing values under update
            # keys so the full attribute envelope is what the schema sees, not
            # just the patch delta. Raises ValidationError (HTTP 422) when a
            # mandatory schema is violated; advisory violations return warnings
            # but do not block the write.
            merged_attributes = {**{k: v.value for k, v in existing_by_key.items()}, **updates}
            await self._schema.validate_capability(ctx, entity.entity_type, merged_attributes)

            # Validate stage_progression transition when the attribute is being
            # changed. The check runs after _assert_tenant (above) so tenant
            # isolation is already enforced. ProgressionService reads tenant_id
            # from ctx — it never opens a new cross-tenant query path.
            if "stage_progression" in updates:
                new_state = updates["stage_progression"]
                old_state_attr = existing_by_key.get("stage_progression")
                old_state = old_state_attr.value if old_state_attr is not None else None
                if new_state != old_state:
                    # Build a lightweight attribute view for the progression
                    # service. Merges existing values with incoming updates so
                    # gate checks see the post-write attribute state.
                    _attr_view = _ProgressionEntityView(
                        entity_id=entity.entity_id,
                        entity_type=entity.entity_type,
                        attributes=merged_attributes,
                    )
                    progression_svc = ProgressionService(
                        session_factory=self._session_factory,
                        clock=self._clock,
                    )
                    await progression_svc.validate_transition(ctx, _attr_view, old_state, new_state)
                    # validate_transition returns ValidationResult(valid=True)
                    # or raises ProgressionError (HTTP 422).

            for key, value in updates.items():
                # `name` lives on the Entity row itself; every other key is a
                # bi-temporal attribute. Write through to entity.name when
                # the update touches it so subsequent get_full_capability /
                # to_response reflects the new value.
                if key == "name":
                    entity.name = value
                    continue
                # Close the currently-open row for this key, if any.
                current = existing_by_key.get(key)
                if current is not None:
                    current.t_valid_to = now
                # Insert the new bi-temporal row.
                session.add(
                    Attribute(
                        attr_id=uuid.uuid4(),
                        tenant_id=ctx.tenant_id,
                        entity_id=entity_id,
                        key=key,
                        value=value,
                        t_valid_from=valid_from,
                        t_valid_to=None,
                        t_ingested_at=now,
                        t_invalidated_at=None,
                        created_by=ctx.actor_id,
                    )
                )

        return _entity_to_ref(entity)

    async def delete_entity(self, ctx: TenantContext, entity_id: uuid.UUID) -> None:
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            entity = await session.get(Entity, entity_id)
            if entity is None:
                msg = f"entity {entity_id} not found"
                raise NotFoundError(msg)
            self._assert_tenant(ctx, entity.tenant_id)
            entity.is_active = False
            # Cascade soft-delete to attributes, facts, edges.
            attr_rows = await session.execute(
                select(Attribute).where(
                    Attribute.tenant_id == ctx.tenant_id,
                    Attribute.entity_id == entity_id,
                    Attribute.t_invalidated_at.is_(None),
                )
            )
            for attr in attr_rows.scalars():
                attr.t_valid_to = now
                attr.t_invalidated_at = now
            fact_rows = await session.execute(
                select(Fact).where(
                    Fact.tenant_id == ctx.tenant_id,
                    Fact.entity_id == entity_id,
                    Fact.t_invalidated_at.is_(None),
                )
            )
            for fact in fact_rows.scalars():
                fact.t_valid_to = now
                fact.t_invalidated_at = now
            edge_rows = await session.execute(
                select(Edge).where(
                    Edge.tenant_id == ctx.tenant_id,
                    (Edge.src_entity_id == entity_id) | (Edge.dst_entity_id == entity_id),
                    Edge.t_invalidated_at.is_(None),
                )
            )
            for edge in edge_rows.scalars():
                edge.t_valid_to = now
                edge.t_invalidated_at = now

    # ---- tenant initialisation --------------------------------------------

    async def seed_default_roles(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> None:
        """Seed the four default roles for *tenant_id* if they don't exist yet.

        Idempotent: uses INSERT … ON CONFLICT DO NOTHING so concurrent calls
        or re-runs are safe. Called at tenant creation time; must run within
        an existing transaction so the caller can roll back atomically on
        failure.

        Also inserts a tenant-level default ``rate_limits`` row (actor_id NULL)
        so the rate-limit middleware always has a fallback row to read.
        """
        now = self._clock.now()
        for name in (ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN, ROLE_AUDITOR):
            await session.execute(
                text(
                    "INSERT INTO roles (role_id, tenant_id, name, permissions, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :name, '{}', :now) "
                    "ON CONFLICT (tenant_id, name) DO NOTHING"
                ),
                {"tid": tenant_id, "name": name, "now": now},
            )
        await session.execute(
            text(
                "INSERT INTO rate_limits "
                "(limit_id, tenant_id, actor_id, reads_per_second, writes_per_second, created_at) "
                "VALUES (gen_random_uuid(), :tid, NULL, 100, 10, :now) "
                "ON CONFLICT DO NOTHING"
            ),
            {"tid": tenant_id, "now": now},
        )

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _assert_tenant(ctx: TenantContext, row_tenant_id: uuid.UUID) -> None:
        if row_tenant_id != ctx.tenant_id:
            msg = "row belongs to a different tenant"
            raise TenantIsolationError(msg)


__all__ = ["EntityService", "_entity_to_ref", "_validate_semver_attribute"]
