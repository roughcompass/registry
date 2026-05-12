"""AdoptionService — cross-tenant capability adoption lifecycle.

A consumer tenant declares intent to depend on a provider tenant's
capability via :meth:`AdoptionService.adopt`. The service does three
things atomically inside a single SQL transaction:

1. Inserts an ``adoption_events`` row recording the relationship.
2. Inserts a ``provides_to`` edge owned by the **provider** tenant
   (this edge bypasses the normal ``CatalogService.create_edge`` gate
   because the gate rejects ``provides_to`` for direct creation — only
   AdoptionService is allowed to write it).
3. Calls the auto-subscribe hook, allowing the caller to wire in
   subscription creation inside the same transaction.

Authorisation
-------------
* The caller must hold ``producer`` or ``admin`` role in the
  **consumer** tenant. Adoption is a consumer-side act ("I am adopting
  this dependency"); the producer is informed via the
  ``provides_to`` edge and downstream subscription.
* :meth:`VisibilityService.assert_visible` is called against the
  provider capability before any write — a tenant cannot adopt a
  capability it cannot see.

Soft-delete
-----------
:meth:`unadopt` marks the adoption row by setting
``t_invalidated_at`` (bi-temporal closure). The ``provides_to`` edge
is NOT removed — the historical relationship remains queryable via
``as_of`` traversal. Re-adoption (``adopt`` after ``unadopt``) creates
a *new* row; the old row stays for audit.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.auth.context import ROLE_ADMIN, ROLE_PRODUCER
from registry.exceptions import NotFoundError, ValidationError
from registry.service.visibility import VisibilityService
from registry.types import AdoptionEventRef, Clock, TenantContext

_log = logging.getLogger(__name__)

#: Roles permitted to call adopt/unadopt on the consumer side.
_REQUIRED_ROLES: frozenset[str] = frozenset({ROLE_PRODUCER, ROLE_ADMIN})


class AutoSubscribeHook(Protocol):
    """Hook called from inside the adoption transaction.

    The orchestrator wires ``SubscriptionService.auto_subscribe`` here so
    that subscriptions are created in the same transaction as the adoption
    and ``provides_to`` edge. Until a real implementation is injected, a
    no-op stub is used. The hook receives the open ``AsyncSession`` so it
    can write inside the same transaction that created the adoption + edge.
    """

    async def __call__(
        self,
        *,
        session: AsyncSession,
        ctx: TenantContext,
        adoption: AdoptionEventRef,
    ) -> None: ...


async def _noop_auto_subscribe(
    *,
    session: AsyncSession,
    ctx: TenantContext,
    adoption: AdoptionEventRef,
) -> None:
    """Default no-op stub used when no auto-subscribe hook is injected."""
    return None


class AdoptionService:
    """Adoption lifecycle for cross-tenant capability dependencies."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        visibility: VisibilityService,
        auto_subscribe: AutoSubscribeHook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._visibility = visibility
        self._auto_subscribe: AutoSubscribeHook = auto_subscribe if auto_subscribe is not None else _noop_auto_subscribe

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def adopt(
        self,
        ctx: TenantContext,
        provider_capability_id: uuid.UUID,
        consumer_tenant_id: uuid.UUID,
        intent: str | None = None,
        version_pin: str | None = None,
        clock: Clock | None = None,
    ) -> AdoptionEventRef:
        """Create an adoption event + provides_to edge + auto-subscribe.

        ``ctx`` is the *consumer-side* caller. ``ctx.tenant_id`` must
        equal ``consumer_tenant_id`` — the adoption is being recorded
        on behalf of the consumer tenant the caller belongs to. Cross-
        tenant adopt-on-behalf-of is not supported (would be a footgun
        for the existing role model).

        The provider tenant is read from the capability's entity row.
        The ``adoption_events.tenant_id`` is set to the **provider**
        tenant (owner of the capability) per the data model; the
        consumer tenant is in ``consumer_tenant_id``.

        Raises
        ------
        PermissionError
            If the caller lacks ``producer`` or ``admin`` role, or if
            ``ctx.tenant_id != consumer_tenant_id``.
        NotFoundError
            If the provider capability does not exist or is not visible
            to the consumer tenant.
        ValidationError
            If ``intent`` or ``version_pin`` violate their constraints.
        """
        self._assert_authorized(ctx, consumer_tenant_id)
        _validate_intent(intent)
        _validate_version_pin(version_pin)

        # Visibility check before any write — a tenant cannot adopt a capability it cannot see.
        await self._visibility.assert_visible(ctx, provider_capability_id)

        effective_clock = clock if clock is not None else self._clock
        now = effective_clock.now()

        async with self._session_factory() as session, session.begin():
            provider_tenant_id = await self._lookup_provider_tenant(session, provider_capability_id)

            adoption_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO adoption_events
                        (adoption_id, tenant_id, provider_capability_id,
                         consumer_tenant_id, actor_id, intent, version_pin,
                         t_valid_from, t_ingested_at)
                    VALUES (:aid, :ptid, :cap, :ctid, :actor, :intent,
                            :ver, :now, :now)
                    """
                ),
                {
                    "aid": adoption_id,
                    "ptid": provider_tenant_id,
                    "cap": provider_capability_id,
                    "ctid": consumer_tenant_id,
                    "actor": ctx.actor_id,
                    "intent": intent,
                    "ver": version_pin,
                    "now": now,
                },
            )

            await self._insert_provides_to_edge(
                session=session,
                provider_tenant_id=provider_tenant_id,
                provider_capability_id=provider_capability_id,
                consumer_tenant_id=consumer_tenant_id,
                now=now,
            )

            adoption_ref = AdoptionEventRef(
                adoption_id=adoption_id,
                tenant_id=provider_tenant_id,
                provider_capability_id=provider_capability_id,
                consumer_tenant_id=consumer_tenant_id,
                actor_id=ctx.actor_id,
                intent=intent,
                version_pin=version_pin,
                t_valid_from=now,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
            )

            await self._auto_subscribe(session=session, ctx=ctx, adoption=adoption_ref)

        _log.info(
            "adoption_created adoption_id=%s provider_cap=%s consumer_tenant=%s",
            adoption_id,
            provider_capability_id,
            consumer_tenant_id,
        )
        return adoption_ref

    async def unadopt(
        self,
        ctx: TenantContext,
        adoption_id: uuid.UUID,
        clock: Clock | None = None,
    ) -> None:
        """Soft-delete an adoption row by setting ``t_invalidated_at``.

        The ``provides_to`` edge is intentionally not removed — the
        historical relationship remains queryable via bi-temporal
        ``as_of`` traversal. Callers that want to fully break the
        edge must close it separately via the catalog API.

        Caller must hold ``producer`` or ``admin`` in the consumer
        tenant that owns the adoption row.
        """
        effective_clock = clock if clock is not None else self._clock
        now = effective_clock.now()

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    """
                    SELECT consumer_tenant_id, t_invalidated_at
                    FROM adoption_events
                    WHERE adoption_id = :aid
                    """
                ),
                {"aid": adoption_id},
            )
            row = result.first()
            if row is None:
                msg = f"adoption {adoption_id} not found"
                raise NotFoundError(msg)
            consumer_tenant_id, already_invalidated = row.consumer_tenant_id, row.t_invalidated_at
            self._assert_authorized(ctx, consumer_tenant_id)

            if already_invalidated is not None:
                # Idempotent — already unadopted.
                return

            await session.execute(
                text(
                    """
                    UPDATE adoption_events
                       SET t_invalidated_at = :now,
                           t_valid_to       = :now
                     WHERE adoption_id = :aid
                       AND t_invalidated_at IS NULL
                    """
                ),
                {"aid": adoption_id, "now": now},
            )

        _log.info(
            "adoption_unadopted adoption_id=%s consumer_tenant=%s",
            adoption_id,
            consumer_tenant_id,
        )

    async def get_active_adoption(
        self,
        consumer_tenant_id: uuid.UUID,
        provider_capability_id: uuid.UUID,
    ) -> AdoptionEventRef | None:
        """Return the current active adoption row, if any.

        "Active" means ``t_invalidated_at IS NULL``. Returns the most
        recent row when multiple are open (which shouldn't happen given
        the uniqueness constraint, but defensive).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT adoption_id, tenant_id, provider_capability_id,
                           consumer_tenant_id, actor_id, intent, version_pin,
                           t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at
                    FROM adoption_events
                    WHERE consumer_tenant_id = :ctid
                      AND provider_capability_id = :cap
                      AND t_invalidated_at IS NULL
                    ORDER BY t_valid_from DESC
                    LIMIT 1
                    """
                ),
                {"ctid": consumer_tenant_id, "cap": provider_capability_id},
            )
            row = result.mappings().first()
            if row is None:
                return None
            return AdoptionEventRef(
                adoption_id=row["adoption_id"],
                tenant_id=row["tenant_id"],
                provider_capability_id=row["provider_capability_id"],
                consumer_tenant_id=row["consumer_tenant_id"],
                actor_id=row["actor_id"],
                intent=row["intent"],
                version_pin=row["version_pin"],
                t_valid_from=row["t_valid_from"],
                t_valid_to=row["t_valid_to"],
                t_ingested_at=row["t_ingested_at"],
                t_invalidated_at=row["t_invalidated_at"],
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_authorized(ctx: TenantContext, consumer_tenant_id: uuid.UUID) -> None:
        """Caller must hold producer/admin in their own tenant AND be the
        consumer tenant. Mismatch raises PermissionError so the orchestrator
        surfaces it as 403."""
        if ctx.tenant_id != consumer_tenant_id:
            msg = (
                f"caller tenant {ctx.tenant_id} cannot adopt on behalf of "
                f"a different consumer tenant {consumer_tenant_id}"
            )
            raise PermissionError(msg)
        if not any(role in _REQUIRED_ROLES for role in ctx.roles):
            msg = f"adopt/unadopt requires one of: {sorted(_REQUIRED_ROLES)!r}; " f"caller has roles {ctx.roles}"
            raise PermissionError(msg)

    @staticmethod
    async def _lookup_provider_tenant(session: AsyncSession, provider_capability_id: uuid.UUID) -> uuid.UUID:
        result = await session.execute(
            text("SELECT tenant_id FROM entities WHERE entity_id = :eid"),
            {"eid": provider_capability_id},
        )
        row = result.first()
        if row is None:
            msg = f"provider capability {provider_capability_id} not found"
            raise NotFoundError(msg)
        return row.tenant_id

    @staticmethod
    async def _insert_provides_to_edge(
        *,
        session: AsyncSession,
        provider_tenant_id: uuid.UUID,
        provider_capability_id: uuid.UUID,
        consumer_tenant_id: uuid.UUID,
        now: Any,
    ) -> None:
        """Insert a ``provides_to`` edge owned by the provider tenant.

        ``CatalogService.create_edge`` rejects this rel directly; the
        only legitimate writer is AdoptionService.

        Shape: a self-loop on the provider capability
        (``src = dst = provider_capability_id``) with the consumer
        tenant encoded in ``properties``. The self-loop satisfies the
        ``edges_dst_entity_id_fkey`` constraint (both endpoints must
        reference an existing entity row), while the
        ``properties.consumer_tenant_id`` field carries the cross-tenant
        relationship for downstream projection queries.
        Alternatives considered:

        * dst=consumer-side entity — rejected because not every consumer
          tenant has a dedicated consumer-side capability; adoption is
          tenant-level.
        * dst=consumer_tenant_id directly — rejected because tenant UUIDs
          don't satisfy the entities FK.

        Multiple adoptions from the same consumer to the same provider
        capability produce identical edge rows; ``ON CONFLICT DO NOTHING``
        keeps the upsert idempotent at the row level even though the
        actual uniqueness lives on ``adoption_events`` (uq_adoption).
        """
        edge_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO edges
                    (edge_id, tenant_id, src_entity_id, rel, dst_entity_id,
                     properties, is_authoritative, t_valid_from, t_ingested_at)
                VALUES (:eid, :ptid, :cap, 'provides_to', :cap,
                        CAST(:props AS jsonb), TRUE, :now, :now)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "eid": edge_id,
                "ptid": provider_tenant_id,
                "cap": provider_capability_id,
                "props": f'{{"consumer_tenant_id": "{consumer_tenant_id}"}}',
                "now": now,
            },
        )


# ---------------------------------------------------------------------------
# Module-level validators (easier to unit-test in isolation)
# ---------------------------------------------------------------------------


_MAX_INTENT_LEN = 1000
_MAX_VERSION_PIN_LEN = 64


def _validate_intent(intent: str | None) -> None:
    if intent is None:
        return
    if not isinstance(intent, str):
        msg = f"intent must be a string or None; got {type(intent).__name__}"
        raise ValidationError(msg)
    if len(intent) > _MAX_INTENT_LEN:
        msg = (
            f"intent too long: {len(intent)} > {_MAX_INTENT_LEN} chars. "
            f"Keep it short — adopt() is an audit-trail mechanism."
        )
        raise ValidationError(msg)


def _validate_version_pin(version_pin: str | None) -> None:
    if version_pin is None:
        return
    if not isinstance(version_pin, str):
        msg = f"version_pin must be a string or None; got {type(version_pin).__name__}"
        raise ValidationError(msg)
    if len(version_pin) > _MAX_VERSION_PIN_LEN:
        msg = f"version_pin too long: {len(version_pin)} > " f"{_MAX_VERSION_PIN_LEN} chars"
        raise ValidationError(msg)


__all__ = [
    "AdoptionService",
    "AutoSubscribeHook",
    "AdoptionEventRef",
    "_validate_intent",
    "_validate_version_pin",
]
