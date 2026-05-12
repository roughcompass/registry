"""External-ID registry REST endpoints.

Admin (tenant-admin role required):
  POST   /v1/admin/external-systems           → {slug, ...} (201)
  GET    /v1/admin/external-systems           → list
  DELETE /v1/admin/external-systems/{slug}    → 204  (HttpMethodRouter)

Entity mappings (any authenticated user):
  GET    /v1/entities/{entity_id}/external-ids            → list[ExternalIdRef]
  POST   /v1/entities/{entity_id}/external-ids            → ExternalIdRef (201)
  PATCH  /v1/entities/{entity_id}/external-ids/{pk}       → ExternalIdRef (HttpMethodRouter)
  DELETE /v1/entities/{entity_id}/external-ids/{pk}       → 204           (HttpMethodRouter)

Lookup:
  GET    /v1/entities?external_system=<slug>&external_id=<id>  → EntityRef | 404

All mutation routes (PATCH, DELETE) are registered via HttpMethodRouter so they
honour REGISTRY_HTTP_METHODS_MODE and expose POST-tunneled aliases automatically.

Error mapping:
  NotFoundError       → 404
  ConflictError       → 409
  TenantIsolationError → 404  (avoids leaking entity existence across tenants)
  ValidationError     → 422
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel

from registry.api.auth.context import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER, require_roles
from registry.api.errors import map_catalog_error
from registry.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers._common import get_service
from registry.exceptions import ConflictError, NotFoundError, TenantIsolationError
from registry.service.external_ids import ExternalIdService
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Auth shortcuts
# ---------------------------------------------------------------------------

_admin_required = require_roles([ROLE_ADMIN])
_authenticated = require_roles([ROLE_ADMIN, ROLE_PRODUCER, ROLE_CONSUMER, ROLE_AUDITOR])

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class ExternalSystemCreate(BaseModel):
    slug: str
    display_name: str
    url_template: str | None = None
    description: str | None = None


class ExternalSystemResponse(BaseModel):
    slug: str
    tenant_id: uuid.UUID
    display_name: str
    url_template: str | None
    description: str | None
    created_at: Any  # datetime.datetime — Any to avoid import cycle with schemas


class ExternalIdCreate(BaseModel):
    external_system_slug: str
    external_id: str
    url: str | None = None
    metadata_jsonb: dict[str, Any] | None = None


class ExternalIdPatch(BaseModel):
    url: str | None = None
    metadata_jsonb: dict[str, Any] | None = None


class ExternalIdResponse(BaseModel):
    external_id_pk: uuid.UUID
    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    external_system_slug: str
    external_id: str
    url: str | None
    metadata_jsonb: dict[str, Any] | None


class ExternalIdListResponse(BaseModel):
    """Paginated list envelope for GET /v1/entities/{id}/external-ids.

    Cursor wiring: envelope-only. External-ID mappings per entity are bounded
    (typically 1–10 per entity), so ``next_cursor`` is always ``None`` in
    practice. The wrapper exists for client shape consistency.
    """

    items: list[ExternalIdResponse]
    next_cursor: str | None


class EntityRefResponse(BaseModel):
    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    is_active: bool
    created_at: Any  # datetime.datetime


# ---------------------------------------------------------------------------
# Service accessor
# ---------------------------------------------------------------------------


def _svc(request: Request) -> ExternalIdService:
    return request.app.state.external_ids  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _ref_to_response(ref: Any) -> ExternalIdResponse:
    return ExternalIdResponse(
        external_id_pk=ref.external_id_pk,
        entity_id=ref.entity_id,
        tenant_id=ref.tenant_id,
        external_system_slug=ref.external_system_slug,
        external_id=ref.external_id,
        url=ref.url,
        metadata_jsonb=ref.metadata_jsonb,
    )


def _sys_to_response(d: dict[str, Any]) -> ExternalSystemResponse:
    return ExternalSystemResponse(
        slug=d["slug"],
        tenant_id=d["tenant_id"],
        display_name=d["display_name"],
        url_template=d["url_template"],
        description=d["description"],
        created_at=d["created_at"],
    )


# ---------------------------------------------------------------------------
# Mode settings (read once at module load, matching admin.py pattern)
# ---------------------------------------------------------------------------

_mode, _sep = get_mode_settings()

# ---------------------------------------------------------------------------
# Admin router — /v1/admin/external-systems
# ---------------------------------------------------------------------------

_admin_base = APIRouter(prefix="/v1/admin", tags=["admin: external-systems"])
_admin_mr = HttpMethodRouter(_admin_base, mode=_mode, separator=_sep)


@_admin_base.post(
    "/external-systems",
    response_model=ExternalSystemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an external system (admin)",
)
async def create_external_system(
    body: ExternalSystemCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> ExternalSystemResponse:
    """Register a new external-system slug for the tenant.

    ``slug`` must be unique per tenant.  Duplicate slug returns ``409 Conflict``.
    ``url_template`` may contain ``{external_id}`` which is substituted when
    external-ID mappings are created without an explicit ``url``.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    svc = _svc(request)
    try:
        result = await svc.register_external_system(
            ctx,
            slug=body.slug,
            display_name=body.display_name,
            url_template=body.url_template,
            description=body.description,
        )
    except (ConflictError, NotFoundError, TenantIsolationError) as exc:
        raise map_catalog_error(exc) from exc
    response = _sys_to_response(result)
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


