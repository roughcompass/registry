"""Subscription REST endpoints.

Surface:

  POST   /v1/capabilities/{capability_id}/subscriptions  → 201 + {subscription_id}
  GET    /v1/capabilities/{capability_id}/subscriptions  → list[SubscriptionResponse]
  PATCH  /v1/subscriptions/{subscription_id}             → SubscriptionResponse
  DELETE /v1/subscriptions/{subscription_id}             → 204

PATCH and DELETE are registered via :class:`HttpMethodRouter` so the
``REGISTRY_HTTP_METHODS_MODE`` env var controls whether POST-tunneled
aliases are also exposed (``POST /v1/subscriptions/{id}:update`` etc.).

Authorisation
-------------
``consumer``, ``producer``, or ``admin`` role required for create/list/
update/delete (the consumer side of a tenant owns its subscriptions).

Error mapping
-------------
- ``NotFoundError``    → 404
- ``ValidationError``  → 422 (closed vocabulary for event_kinds)
- ``PermissionError``  → 403 (visibility chokepoint)
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field

from registry.api.auth.context import ROLE_ADMIN, ROLE_CONSUMER, ROLE_PRODUCER, require_roles
from registry.api.errors import map_catalog_error
from registry.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.routers._common import get_service
from registry.api.schemas import Links, SubscriptionListResponse, SubscriptionResponse
from registry.exceptions import NotFoundError, ValidationError
from registry.service.subscriptions import SubscriptionService
from registry.types import SubscriptionRef, TenantContext

# ---------------------------------------------------------------------------
# Auth shortcuts
# ---------------------------------------------------------------------------

_sub_roles = [ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN]
_sub_required = require_roles(_sub_roles)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class SubscriptionCreate(BaseModel):
    event_kinds: list[str] = Field(..., min_length=1)
    webhook_url: str | None = None
    webhook_hmac_secret_ref: str | None = None


class SubscriptionUpdate(BaseModel):
    event_kinds: list[str] | None = Field(default=None, min_length=1)
    webhook_url: str | None = None
    webhook_hmac_secret_ref: str | None = None
    is_enabled: bool | None = None


# ---------------------------------------------------------------------------
# Service accessor
# ---------------------------------------------------------------------------


def _svc(request: Request) -> SubscriptionService:
    return request.app.state.subscriptions  # type: ignore[no-any-return]


def _ref_to_response(ref: SubscriptionRef, *, audit: bool = False, include_links: bool = False) -> SubscriptionResponse:
    """Convert a SubscriptionRef to the response model.

    Default shape is UI-flavoured (core subscription fields only). Pass
    ``audit=True`` to populate the bitemporal audit fields — used by
    ``?view=audit`` on the parent endpoint.

    Pass ``include_links=True`` on single-resource responses (PATCH) to
    include ``_links.self`` + ``_links.capability``; list endpoint items
    intentionally omit links to keep payload size manageable.
    """
    base: dict = dict(
        subscription_id=ref.subscription_id,
        tenant_id=ref.tenant_id,
        actor_id=ref.actor_id,
        capability_id=ref.capability_id,
        event_kinds=list(ref.event_kinds),
        webhook_url=ref.webhook_url,
        webhook_hmac_secret_ref=ref.webhook_hmac_secret_ref,
        is_enabled=ref.is_enabled,
        digest_window=ref.digest_window,
    )
    if audit:
        base.update(
            valid_from=ref.t_valid_from,
            valid_to=ref.t_valid_to,
            ingested_at=ref.t_ingested_at,
            invalidated_at=ref.t_invalidated_at,
        )
    if include_links:
        base["links"] = Links(
            self=f"/v1/subscriptions/{ref.subscription_id}",
            capability=f"/v1/capabilities/{ref.capability_id}",
        )
    return SubscriptionResponse(**base)


# ---------------------------------------------------------------------------
# Mode settings
# ---------------------------------------------------------------------------

_mode, _sep = get_mode_settings()


# ---------------------------------------------------------------------------
# Capability-scoped POST + GET
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/v1/capabilities", tags=["subscriptions"])

# Reusable view query param annotation for subscription endpoints.
_ViewParam = Annotated[
    str,
    Query(
        description=(
            "Response shape. ``default`` is the standard UI-flavoured shape. "
            "``audit`` adds bitemporal columns (valid_from / valid_to / "
            "ingested_at / invalidated_at) for audit / compliance consumers."
        )
    ),
]


def _validate_view(view: str) -> bool:
    """Raise 422 if view is not a known value; return True when audit mode."""
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    return view == "audit"


@router.post(
    "/{capability_id}/subscriptions",
    status_code=status.HTTP_201_CREATED,
    summary="Create a subscription for a capability",
)
async def create_subscription(
    capability_id: Annotated[str, Path(description="Capability UUID or slug")],
    body: SubscriptionCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_sub_required),
) -> dict[str, str]:
    """Create an active subscription owned by the caller's tenant.

    The path segment accepts a UUID or slug-form name. Visibility is enforced
    before the row is written. Returns ``{"subscription_id": "<uuid>"}``; the
    full record can be retrieved via the list endpoint.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, capability_id)
        sid = await svc.create_subscription(
            ctx=ctx,
            capability_id=resolved.entity_id,
            event_kinds=body.event_kinds,
            webhook_url=body.webhook_url,
            webhook_hmac_secret_ref=body.webhook_hmac_secret_ref,
        )
    except (NotFoundError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    response_body = {"subscription_id": str(sid)}
    await idem.persist(ctx, 201, response_body)
    return response_body


@router.get(
    "/{capability_id}/subscriptions",
    response_model=SubscriptionListResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="List the caller's subscriptions for a capability",
)
async def list_subscriptions_for_capability(
    capability_id: Annotated[str, Path(description="Capability UUID or slug")],
    request: Request,
    view: _ViewParam = "default",
    ctx: TenantContext = Depends(_sub_required),
) -> SubscriptionListResponse:
    """Active subscriptions owned by ``ctx.tenant_id`` for this capability.

    The path segment accepts a UUID or slug-form name. Tenants only see
    their own subscriptions through this endpoint.

    Pass ``?view=audit`` to include bitemporal columns in the response.

    Pagination: ``next_cursor`` is always ``None`` — subscriptions per
    capability per tenant are bounded (typically 1–5 rows), so keyset
    pagination is not wired. The envelope exists for client shape consistency.
    """
    audit = _validate_view(view)
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, capability_id)
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    refs = await svc.list_subscriptions(ctx=ctx, capability_id=resolved.entity_id)
    return SubscriptionListResponse(
        items=[_ref_to_response(r, audit=audit) for r in refs],
        next_cursor=None,
    )


