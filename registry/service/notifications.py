"""NotificationService — in-catalog inbox for capability events.

Reads from the ``notifications`` table (DDL in migration 0009).
Reads are payload-minimal — only the columns persisted on the row are
returned. The :class:`~registry.types.CapabilityRegistryEvent` dataclass is
the canonical wire format.

Surface
-------
* :meth:`list_notifications` — cursor-paginated list scoped to
  ``ctx.tenant_id``, filterable by status (``unread`` | ``read`` | ``all``).
* :meth:`mark_read` — flip a single row from ``unread`` → ``read``;
  idempotent on already-read or missing rows (no exception).

Cursor format
-------------
``ts`` (TIMESTAMPTZ) is the partition key and the natural ordering. Each
response returns ``next_cursor`` = the ``ts`` ISO-8601 of the last row,
which the next call passes in as ``cursor`` to fetch strictly older rows.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import ValidationError
from registry.types import CapabilityRegistryEvent, Clock, TenantContext

_log = logging.getLogger(__name__)

_VALID_STATUSES: frozenset[str] = frozenset({"unread", "read", "all"})

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500


def _parse_cursor(cursor: str | None) -> datetime.datetime | None:
    """Decode the cursor (ISO-8601 ts). Returns ``None`` for the first page."""
    if cursor is None or cursor == "":
        return None
    try:
        dt = datetime.datetime.fromisoformat(cursor)
    except ValueError as exc:
        raise ValidationError(f"cursor must be an ISO-8601 datetime: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt


def _validate_status(status: str) -> None:
    if status not in _VALID_STATUSES:
        raise ValidationError(f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}")


def _clamp_page_size(page_size: int) -> int:
    if page_size <= 0:
        return _DEFAULT_PAGE_SIZE
    if page_size > _MAX_PAGE_SIZE:
        return _MAX_PAGE_SIZE
    return page_size


class NotificationService:
    """Tenant-scoped inbox reads + read-state updates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def list_notifications(
        self,
        ctx: TenantContext,
        *,
        status: str = "unread",
        cursor: str | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> tuple[list[CapabilityRegistryEvent], str | None]:
        """Return one page of notifications for the caller's tenant.

        Ordering: ``ts DESC`` (newest first). Cursor pagination: the
        next page is fetched by passing the ``next_cursor`` from this
        response back in as ``cursor`` — the service then returns rows
        with ``ts < cursor``.
        """
        _validate_status(status)
        cursor_dt = _parse_cursor(cursor)
        size = _clamp_page_size(page_size)

        params: dict[str, Any] = {"tid": ctx.tenant_id, "lim": size + 1}
        where = ["tenant_id = :tid"]
        if status != "all":
            where.append("status = :status")
            params["status"] = status
        if cursor_dt is not None:
            where.append("ts < :cursor")
            params["cursor"] = cursor_dt

        sql = (
            "SELECT notification_id, tenant_id, subscription_id, "
            "       capability_id, capability_slug, event_kind, "
            "       change_classification, version_before, version_after, "
            "       occurred_at, fetch_url, ts "
            "FROM notifications "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY ts DESC, notification_id "
            "LIMIT :lim"
        )

        async with self._session_factory() as session:
            result = await session.execute(text(sql), params)
            rows = result.mappings().all()

        next_cursor: str | None = None
        if len(rows) > size:
            # Trim the lookahead row; the trimmed row's ts is the cursor.
            next_cursor = rows[size - 1]["ts"].isoformat()
            rows = rows[:size]

        return [_row_to_event(r) for r in rows], next_cursor

    async def mark_read(
        self,
        ctx: TenantContext,
        notification_id: uuid.UUID,
    ) -> None:
        """Flip ``status`` from ``unread`` to ``read``.

        Idempotent: missing rows and already-read rows are no-ops (no
        exception). Tenant scoping is enforced — a caller cannot toggle
        another tenant's notification.
        """
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE notifications "
                    "SET status = 'read' "
                    "WHERE notification_id = :nid "
                    "  AND tenant_id = :tid "
                    "  AND status = 'unread'"
                ),
                {"nid": notification_id, "tid": ctx.tenant_id},
            )


def _row_to_event(row: Any) -> CapabilityRegistryEvent:
    return CapabilityRegistryEvent(
        notification_id=row["notification_id"],
        tenant_id=row["tenant_id"],
        subscription_id=row["subscription_id"],
        capability_id=row["capability_id"],
        capability_slug=row["capability_slug"],
        event_kind=row["event_kind"],
        change_classification=row["change_classification"],
        version_before=row["version_before"],
        version_after=row["version_after"],
        occurred_at=row["occurred_at"],
        fetch_url=row["fetch_url"],
    )


def event_to_dict(event: CapabilityRegistryEvent) -> dict[str, Any]:
    """Serialise a CapabilityRegistryEvent for JSON responses (REST + MCP).

    UUIDs render as strings; datetimes as ISO-8601. This is the canonical
    payload shape consumed by both surfaces — keep them aligned by going
    through this single function.
    """
    return {
        "notification_id": str(event.notification_id),
        "tenant_id": str(event.tenant_id),
        "subscription_id": (str(event.subscription_id) if event.subscription_id else None),
        "capability_id": str(event.capability_id),
        "capability_slug": event.capability_slug,
        "event_kind": event.event_kind,
        "change_classification": event.change_classification,
        "version_before": event.version_before,
        "version_after": event.version_after,
        "occurred_at": event.occurred_at.isoformat(),
        "fetch_url": event.fetch_url,
    }


__all__ = [
    "NotificationService",
    "event_to_dict",
]
