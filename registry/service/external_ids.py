"""ExternalIdService — passive external-system ID mapping.

Design constraints
------------------
* No bi-temporal history on ``entity_external_ids``.  Rows are either live or
  hard-deleted.  Soft-delete is intentionally not used here; the audit trail
  is written to ``audit_log`` instead so deletion history is recoverable.
* Uniqueness is ``(tenant_id, external_system_slug, external_id)``.  A duplicate
  insert raises :class:`~registry.exceptions.ConflictError` with the existing PK
  cited in the message.
* URL resolution: when the ``external_systems.url_template`` contains
  ``{external_id}``, the service substitutes the literal ``external_id`` string
  at mapping-create time and stores the result in ``entity_external_ids.url``.
  An explicit ``url`` argument supplied by the caller takes precedence over
  template resolution.
* Registry does not auto-resolve or dedup mappings — that is tenant-side logic.
* Every hard-delete is audit-logged via ``audit.emit()`` so off-line
  compliance tools can reconstruct deletion history without relying on
  the now-absent row.

Session management
------------------
All methods acquire a session from ``_session_factory`` via ``async with``.
Writes use ``session.begin()`` for an explicit transaction.  Reads are issued
inside the same ``async with`` block (implicit autocommit-safe).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api import audit as audit_emit
from registry.audit import actions
from registry.exceptions import ConflictError, NotFoundError, TenantIsolationError
from registry.types import Clock, EntityRef, ExternalIdRef, TenantContext

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row → dataclass helpers
# ---------------------------------------------------------------------------


def _row_to_ext_ref(row: Any) -> ExternalIdRef:
    """Map a SQLAlchemy ``Row`` (from the raw-SQL SELECT) to ``ExternalIdRef``."""
    return ExternalIdRef(
        external_id_pk=row.external_id_pk,
        entity_id=row.entity_id,
        tenant_id=row.tenant_id,
        external_system_slug=row.external_system_slug,
        external_id=row.external_id,
        url=row.url,
        metadata_jsonb=row.metadata_jsonb,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_entity_ref(row: Any) -> EntityRef:
    """Map a raw entity row to ``EntityRef``."""
    return EntityRef(
        entity_id=row.entity_id,
        tenant_id=row.tenant_id,
        entity_type=row.entity_type,
        name=row.name,
        external_id=row.external_id,
        is_active=row.is_active,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class ExternalIdService:
    """Service for the external-system registry and entity external-ID mappings.

    Parameters
    ----------
    session_factory:
        Async session maker (``async_sessionmaker[AsyncSession]``).  Injected
        at construction; never called outside this class.
    clock:
        UTC time source.  Injected so tests can freeze time without
        monkeypatching; production callers pass ``SystemClock()``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # ------------------------------------------------------------------
    # External system registry
    # ------------------------------------------------------------------

    async def register_external_system(
        self,
        ctx: TenantContext,
        slug: str,
        display_name: str,
        url_template: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Register a new external system for the tenant.

        ``slug`` must be unique per tenant.  A duplicate insert raises
        :class:`~registry.exceptions.ConflictError`.

        Returns a dict representing the inserted ``external_systems`` row.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            try:
                await session.execute(
                    text(
                        "INSERT INTO external_systems "
                        "(slug, tenant_id, display_name, url_template, description, created_at) "
                        "VALUES (:slug, :tenant_id, :display_name, :url_template, :description, :created_at)"
                    ),
                    {
                        "slug": slug,
                        "tenant_id": ctx.tenant_id,
                        "display_name": display_name,
                        "url_template": url_template,
                        "description": description,
                        "created_at": now,
                    },
                )
            except IntegrityError as exc:
                msg = f"external system with slug={slug!r} already exists " f"for tenant {ctx.tenant_id}"
                raise ConflictError(msg) from exc

        _log.info(
            "registered external system slug=%r tenant=%s",
            slug,
            ctx.tenant_id,
        )
        return {
            "slug": slug,
            "tenant_id": ctx.tenant_id,
            "display_name": display_name,
            "url_template": url_template,
            "description": description,
            "created_at": now,
        }

    async def list_external_systems(self, ctx: TenantContext) -> list[dict[str, Any]]:
        """Return all external systems registered for the tenant."""
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT slug, tenant_id, display_name, url_template, description, created_at "
                    "FROM external_systems "
                    "WHERE tenant_id = :tid "
                    "ORDER BY slug"
                ),
                {"tid": ctx.tenant_id},
            )
            rows = result.all()

        return [
            {
                "slug": r.slug,
                "tenant_id": r.tenant_id,
                "display_name": r.display_name,
                "url_template": r.url_template,
                "description": r.description,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def delete_external_system(self, ctx: TenantContext, slug: str) -> None:
        """Hard-delete an external system registration.

        Raises :class:`~registry.exceptions.NotFoundError` if the slug does not
        exist for this tenant.  Cascading deletes on ``entity_external_ids`` are
        the caller's / DB's responsibility (no FK cascade in schema — service
        must not leave orphaned mappings; callers should delete mappings first
        or the DB unique constraint will prevent re-registration of the same
        slug until orphans are cleared).
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text("DELETE FROM external_systems " "WHERE tenant_id = :tid AND slug = :slug"),
                {"tid": ctx.tenant_id, "slug": slug},
            )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            msg = f"external system slug={slug!r} not found for tenant {ctx.tenant_id}"
            raise NotFoundError(msg)

        _log.info("deleted external system slug=%r tenant=%s", slug, ctx.tenant_id)

    # ------------------------------------------------------------------
    # Entity external-ID mappings
    # ------------------------------------------------------------------

    async def add_external_id(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        external_system_slug: str,
        external_id: str,
        url: str | None = None,
        metadata_jsonb: dict[str, Any] | None = None,
    ) -> ExternalIdRef:
        """Create a new external-ID mapping for an entity.

        URL resolution
        ~~~~~~~~~~~~~~
        1. If ``url`` is supplied explicitly it is stored as-is.
        2. Else if ``external_systems.url_template`` is set, substitute
           ``{external_id}`` in the template and store the result.
        3. Otherwise ``url`` is ``None``.

        Raises
        ------
        ConflictError
            When ``(tenant_id, external_system_slug, external_id)`` already
            exists.  The message includes the existing ``external_id_pk``.
        NotFoundError
            When the external system slug is not registered for this tenant.
        TenantIsolationError
            When the resolved entity does not belong to this tenant.
        """
        now = self._clock.now()
        pk = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            # Ownership guard: entity must exist and belong to this tenant.
            entity_result = await session.execute(
                text("SELECT tenant_id FROM entities " "WHERE entity_id = :eid"),
                {"eid": entity_id},
            )
            entity_row = entity_result.first()
            if entity_row is None:
                msg = f"entity {entity_id} not found"
                raise NotFoundError(msg)
            if entity_row.tenant_id != ctx.tenant_id:
                msg = f"entity {entity_id} belongs to tenant {entity_row.tenant_id}, " f"not {ctx.tenant_id}"
                raise TenantIsolationError(msg)

            # Resolve URL from template when no explicit URL given.
            resolved_url = url
            if resolved_url is None:
                sys_result = await session.execute(
                    text("SELECT url_template FROM external_systems " "WHERE tenant_id = :tid AND slug = :slug"),
                    {"tid": ctx.tenant_id, "slug": external_system_slug},
                )
                sys_row = sys_result.first()
                if sys_row is None:
                    msg = f"external system slug={external_system_slug!r} " f"not registered for tenant {ctx.tenant_id}"
                    raise NotFoundError(msg)
                if sys_row.url_template is not None:
                    resolved_url = sys_row.url_template.replace("{external_id}", external_id)
            else:
                # Still verify external system exists.
                sys_result = await session.execute(
                    text("SELECT 1 FROM external_systems " "WHERE tenant_id = :tid AND slug = :slug"),
                    {"tid": ctx.tenant_id, "slug": external_system_slug},
                )
                if sys_result.first() is None:
                    msg = f"external system slug={external_system_slug!r} " f"not registered for tenant {ctx.tenant_id}"
                    raise NotFoundError(msg)

            # Insert the mapping row.
            meta_json = json.dumps(metadata_jsonb) if metadata_jsonb is not None else None
            try:
                await session.execute(
                    text(
                        "INSERT INTO entity_external_ids "
                        "(external_id_pk, entity_id, tenant_id, external_system_slug, "
                        " external_id, url, metadata_jsonb, created_at, updated_at) "
                        "VALUES (:pk, :entity_id, :tenant_id, :slug, :ext_id, :url, "
                        "        CAST(:meta AS jsonb), :created_at, :updated_at)"
                    ),
                    {
                        "pk": pk,
                        "entity_id": entity_id,
                        "tenant_id": ctx.tenant_id,
                        "slug": external_system_slug,
                        "ext_id": external_id,
                        "url": resolved_url,
                        "meta": meta_json,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            except IntegrityError as exc:
                # The current transaction is now aborted; open a fresh session
                # to look up the existing PK for the error message.
                existing_pk: str | uuid.UUID = "unknown"
                try:
                    async with self._session_factory() as lookup_session:
                        lookup_result = await lookup_session.execute(
                            text(
                                "SELECT external_id_pk FROM entity_external_ids "
                                "WHERE tenant_id = :tid AND external_system_slug = :slug "
                                "  AND external_id = :ext_id"
                            ),
                            {
                                "tid": ctx.tenant_id,
                                "slug": external_system_slug,
                                "ext_id": external_id,
                            },
                        )
                        existing_row = lookup_result.first()
                        if existing_row is not None:
                            existing_pk = existing_row.external_id_pk
                except Exception:  # noqa: BLE001
                    pass  # already defaulted to "unknown"
                msg = (
                    f"external ID {external_id!r} for system {external_system_slug!r} "
                    f"already exists (external_id_pk={existing_pk})"
                )
                raise ConflictError(msg) from exc

        _log.info(
            "added external ID pk=%s entity=%s system=%r tenant=%s",
            pk,
            entity_id,
            external_system_slug,
            ctx.tenant_id,
        )
        return ExternalIdRef(
            external_id_pk=pk,
            entity_id=entity_id,
            tenant_id=ctx.tenant_id,
            external_system_slug=external_system_slug,
            external_id=external_id,
            url=resolved_url,
            metadata_jsonb=metadata_jsonb,
            created_at=now,
            updated_at=now,
        )

    async def list_external_ids(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
    ) -> list[ExternalIdRef]:
        """Return all external-ID mappings for an entity, ordered by creation time."""
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT external_id_pk, entity_id, tenant_id, external_system_slug, "
                    "       external_id, url, metadata_jsonb, created_at, updated_at "
                    "FROM entity_external_ids "
                    "WHERE tenant_id = :tid AND entity_id = :eid "
                    "ORDER BY created_at"
                ),
                {"tid": ctx.tenant_id, "eid": entity_id},
            )
            rows = result.all()

        return [_row_to_ext_ref(r) for r in rows]

    async def lookup_by_external_id(
        self,
        ctx: TenantContext,
        external_system_slug: str,
        external_id: str,
    ) -> EntityRef | None:
        """Return the entity mapped to ``(external_system_slug, external_id)``.

        Returns ``None`` when no mapping exists.  Registry never auto-resolves
        ambiguity; if multiple rows somehow exist (should be prevented by the
        unique constraint), the most-recently-created one is returned.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT e.entity_id, e.tenant_id, e.entity_type, e.name, "
                    "       e.external_id, e.is_active, e.created_at "
                    "FROM entity_external_ids m "
                    "JOIN entities e USING (entity_id) "
                    "WHERE m.tenant_id = :tid "
                    "  AND m.external_system_slug = :slug "
                    "  AND m.external_id = :ext_id "
                    "ORDER BY m.created_at DESC "
                    "LIMIT 1"
                ),
                {
                    "tid": ctx.tenant_id,
                    "slug": external_system_slug,
                    "ext_id": external_id,
                },
            )
            row = result.first()

        if row is None:
            return None
        return _row_to_entity_ref(row)

    async def delete_external_id(
        self,
        ctx: TenantContext,
        external_id_pk: uuid.UUID,
    ) -> None:
        """Hard-delete a single external-ID mapping by its primary key.

        Ownership is verified: the row must belong to ``ctx.tenant_id``.
        Raises :class:`~registry.exceptions.NotFoundError` if the row does not
        exist or belongs to a different tenant (avoids leaking existence).
        The deletion is audit-logged unconditionally before the row is removed.

        Hard-delete only: no soft-history; the row is gone after this call.
        """
        async with self._session_factory() as session, session.begin():
            # Verify ownership and capture snapshot for audit log.
            snap_result = await session.execute(
                text(
                    "SELECT external_id_pk, entity_id, tenant_id, "
                    "       external_system_slug, external_id "
                    "FROM entity_external_ids "
                    "WHERE external_id_pk = :pk AND tenant_id = :tid"
                ),
                {"pk": external_id_pk, "tid": ctx.tenant_id},
            )
            snap = snap_result.first()
            if snap is None:
                msg = f"external_id_pk={external_id_pk} not found " f"for tenant {ctx.tenant_id}"
                raise NotFoundError(msg)

            audit_snapshot = {
                "external_system_slug": snap.external_system_slug,
                "external_id": snap.external_id,
                "entity_id": str(snap.entity_id),
            }

            # Hard delete.
            await session.execute(
                text("DELETE FROM entity_external_ids " "WHERE external_id_pk = :pk AND tenant_id = :tid"),
                {"pk": external_id_pk, "tid": ctx.tenant_id},
            )

        # Audit in a separate transaction so a write failure never unwinds the
        # completed delete.  emit() opens its own session and swallows errors
        # internally, incrementing catalog_audit_write_failures_total instead.
        await audit_emit.emit(
            self._session_factory,
            ctx,
            self._clock,
            action=actions.EXTERNAL_ID_DELETED,
            target_type="entity_external_id",
            target_id=external_id_pk,
            after=audit_snapshot,
        )

        _log.info(
            "hard-deleted external_id_pk=%s tenant=%s actor=%s",
            external_id_pk,
            ctx.tenant_id,
            ctx.actor_id,
        )

    async def update_external_id(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        external_id_pk: uuid.UUID,
        url: str | None = None,
        metadata_jsonb: dict[str, Any] | None = None,
    ) -> ExternalIdRef:
        """Update the ``url`` or ``metadata_jsonb`` of an existing mapping.

        Ownership is verified: the row must belong to both ``ctx.tenant_id``
        and ``entity_id``.  Raises :class:`~registry.exceptions.NotFoundError`
        if the row is absent or ownership fails (avoids leaking existence).

        Only supplied fields (non-``None``) replace the stored values.  To
        explicitly clear a field pass an empty string or empty dict rather
        than ``None`` — ``None`` means "no change".

        Returns the updated :class:`~registry.types.ExternalIdRef`.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            # Fetch current row — verifies ownership.
            row_result = await session.execute(
                text(
                    "SELECT external_id_pk, entity_id, tenant_id, "
                    "       external_system_slug, external_id, url, "
                    "       metadata_jsonb, created_at "
                    "FROM entity_external_ids "
                    "WHERE external_id_pk = :pk "
                    "  AND tenant_id = :tid "
                    "  AND entity_id = :eid"
                ),
                {"pk": external_id_pk, "tid": ctx.tenant_id, "eid": entity_id},
            )
            row = row_result.first()
            if row is None:
                msg = f"external_id_pk={external_id_pk} not found for " f"entity {entity_id} tenant {ctx.tenant_id}"
                raise NotFoundError(msg)

            resolved_url = url if url is not None else row.url
            resolved_meta = metadata_jsonb if metadata_jsonb is not None else row.metadata_jsonb
            meta_json = json.dumps(resolved_meta) if resolved_meta is not None else None

            await session.execute(
                text(
                    "UPDATE entity_external_ids "
                    "SET url = :url, metadata_jsonb = CAST(:meta AS jsonb), updated_at = :now "
                    "WHERE external_id_pk = :pk AND tenant_id = :tid"
                ),
                {
                    "url": resolved_url,
                    "meta": meta_json,
                    "now": now,
                    "pk": external_id_pk,
                    "tid": ctx.tenant_id,
                },
            )

        _log.info(
            "updated external_id_pk=%s entity=%s tenant=%s actor=%s",
            external_id_pk,
            entity_id,
            ctx.tenant_id,
            ctx.actor_id,
        )
        return ExternalIdRef(
            external_id_pk=external_id_pk,
            entity_id=entity_id,
            tenant_id=ctx.tenant_id,
            external_system_slug=row.external_system_slug,
            external_id=row.external_id,
            url=resolved_url,
            metadata_jsonb=resolved_meta,
            created_at=row.created_at,
            updated_at=now,
        )
