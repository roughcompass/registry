"""SubscriptionService — subscription lifecycle and capability event fan-out.

The subscription service owns three things:

1. Subscription lifecycle (create / list / soft-delete / list for a tenant).
2. The ``auto_subscribe`` entry point invoked by :mod:`registry.service.adoption`
   from inside the adoption transaction (inbox-only, idempotent).
3. ``emit_event``: the fan-out path that turns a single capability mutation
   into one ``notifications`` row per active subscription and a
   ``notification_deliveries`` row per subscription that has a webhook URL.

Payload-minimality is enforced by the type system: the service stores only the
columns defined on ``notifications`` (slug, kinds, versions, fetch_url) — no
body text, description, or freeform field is ever written. The
:class:`~registry.types.CapabilityRegistryEvent` dataclass mirrors the same shape
and is the canonical wire format for both the inbox (``GET /v1/notifications``)
and the webhook payload.

Event vocabulary is closed:

    {version_published, deprecation, breaking_change,
     conflict_added, integration_added}

Any other ``event_kind`` (on either create or emit) raises ``ValidationError``
→ 422. Digest windows are inherited at create time from the tenant's
``notification_digest_window`` column and are not retroactively updated when
that column changes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import NotFoundError, ValidationError
from registry.service.adoption import AutoSubscribeHook
from registry.service.visibility import VisibilityService
from registry.types import (
    AdoptionEventRef,
    Clock,
    SubscriptionRef,
    TenantContext,
)

_log = logging.getLogger(__name__)

#: Closed event vocabulary — any other kind raises ValidationError.
VALID_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "version_published",
        "deprecation",
        "breaking_change",
        "conflict_added",
        "integration_added",
    }
)

#: Default event kinds auto-subscribed on adoption.
AUTO_SUBSCRIBE_EVENT_KINDS: tuple[str, ...] = (
    "version_published",
    "deprecation",
    "breaking_change",
)

#: Valid digest windows accepted by tenants.notification_digest_window.
_VALID_DIGEST_WINDOWS: frozenset[str] = frozenset({"none", "5m", "15m", "1h", "6h", "24h"})


def _validate_event_kinds(event_kinds: list[str]) -> None:
    """Reject empty list or any kind outside the closed vocabulary."""
    if not event_kinds:
        raise ValidationError("event_kinds must be non-empty. Valid kinds: " f"{sorted(VALID_EVENT_KINDS)}.")
    bad = [k for k in event_kinds if k not in VALID_EVENT_KINDS]
    if bad:
        raise ValidationError(f"invalid event_kinds {bad!r}. Valid kinds: " f"{sorted(VALID_EVENT_KINDS)}.")


class SubscriptionService:
    """Subscription lifecycle + event fan-out."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        visibility: VisibilityService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._visibility = visibility

    # ------------------------------------------------------------------
    # Create / list / delete
    # ------------------------------------------------------------------

    async def create_subscription(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID,
        event_kinds: list[str],
        webhook_url: str | None = None,
        webhook_hmac_secret_ref: str | None = None,
    ) -> uuid.UUID:
        """Create a subscription owned by ``ctx.tenant_id``.

        Visibility is enforced before the row is written: the caller must be
        able to see the capability they are subscribing
        to. ``digest_window`` snapshots the tenant's current
        ``notification_digest_window``.

        ``webhook_url == None`` is an inbox-only subscription. When set,
        ``webhook_hmac_secret_ref`` should also be set (the worker
        rejects unsigned deliveries) — this service treats both as opaque
        strings; HMAC computation is the worker's responsibility.
        """
        _validate_event_kinds(event_kinds)
        if self._visibility is not None:
            await self._visibility.assert_visible(ctx, capability_id)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            digest = await self._fetch_tenant_digest_window(session, ctx.tenant_id)
            sub_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO subscriptions
                        (subscription_id, tenant_id, actor_id, capability_id,
                         event_kinds, webhook_url, webhook_hmac_secret_ref,
                         is_enabled, digest_window,
                         t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at,
                         created_at)
                    VALUES (:sid, :tid, :aid, :cap,
                            :kinds, :url, :secret,
                            TRUE, :digest,
                            :now, NULL, :now, NULL,
                            :now)
                    """
                ),
                {
                    "sid": sub_id,
                    "tid": ctx.tenant_id,
                    "aid": ctx.actor_id,
                    "cap": capability_id,
                    "kinds": list(event_kinds),
                    "url": webhook_url,
                    "secret": webhook_hmac_secret_ref,
                    "digest": digest,
                    "now": now,
                },
            )
        _log.info(
            "subscription_created sid=%s tenant=%s cap=%s kinds=%s digest=%s",
            sub_id,
            ctx.tenant_id,
            capability_id,
            event_kinds,
            digest,
        )
        return sub_id

    async def auto_subscribe(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        capability_id: uuid.UUID,
        event_kinds: list[str] | None = None,
    ) -> uuid.UUID:
        """Inbox-only subscription written inside an open transaction.

        Idempotent: if an active subscription already exists for
        ``(tenant_id, capability_id)`` with overlapping event kinds, the
        existing ``subscription_id`` is returned and no row is created.
        Called by :mod:`registry.service.adoption` from inside the
        adoption transaction (see :class:`AutoSubscribeHook`).
        """
        kinds = list(event_kinds or AUTO_SUBSCRIBE_EVENT_KINDS)
        _validate_event_kinds(kinds)

        existing = await session.execute(
            text(
                """
                SELECT subscription_id, event_kinds FROM subscriptions
                WHERE tenant_id = :tid
                  AND capability_id = :cap
                  AND webhook_url IS NULL
                  AND t_invalidated_at IS NULL
                LIMIT 1
                """
            ),
            {"tid": tenant_id, "cap": capability_id},
        )
        row = existing.first()
        if row is not None:
            return row.subscription_id  # type: ignore[no-any-return]

        now = self._clock.now()
        digest = await self._fetch_tenant_digest_window(session, tenant_id)
        sub_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO subscriptions
                    (subscription_id, tenant_id, actor_id, capability_id,
                     event_kinds, webhook_url, webhook_hmac_secret_ref,
                     is_enabled, digest_window,
                     t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at,
                     created_at)
                VALUES (:sid, :tid, :aid, :cap,
                        :kinds, NULL, NULL,
                        TRUE, :digest,
                        :now, NULL, :now, NULL,
                        :now)
                """
            ),
            {
                "sid": sub_id,
                "tid": tenant_id,
                "aid": actor_id,
                "cap": capability_id,
                "kinds": kinds,
                "digest": digest,
                "now": now,
            },
        )
        _log.info(
            "auto_subscribed sid=%s tenant=%s cap=%s kinds=%s",
            sub_id,
            tenant_id,
            capability_id,
            kinds,
        )
        return sub_id

    async def list_subscriptions(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID | None = None,
    ) -> list[SubscriptionRef]:
        """Active subscriptions owned by ``ctx.tenant_id``.

        ``capability_id`` filters to one capability when set.
        """
        async with self._session_factory() as session:
            if capability_id is None:
                result = await session.execute(
                    text(
                        """
                        SELECT subscription_id, tenant_id, actor_id, capability_id,
                               event_kinds, webhook_url, webhook_hmac_secret_ref,
                               is_enabled, digest_window,
                               t_valid_from, t_valid_to,
                               t_ingested_at, t_invalidated_at
                        FROM subscriptions
                        WHERE tenant_id = :tid
                          AND t_invalidated_at IS NULL
                        ORDER BY t_valid_from DESC
                        """
                    ),
                    {"tid": ctx.tenant_id},
                )
            else:
                result = await session.execute(
                    text(
                        """
                        SELECT subscription_id, tenant_id, actor_id, capability_id,
                               event_kinds, webhook_url, webhook_hmac_secret_ref,
                               is_enabled, digest_window,
                               t_valid_from, t_valid_to,
                               t_ingested_at, t_invalidated_at
                        FROM subscriptions
                        WHERE tenant_id = :tid
                          AND capability_id = :cap
                          AND t_invalidated_at IS NULL
                        ORDER BY t_valid_from DESC
                        """
                    ),
                    {"tid": ctx.tenant_id, "cap": capability_id},
                )
            return [_row_to_subscription_ref(r) for r in result.mappings().all()]

    async def update_subscription(
        self,
        ctx: TenantContext,
        subscription_id: uuid.UUID,
        *,
        event_kinds: list[str] | None = None,
        webhook_url: str | None = None,
        webhook_hmac_secret_ref: str | None = None,
        is_enabled: bool | None = None,
    ) -> SubscriptionRef:
        """Update mutable fields on an active subscription.

        Only fields with non-``None`` values are written; everything else
        is left untouched. ``event_kinds`` (if provided) is validated
        against the closed vocabulary.

        ``tenant_id`` and ``capability_id`` are immutable — to change
        either, delete the subscription and create a new one. This is
        enforced by the tenant scoping on the lookup.

        Raises :class:`NotFoundError` if the subscription does not exist
        or belongs to a different tenant.
        """
        if event_kinds is not None:
            _validate_event_kinds(event_kinds)

        sets: list[str] = []
        params: dict[str, Any] = {
            "sid": subscription_id,
            "tid": ctx.tenant_id,
        }
        if event_kinds is not None:
            sets.append("event_kinds = :kinds")
            params["kinds"] = list(event_kinds)
        if webhook_url is not None:
            sets.append("webhook_url = :url")
            params["url"] = webhook_url
        if webhook_hmac_secret_ref is not None:
            sets.append("webhook_hmac_secret_ref = :secret")
            params["secret"] = webhook_hmac_secret_ref
        if is_enabled is not None:
            sets.append("is_enabled = :enabled")
            params["enabled"] = is_enabled

        async with self._session_factory() as session, session.begin():
            check = await session.execute(
                text(
                    "SELECT subscription_id FROM subscriptions "
                    "WHERE subscription_id = :sid AND tenant_id = :tid "
                    "  AND t_invalidated_at IS NULL"
                ),
                {"sid": subscription_id, "tid": ctx.tenant_id},
            )
            if check.first() is None:
                raise NotFoundError(f"subscription {subscription_id} not found")

            if sets:
                await session.execute(
                    text(
                        "UPDATE subscriptions SET "
                        + ", ".join(sets)
                        + " WHERE subscription_id = :sid AND tenant_id = :tid"
                    ),
                    params,
                )

            refreshed = await session.execute(
                text(
                    """
                    SELECT subscription_id, tenant_id, actor_id, capability_id,
                           event_kinds, webhook_url, webhook_hmac_secret_ref,
                           is_enabled, digest_window,
                           t_valid_from, t_valid_to,
                           t_ingested_at, t_invalidated_at
                    FROM subscriptions
                    WHERE subscription_id = :sid
                    """
                ),
                {"sid": subscription_id},
            )
            row = refreshed.mappings().first()
            if row is None:  # pragma: no cover — guarded by check above
                raise NotFoundError(f"subscription {subscription_id} not found")
            return _row_to_subscription_ref(row)

    async def delete_subscription(
        self,
        ctx: TenantContext,
        subscription_id: uuid.UUID,
    ) -> None:
        """Soft-delete by setting ``t_invalidated_at``.

        Idempotent: a second call on an already-invalidated row is a
        no-op. Raises :class:`NotFoundError` if the subscription does
        not exist *or* belongs to a different tenant (tenants cannot
        peek at each other's subscriptions through this API).
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            row = await session.execute(
                text(
                    "SELECT subscription_id, t_invalidated_at FROM subscriptions "
                    "WHERE subscription_id = :sid AND tenant_id = :tid"
                ),
                {"sid": subscription_id, "tid": ctx.tenant_id},
            )
            sub = row.first()
            if sub is None:
                raise NotFoundError(f"subscription {subscription_id} not found")
            if sub.t_invalidated_at is not None:
                return
            await session.execute(
                text(
                    "UPDATE subscriptions "
                    "SET t_invalidated_at = :now, t_valid_to = :now "
                    "WHERE subscription_id = :sid"
                ),
                {"sid": subscription_id, "now": now},
            )

    # ------------------------------------------------------------------
    # Event fan-out
    # ------------------------------------------------------------------

    async def emit_event(
        self,
        capability_id: uuid.UUID,
        event_kind: str,
        change_classification: str | None,
        version_before: str | None,
        version_after: str | None,
        fetch_url: str,
        occurred_at: Any | None = None,
    ) -> int:
        """Fan out a capability event to every active subscriber.

        Inserts one ``notifications`` row per matching subscription and
        one ``notification_deliveries`` row per subscription that has a
        ``webhook_url`` (the inbox-only branch skips the deliveries
        write). Returns the number of notifications created.

        Payload-minimal — no body, description, or freeform content is ever
        written. Only the structured ``CapabilityRegistryEvent`` fields are stored.
        """
        _validate_event_kinds([event_kind])
        now = self._clock.now()
        at = occurred_at or now

        async with self._session_factory() as session, session.begin():
            slug = await self._fetch_capability_slug(session, capability_id)
            if slug is None:
                raise NotFoundError(f"capability {capability_id} not found")

            subs = await session.execute(
                text(
                    """
                    SELECT subscription_id, tenant_id, webhook_url
                    FROM subscriptions
                    WHERE capability_id = :cap
                      AND is_enabled = TRUE
                      AND t_invalidated_at IS NULL
                      AND :kind = ANY(event_kinds)
                    """
                ),
                {"cap": capability_id, "kind": event_kind},
            )
            sub_rows = subs.all()

            for s in sub_rows:
                nid = uuid.uuid4()
                await session.execute(
                    text(
                        """
                        INSERT INTO notifications
                            (notification_id, tenant_id, subscription_id,
                             capability_id, capability_slug, event_kind,
                             change_classification, version_before, version_after,
                             occurred_at, fetch_url, status, ts)
                        VALUES (:nid, :tid, :sid,
                                :cap, :slug, :kind,
                                :cls, :vb, :va,
                                :at, :url, 'unread', :now)
                        """
                    ),
                    {
                        "nid": nid,
                        "tid": s.tenant_id,
                        "sid": s.subscription_id,
                        "cap": capability_id,
                        "slug": slug,
                        "kind": event_kind,
                        "cls": change_classification,
                        "vb": version_before,
                        "va": version_after,
                        "at": at,
                        "url": fetch_url,
                        "now": now,
                    },
                )
                if s.webhook_url is not None:
                    await session.execute(
                        text(
                            """
                            INSERT INTO notification_deliveries
                                (delivery_id, notification_id, tenant_id,
                                 webhook_url, attempt_number, status,
                                 attempted_at, next_retry_at, ts)
                            VALUES (gen_random_uuid(), :nid, :tid,
                                    :url, 0, 'pending',
                                    :now, :now, :now)
                            """
                        ),
                        {
                            "nid": nid,
                            "tid": s.tenant_id,
                            "url": s.webhook_url,
                            "now": now,
                        },
                    )

        _log.info(
            "event_emitted cap=%s kind=%s subscribers=%d",
            capability_id,
            event_kind,
            len(sub_rows),
        )
        return len(sub_rows)

    # ------------------------------------------------------------------
    # AutoSubscribeHook adapter
    # ------------------------------------------------------------------

    def adoption_hook(self) -> AutoSubscribeHook:
        """Return a callable matching :class:`AutoSubscribeHook`'s signature.

        Lets ``main.py`` wire ``adoption.auto_subscribe =
        subscriptions.adoption_hook()`` without leaking the protocol
        into call sites.
        """

        async def _hook(
            *,
            session: AsyncSession,
            ctx: TenantContext,
            adoption: AdoptionEventRef,
        ) -> None:
            await self.auto_subscribe(
                session=session,
                tenant_id=adoption.consumer_tenant_id,
                actor_id=ctx.actor_id,
                capability_id=adoption.provider_capability_id,
            )

        return _hook

    # ------------------------------------------------------------------
    # SQL helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetch_tenant_digest_window(session: AsyncSession, tenant_id: uuid.UUID) -> str:
        """Return the tenant's current notification_digest_window or 'none'."""
        result = await session.execute(
            text("SELECT notification_digest_window FROM tenants " "WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        row = result.first()
        if row is None or row.notification_digest_window is None:
            return "none"
        window = str(row.notification_digest_window)
        if window not in _VALID_DIGEST_WINDOWS:
            return "none"
        return window

    @staticmethod
    async def _fetch_capability_slug(session: AsyncSession, capability_id: uuid.UUID) -> str | None:
        result = await session.execute(
            text("SELECT name FROM entities " "WHERE entity_id = :eid AND is_active = TRUE"),
            {"eid": capability_id},
        )
        row = result.first()
        if row is None:
            return None
        return str(row.name)


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_subscription_ref(row: Any) -> SubscriptionRef:
    kinds = row["event_kinds"]
    if not isinstance(kinds, list):
        kinds = list(kinds) if kinds is not None else []
    return SubscriptionRef(
        subscription_id=row["subscription_id"],
        tenant_id=row["tenant_id"],
        actor_id=row["actor_id"],
        capability_id=row["capability_id"],
        event_kinds=list(kinds),
        webhook_url=row["webhook_url"],
        webhook_hmac_secret_ref=row["webhook_hmac_secret_ref"],
        is_enabled=row["is_enabled"],
        digest_window=row["digest_window"],
        t_valid_from=row["t_valid_from"],
        t_valid_to=row["t_valid_to"],
        t_ingested_at=row["t_ingested_at"],
        t_invalidated_at=row["t_invalidated_at"],
    )


__all__ = [
    "AUTO_SUBSCRIBE_EVENT_KINDS",
    "SubscriptionService",
    "VALID_EVENT_KINDS",
]