@_admin_base.get(
    "/external-systems",
    response_model=list[ExternalSystemResponse],
    summary="List registered external systems (admin)",
)
async def list_external_systems(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[ExternalSystemResponse]:
    """Return all external systems registered for the tenant, ordered by slug."""
    svc = _svc(request)
    rows = await svc.list_external_systems(ctx)
    return [_sys_to_response(r) for r in rows]


async def _delete_external_system(
    slug: str,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Hard-delete an external system registration.

    Returns ``204 No Content`` on success.  Returns ``404`` when the slug
    does not exist or belongs to a different tenant (avoids leaking cross-tenant
    registry contents).

    Note: existing entity_external_ids rows that reference this slug are not
    automatically removed by this call.  Callers should delete mappings first,
    or re-registration of the same slug will be unblocked once orphans are cleared.
    """
    svc = _svc(request)
    try:
        await svc.delete_external_system(ctx, slug)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_admin_mr.add_mutation_route(
    path="/external-systems/{slug}",
    action="delete",
    handler=_delete_external_system,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)

# Expose for main.py.
external_systems_admin_router = _admin_base

# ---------------------------------------------------------------------------
# Entity mapping router — /v1/entities
# ---------------------------------------------------------------------------

_entities_base = APIRouter(tags=["external-ids"])
_entities_mr = HttpMethodRouter(_entities_base, mode=_mode, separator=_sep)


@_entities_base.get(
    "/v1/entities",
    response_model=EntityRefResponse,
    summary="Lookup entity by external system slug and external ID",
)
async def lookup_entity_by_external_id(
    request: Request,
    external_system: Annotated[
        str,
        Query(description="External system slug (registered via /v1/admin/external-systems)"),
    ],
    external_id: Annotated[
        str,
        Query(description="The raw external ID string as it appears in the upstream system"),
    ],
    ctx: TenantContext = Depends(get_tenant_context),
) -> EntityRefResponse:
    """Return the entity mapped to ``(external_system, external_id)`` for the tenant.

    Returns ``404 Not Found`` when no mapping exists.  The ``external_system``
    and ``external_id`` query parameters are both required.
    """
    svc = _svc(request)
    entity_ref = await svc.lookup_by_external_id(ctx, external_system, external_id)
    if entity_ref is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no entity mapped to external_system={external_system!r} external_id={external_id!r}",
        )
    return EntityRefResponse(
        entity_id=entity_ref.entity_id,
        tenant_id=entity_ref.tenant_id,
        entity_type=entity_ref.entity_type,
        name=entity_ref.name,
        external_id=entity_ref.external_id,
        is_active=entity_ref.is_active,
        created_at=entity_ref.created_at,
    )


@_entities_base.get(
    "/v1/entities/{entity_id}/external-ids",
    response_model=ExternalIdListResponse,
    summary="List external-ID mappings for an entity",
)
async def list_external_ids(
    entity_id: Annotated[str, Path(description="Entity UUID or slug")],
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
) -> ExternalIdListResponse:
    """Return all external-ID mappings for ``entity_id``, ordered by creation time.

    The path segment accepts a UUID or slug-form name.

    Returns an empty ``items`` list when no mappings exist.  Returns ``404``
    when the entity does not exist (service checks ownership via tenant_id).

    Pagination: ``next_cursor`` is always ``None`` — external-ID mappings per
    entity are bounded (typically 1–10 rows), so keyset pagination is not
    wired. The envelope exists for client shape consistency.
    """
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, entity_id)
    except (NotFoundError, TenantIsolationError) as exc:
        raise map_catalog_error(exc) from exc
    refs = await svc.list_external_ids(ctx, resolved.entity_id)
    return ExternalIdListResponse(items=[_ref_to_response(r) for r in refs], next_cursor=None)


@_entities_base.post(
    "/v1/entities/{entity_id}/external-ids",
    response_model=ExternalIdResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an external-ID mapping for an entity",
)
async def add_external_id(
    entity_id: Annotated[str, Path(description="Entity UUID or slug")],
    body: ExternalIdCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(get_tenant_context),
) -> ExternalIdResponse:
    """Create a new external-ID mapping for ``entity_id``.

    The path segment accepts a UUID or slug-form name.

    URL resolution
    --------------
    1. If ``url`` is supplied in the request body it is stored as-is.
    2. Else if the external system has a ``url_template``, the template is
       expanded with ``{external_id}`` replaced by the provided external ID.
    3. Otherwise ``url`` is stored as ``None``.

    Returns ``409 Conflict`` when the
    ``(tenant_id, external_system_slug, external_id)`` triple already exists;
    the message includes the existing ``external_id_pk``.

    Returns ``404`` when:
    - ``entity_id`` does not exist.
    - ``external_system_slug`` is not registered for this tenant.

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
        resolved = await catalog_svc.resolve_entity_handle(ctx, entity_id)
        ref = await svc.add_external_id(
            ctx,
            entity_id=resolved.entity_id,
            external_system_slug=body.external_system_slug,
            external_id=body.external_id,
            url=body.url,
            metadata_jsonb=body.metadata_jsonb,
        )
    except (NotFoundError, ConflictError, TenantIsolationError) as exc:
        raise map_catalog_error(exc) from exc
    response = _ref_to_response(ref)
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


async def _patch_external_id(
    entity_id: Annotated[str, Path(description="Entity UUID or slug")],
    external_id_pk: uuid.UUID,
    body: ExternalIdPatch,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
) -> ExternalIdResponse:
    """Update the ``url`` or ``metadata_jsonb`` of an existing external-ID mapping.

    The path segment accepts a UUID or slug-form name for the entity.

    Returns ``404`` when the mapping does not exist or belongs to a different
    entity/tenant.  Only fields present in the request body are updated.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a debug warning and
    accepts the write.  ETag is computed from the mapping's primary key +
    ``updated_at`` before the write so a stale precondition fails fast.

    There is no detail-GET for an individual external-ID mapping; the client
    can acquire the ETag from the list endpoint body (list does not emit an
    ETag header) or from a prior PATCH response — the ETag for the updated
    record is computed from the returned ``external_id_pk + updated_at`` values.
    """
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, entity_id)
        # Fetch the pre-write mapping to compute the ETag before mutating.
        pre_refs = await svc.list_external_ids(ctx, resolved.entity_id)
        pre_ref = next((r for r in pre_refs if r.external_id_pk == external_id_pk), None)
        if pre_ref is None:
            raise NotFoundError(f"external_id_pk={external_id_pk} not found")
        pre_etag = compute_etag(
            pre_ref.external_id_pk,
            latest_timestamp(pre_ref.updated_at),
        )
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="external_id",
        )
        ref = await svc.update_external_id(
            ctx,
            entity_id=resolved.entity_id,
            external_id_pk=external_id_pk,
            url=body.url,
            metadata_jsonb=body.metadata_jsonb,
        )
    except (NotFoundError, TenantIsolationError) as exc:
        raise map_catalog_error(exc) from exc
    return _ref_to_response(ref)