# ---------------------------------------------------------------------------
# Subscription-scoped PATCH + DELETE — via HttpMethodRouter
# ---------------------------------------------------------------------------

mutation_router = APIRouter(prefix="/v1/subscriptions", tags=["subscriptions"])
_mut_mr = HttpMethodRouter(mutation_router, mode=_mode, separator=_sep)


async def _update_subscription_handler(
    subscription_id: uuid.UUID,
    body: SubscriptionUpdate,
    request: Request,
    view: _ViewParam = "default",
    ctx: TenantContext = Depends(_sub_required),
) -> SubscriptionResponse:
    """Update mutable fields on an active subscription.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a debug warning and
    accepts the write.  ETag is computed from the subscription identifier +
    its ``t_ingested_at`` timestamp before the write so a stale precondition
    fails fast.

    There is no detail-GET endpoint for subscriptions — clients that need an
    ETag should parse it from the subscription create response or from the list
    endpoint item (list items do not currently emit ETags, so the safest source
    is a prior PATCH response).

    Pass ``?view=audit`` to include bitemporal columns in the response.
    """
    audit = _validate_view(view)
    svc = _svc(request)
    try:
        # Fetch the pre-write snapshot to compute the ETag. The update_subscription
        # call below re-fetches inside its own transaction; this extra read is
        # necessary so a stale If-Match fails before any mutation runs.
        pre_refs = await svc.list_subscriptions(ctx=ctx)
        pre_ref = next((r for r in pre_refs if r.subscription_id == subscription_id), None)
        if pre_ref is None:
            raise NotFoundError(f"subscription {subscription_id} not found")
        pre_etag = compute_etag(
            pre_ref.subscription_id,
            latest_timestamp(pre_ref.t_ingested_at),
        )
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="subscription",
        )
        ref = await svc.update_subscription(
            ctx=ctx,
            subscription_id=subscription_id,
            event_kinds=body.event_kinds,
            webhook_url=body.webhook_url,
            webhook_hmac_secret_ref=body.webhook_hmac_secret_ref,
            is_enabled=body.is_enabled,
        )
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _ref_to_response(ref, audit=audit, include_links=True)


async def _delete_subscription_handler(
    subscription_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_sub_required),
) -> Response:
    """Soft-delete (sets t_invalidated_at). Idempotent."""
    svc = _svc(request)
    try:
        await svc.delete_subscription(ctx=ctx, subscription_id=subscription_id)
    except NotFoundError as exc:
        raise map_catalog_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_mut_mr.add_mutation_route(
    path="/{subscription_id}",
    action="update",
    handler=_update_subscription_handler,
    verb="PATCH",
    response_model=SubscriptionResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="Update a subscription",
)

_mut_mr.add_mutation_route(
    path="/{subscription_id}",
    action="delete",
    handler=_delete_subscription_handler,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Soft-delete a subscription",
)


__all__ = ["router", "mutation_router"]
