"""Notification inbox REST endpoints.

Surface:

  GET  /v1/notifications?status={unread|read|all}&cursor=...&page_size=50
        → {items: list[CapabilityRegistryEvent], next_cursor: str | None}
  POST /v1/notifications/{id}:mark-read → 204

Payload is :class:`CapabilityRegistryEvent` — payload-minimal (no body text,
descriptions, or freeform content). Consumers must follow ``fetch_url`` to
retrieve the canonical record.

The POST verb on the mark-read endpoint uses the action-suffix style
(``:mark-read``) regardless of ``REGISTRY_HTTP_METHODS_MODE`` because the
operation is not a CRUD primitive (PATCH would imply partial-update of
the notification row, which is misleading for a status flip).

Authorisation
-------------
``consumer``, ``producer``, or ``admin``. Auditor cannot mark notifications
read but can list them — this matches the audit-trail constraint.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel

from registry.api.auth.context import ROLE_ADMIN, ROLE_CONSUMER, ROLE_PRODUCER, require_roles
from registry.api.errors import map_catalog_error
from registry.api.middleware.tenant import get_tenant_context
from registry.exceptions import ValidationError
from registry.service.notifications import NotificationService, event_to_dict
from registry.types import TenantContext

_mutate_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN])


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class NotificationItem(BaseModel):
    notification_id: str
    tenant_id: str
    subscription_id: str | None
    capability_id: str
    capability_slug: str
    event_kind: str
    change_classification: str | None
    version_before: str | None
    version_after: str | None
    occurred_at: str
    fetch_url: str


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Service accessor
# ---------------------------------------------------------------------------


def _svc(request: Request) -> NotificationService:
    return request.app.state.notifications  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# GET /v1/notifications
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/v1/notifications", tags=["notifications"])


@router.get(
    "",
    response_model=NotificationListResponse,
    summary="List notifications for the caller's tenant",
)
async def list_notifications(
    request: Request,
    status_filter: str = Query("unread", alias="status"),
    cursor: str | None = Query(None),
    page_size: int = Query(50, ge=1, le=500),
    view: Annotated[
        str,
        Query(
            description=(
                "Response shape. ``default`` is the standard UI-flavoured shape. "
                "``audit`` is accepted for API consistency but is currently a "
                "no-op here — NotificationItem has no bitemporal columns to expose. "
                "This parameter is reserved for future use."
            )
        ),
    ] = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> NotificationListResponse:
    """Cursor-paginated inbox view. Newest notifications first.

    ``status`` ∈ {``unread``, ``read``, ``all``}; default is ``unread``.
    ``next_cursor`` is non-null only when the page is full and there are
    more rows to read; pass it back in as ``cursor`` for the next page.

    ``view`` is accepted for API consistency but is currently a no-op —
    notification items have no bitemporal columns to expose.
    """
    svc = _svc(request)
    try:
        events, next_cursor = await svc.list_notifications(
            ctx=ctx,
            status=status_filter,
            cursor=cursor,
            page_size=page_size,
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return NotificationListResponse(
        items=[NotificationItem(**event_to_dict(e)) for e in events],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# POST /v1/notifications/{id}:mark-read
# ---------------------------------------------------------------------------


@router.post(
    "/{notification_id}:mark-read",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Mark a notification as read",
)
async def mark_read(
    notification_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_mutate_required),
) -> Response:
    """Flip ``status`` from ``unread`` to ``read``.

    Idempotent — repeated calls and unknown ids both succeed silently.
    Tenant scoping is enforced by the service.
    """
    svc = _svc(request)
    await svc.mark_read(ctx=ctx, notification_id=notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
