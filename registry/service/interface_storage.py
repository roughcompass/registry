"""InterfaceStorageService — bi-temporal storage for capability interface declarations.

Persists capability interface declarations as bi-temporal attribute rows:

* ``interface_source`` — the original payload as submitted by the producer,
  keyed by ``interface_format`` so the source can be re-rendered.
* ``interface_canonical`` — the normalised :class:`InterfaceSurface`
  serialised to JSON. The diff engine and breaking-change advisor
  operate exclusively on this column.

On write, previous active rows for both keys are soft-superseded
(``t_invalidated_at = now()``). On read with ``as_of``, the active rows
at that instant are returned, so historical surfaces remain queryable.

Visibility is enforced at the call site: the caller must own the capability
*and* must have the ``producer`` or ``admin`` role.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.auth.context import ROLE_ADMIN, ROLE_PRODUCER
from registry.exceptions import NotFoundError
from registry.service.interface_normalize import normalize
from registry.service.visibility import VisibilityService
from registry.types import Clock, InterfaceSurface, TenantContext

_log = logging.getLogger(__name__)

INTERFACE_SOURCE_KEY = "interface_source"
INTERFACE_CANONICAL_KEY = "interface_canonical"


@dataclasses.dataclass
class InterfaceRecord:
    """Returned by :meth:`InterfaceStorageService.get_interface`.

    ``as_of`` mirrors what the caller passed (``None`` for current truth).
    """

    capability_id: uuid.UUID
    interface_canonical: InterfaceSurface | None
    interface_source: dict[str, Any] | None
    interface_format: str | None
    as_of: datetime.datetime | None


class InterfaceStorageService:
    """Bi-temporal storage of normalised interface surfaces."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        visibility: VisibilityService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._visibility = visibility

    async def put_interface(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID,
        interface_source: Any,
        interface_format: str,
    ) -> InterfaceSurface:
        """Replace the capability's interface surface (bi-temporal supersession).

        Order: visibility check → normalize (rejects bad input early) →
        single transaction that invalidates the old rows and inserts the
        new pair. Returns the normalised :class:`InterfaceSurface` for
        immediate use (the advisor would otherwise re-fetch it).
        """
        await self._assert_owner(ctx, capability_id)
        canonical = normalize(interface_source, interface_format)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attributes
                    SET t_invalidated_at = :now,
                        t_valid_to = :now
                    WHERE entity_id = :eid
                      AND key IN (:k_src, :k_can)
                      AND t_invalidated_at IS NULL
                      AND t_valid_to IS NULL
                    """
                ),
                {
                    "now": now,
                    "eid": capability_id,
                    "k_src": INTERFACE_SOURCE_KEY,
                    "k_can": INTERFACE_CANONICAL_KEY,
                },
            )
            source_payload = {
                "format": interface_format,
                "raw": interface_source,
            }
            await session.execute(
                text(
                    """
                    INSERT INTO attributes
                      (attr_id, tenant_id, entity_id, key, value,
                       t_valid_from, t_valid_to, t_ingested_at,
                       t_invalidated_at, created_by)
                    VALUES (gen_random_uuid(), :tid, :eid,
                            :k, CAST(:v AS jsonb),
                            :now, NULL, :now, NULL, :actor)
                    """
                ),
                {
                    "tid": ctx.tenant_id,
                    "eid": capability_id,
                    "k": INTERFACE_SOURCE_KEY,
                    "v": json.dumps(source_payload),
                    "now": now,
                    "actor": ctx.actor_id,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO attributes
                      (attr_id, tenant_id, entity_id, key, value,
                       t_valid_from, t_valid_to, t_ingested_at,
                       t_invalidated_at, created_by)
                    VALUES (gen_random_uuid(), :tid, :eid,
                            :k, CAST(:v AS jsonb),
                            :now, NULL, :now, NULL, :actor)
                    """
                ),
                {
                    "tid": ctx.tenant_id,
                    "eid": capability_id,
                    "k": INTERFACE_CANONICAL_KEY,
                    "v": json.dumps(dataclasses.asdict(canonical)),
                    "now": now,
                    "actor": ctx.actor_id,
                },
            )
        _log.info(
            "interface_written cap=%s format=%s actor=%s",
            capability_id,
            interface_format,
            ctx.actor_id,
        )
        return canonical

    async def get_interface(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID,
        as_of: datetime.datetime | None = None,
    ) -> InterfaceRecord:
        """Return the active interface at ``as_of`` (or current truth).

        Visibility-gated. Returns an empty record (``interface_canonical=None``)
        if no interface has been written.
        """
        await self._visibility.assert_visible(ctx, capability_id)

        async with self._session_factory() as session:
            params: dict[str, Any] = {
                "eid": capability_id,
                "k_src": INTERFACE_SOURCE_KEY,
                "k_can": INTERFACE_CANONICAL_KEY,
            }
            if as_of is None:
                sql = text(
                    """
                    SELECT key, value, t_valid_from
                    FROM attributes
                    WHERE entity_id = :eid
                      AND key IN (:k_src, :k_can)
                      AND t_invalidated_at IS NULL
                      AND t_valid_to IS NULL
                    """
                )
            else:
                sql = text(
                    """
                    SELECT DISTINCT ON (key) key, value, t_valid_from
                    FROM attributes
                    WHERE entity_id = :eid
                      AND key IN (:k_src, :k_can)
                      AND t_valid_from <= :as_of
                      AND (t_valid_to IS NULL OR t_valid_to > :as_of)
                      AND (t_invalidated_at IS NULL
                           OR t_invalidated_at > :as_of)
                    ORDER BY key, t_valid_from DESC
                    """
                )
                params["as_of"] = as_of

            result = await session.execute(sql, params)
            rows = {row.key: row.value for row in result.all()}

        canonical: InterfaceSurface | None = None
        if INTERFACE_CANONICAL_KEY in rows:
            raw = rows[INTERFACE_CANONICAL_KEY]
            if isinstance(raw, str):
                raw = json.loads(raw)
            canonical = InterfaceSurface(
                operations=list(raw.get("operations") or []),
                events=list(raw.get("events") or []),
                fields=list(raw.get("fields") or []),
            )

        source: dict[str, Any] | None = None
        fmt: str | None = None
        if INTERFACE_SOURCE_KEY in rows:
            raw = rows[INTERFACE_SOURCE_KEY]
            if isinstance(raw, str):
                raw = json.loads(raw)
            source = raw
            fmt = raw.get("format") if isinstance(raw, dict) else None

        return InterfaceRecord(
            capability_id=capability_id,
            interface_canonical=canonical,
            interface_source=source,
            interface_format=fmt,
            as_of=as_of,
        )

    # ------------------------------------------------------------------
    # Authorisation helper
    # ------------------------------------------------------------------

    async def _assert_owner(self, ctx: TenantContext, capability_id: uuid.UUID) -> None:
        """Caller must own the capability AND have producer/admin role."""
        if not ({ROLE_PRODUCER, ROLE_ADMIN} & set(ctx.roles)):
            raise PermissionError("writing an interface requires producer or admin role")
        async with self._session_factory() as session:
            res = await session.execute(
                text("SELECT tenant_id FROM entities WHERE entity_id = :eid"),
                {"eid": capability_id},
            )
            row = res.first()
        if row is None:
            raise NotFoundError(f"capability {capability_id} not found")
        if row.tenant_id != ctx.tenant_id:
            # Same shape as visibility's tenant-isolation: opaque 404.
            raise NotFoundError(f"capability {capability_id} not found")


__all__ = [
    "INTERFACE_CANONICAL_KEY",
    "INTERFACE_SOURCE_KEY",
    "InterfaceRecord",
    "InterfaceStorageService",
]