_entities_mr.add_mutation_route(
    path="/v1/entities/{entity_id}/external-ids/{external_id_pk}",
    action="update",
    handler=_patch_external_id,
    verb="PATCH",
    response_model=ExternalIdResponse,
)


async def _delete_external_id(
    entity_id: Annotated[str, Path(description="Entity UUID or slug")],
    external_id_pk: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
) -> Response:
    """Hard-delete an external-ID mapping by its primary key.

    The path segment accepts a UUID or slug-form name for the entity.

    There is no soft-history: the row is removed permanently.
    Deletion is audit-logged to ``audit_log`` unconditionally.

    Returns ``204 No Content`` on success.  Returns ``404`` when the mapping
    does not exist or belongs to a different tenant (avoids leaking existence).
    """
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        await catalog_svc.resolve_entity_handle(ctx, entity_id)
        await svc.delete_external_id(ctx, external_id_pk)
    except (NotFoundError, TenantIsolationError) as exc:
        raise map_catalog_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_entities_mr.add_mutation_route(
    path="/v1/entities/{entity_id}/external-ids/{external_id_pk}",
    action="delete",
    handler=_delete_external_id,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)

# Expose for main.py.
entity_external_ids_router = _entities_base


__all__ = [
    "ExternalIdCreate",
    "ExternalIdListResponse",
    "ExternalIdPatch",
    "ExternalIdResponse",
    "ExternalSystemCreate",
    "ExternalSystemResponse",
    "EntityRefResponse",
    "external_systems_admin_router",
    "entity_external_ids_router",
]
