"""Adoption REST endpoints.

Consumer-side adoption surface for cross-tenant capability dependencies:

  POST   /v1/capabilities/{provider_cap_id}/adoptions  → AdoptionEventRef (201)
  GET    /v1/capabilities/{provider_cap_id}/adoptions  → list[AdoptionEventRef]
  DELETE /v1/capabilities/{provider_cap_id}/adoptions/{adoption_id}  → 204

The DELETE route is registered via :class:`HttpMethodRouter` so the
``REGISTRY_HTTP_METHODS_MODE`` env var controls the exposed surface
(REST / POST-tunneled alias / both).

Authorisation
-------------
``producer`` or ``admin`` role required. The service layer additionally
asserts that ``ctx.tenant_id == consumer_tenant_id`` for adopt/unadopt
(no adopt-on-behalf-of).

Error mapping
-------------
- ``NotFoundError``    → 404
- ``ValidationError``  → 422
- ``PermissionError``  → 403
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel

from registry.api.auth.context import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER, require_roles
from registry.api.errors import map_catalog_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.routers._common import get_service
from registry.api.schemas import AdoptionListResponse, AdoptionResponse, Links
from registry.exceptions import NotFoundError, ValidationError
from registry.service.adoption import AdoptionService
from registry.types import AdoptionEventRef, TenantContext

# ---------------------------------------------------------------------------
# Auth shortcuts
# ---------------------------------------------------------------------------

_adopt_required = require_roles([ROLE_PRODUCER, ROLE_ADMIN])
_list_adoptions_required = require_roles([ROLE_PRODUCER, ROLE_ADMIN, ROLE_CONSUMER, ROLE_AUDITOR])

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class AdoptionCreate(BaseModel):
    intent: str | None = None
    version_pin: str | None = None


# ---------------------------------------------------------------------------
# Service accessor
# ---------------------------------------------------------------------------


def _svc(request: Request) -> AdoptionService:
    return request.app.state.adoption  # type: ignore[no-any-return]


def _ref_to_response(
    ref: AdoptionEventRef,
    *,
    audit: bool = False,
    provider_cap_handle: str | None = None,
    include_links: bool = False,
) -> AdoptionResponse:
    """Convert an AdoptionEventRef to the response model.

    Default shape is UI-flavoured (core identifiers and intent fields only).
    Pass ``audit=True`` to populate the bitemporal audit fields — used by
    ``?view=audit`` on the parent endpoint.

    Pass ``include_links=True`` on single-resource responses (POST 201) to
    include ``_links.self`` + ``_links.capability``; list endpoint items
    intentionally omit links. ``provider_cap_handle`` is the address form the
    caller used (slug or UUID) so the capability URL mirrors the request path.
    """
    base: dict = dict(
        adoption_id=ref.adoption_id,
        tenant_id=ref.tenant_id,
        provider_capability_id=ref.provider_capability_id,
        consumer_tenant_id=ref.consumer_tenant_id,
        actor_id=ref.actor_id,
        intent=ref.intent,
        version_pin=ref.version_pin,
    )
    if audit:
        base.update(
            valid_from=ref.t_valid_from,
            valid_to=ref.t_valid_to,
            ingested_at=ref.t_ingested_at,
            invalidated_at=ref.t_invalidated_at,
        )
    if include_links:
        cap_handle = provider_cap_handle or str(ref.provider_capability_id)
        base["links"] = Links(
            self=f"/v1/capabilities/{cap_handle}/adoptions/{ref.adoption_id}",
            capability=f"/v1/capabilities/{cap_handle}",
        )
    return AdoptionResponse(**base)


# ---------------------------------------------------------------------------
# Mode settings (read once at module load)
# ---------------------------------------------------------------------------

_mode, _sep = get_mode_settings()

# ---------------------------------------------------------------------------
# GET / POST router — standard FastAPI routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/v1/capabilities", tags=["adoptions"])


@router.post(
    "/{provider_cap_id}/adoptions",
    response_model=AdoptionResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Adopt a provider capability (cross-tenant)",
)
async def adopt_capability(
    provider_cap_id: Annotated[str, Path(description="Provider capability UUID or slug")],
    body: AdoptionCreate,
    request: Request,
    view: Annotated[
        str,
        Query(
            description=(
                "Response shape. ``default`` is the standard UI-flavoured shape. "
                "``audit`` adds bitemporal columns (valid_from / valid_to / "
                "ingested_at / invalidated_at) for audit / compliance consumers."
            )
        ),
    ] = "default",
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_adopt_required),
) -> AdoptionResponse:
    """Record an adoption event + provides_to edge.

    The path segment accepts a UUID or a slug-form name. The consumer
    tenant is ``ctx.tenant_id``. Returns ``201`` with the newly-created
    adoption row. ``409`` if an active adoption already exists for the
    (consumer, capability) pair (uniqueness constraint).

    Pass ``?view=audit`` to include bitemporal columns in the response.
    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    audit = view == "audit"
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, provider_cap_id)
        ref = await svc.adopt(
            ctx=ctx,
            provider_capability_id=resolved.entity_id,
            consumer_tenant_id=ctx.tenant_id,
            intent=body.intent,
            version_pin=body.version_pin,
        )
    except (NotFoundError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    response = _ref_to_response(ref, audit=audit, provider_cap_handle=provider_cap_id, include_links=True)
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


@router.get(
    "/{provider_cap_id}/adoptions",
    response_model=AdoptionListResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="List active adoptions for a capability",
)
async def list_adoptions(
    provider_cap_id: Annotated[str, Path(description="Provider capability UUID or slug")],
    request: Request,
    view: Annotated[
        str,
        Query(
            description=(
                "Response shape. ``default`` is the standard UI-flavoured shape. "
                "``audit`` adds bitemporal columns (valid_from / valid_to / "
                "ingested_at / invalidated_at) for audit / compliance consumers."
            )
        ),
    ] = "default",
    ctx: TenantContext = Depends(_list_adoptions_required),
) -> AdoptionListResponse:
    """Return the calling tenant's active adoption for this capability,
    if any.

    Scoped to the caller's tenant — listing other tenants' adoptions for
    the same capability is not supported through this endpoint (use the
    projection endpoints for the provider-side view).

    Pass ``?view=audit`` to include bitemporal columns in the response.

    Pagination: ``next_cursor`` is always ``None`` — adoptions per capability
    per tenant are bounded (at most one active row), so keyset pagination is
    not wired. The envelope exists for client shape consistency.
    """
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    audit = view == "audit"
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, provider_cap_id)
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    ref = await svc.get_active_adoption(
        consumer_tenant_id=ctx.tenant_id,
        provider_capability_id=resolved.entity_id,
    )
    adoption_items = [_ref_to_response(ref, audit=audit)] if ref is not None else []
    return AdoptionListResponse(items=adoption_items, next_cursor=None)


# ---------------------------------------------------------------------------
# DELETE router — via HttpMethodRouter (POST-tunneled alias too)
# ---------------------------------------------------------------------------

mutation_router = APIRouter(prefix="/v1/capabilities", tags=["adoptions"])
_mut_mr = HttpMethodRouter(mutation_router, mode=_mode, separator=_sep)


async def _unadopt_capability(
    provider_cap_id: Annotated[str, Path(description="Provider capability UUID or slug")],
    adoption_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_adopt_required),
) -> Response:
    """Soft-delete by setting t_invalidated_at. The provides_to edge is
    retained so historical bi-temporal traversal still surfaces the
    relationship.

    Idempotent: calling on an already-invalidated adoption is a no-op
    (returns 204).
    """
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        await catalog_svc.resolve_entity_handle(ctx, provider_cap_id)
        await svc.unadopt(ctx=ctx, adoption_id=adoption_id)
    except (NotFoundError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_mut_mr.add_mutation_route(
    path="/{provider_cap_id}/adoptions/{adoption_id}",
    action="unadopt",
    handler=_unadopt_capability,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Soft-delete (unadopt) an adoption",
)


__all__ = ["router", "mutation_router"]
