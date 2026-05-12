"""POST/GET/PATCH/DELETE /v1/capabilities.

GET /v1/capabilities/{entity_id} accepts an optional ?as_of= query parameter
for bi-temporal time-travel and returns CapabilityDetailResponse (a superset
of the basic CapabilityResponse shape).

PATCH and DELETE handlers are registered via HttpMethodRouter so
REGISTRY_HTTP_METHODS_MODE controls the exposed surface (REST, POST-tunneled,
or both). The mutation router is exposed as `mutation_router` for inclusion
in main.py.

PATCH /v1/capabilities/{entity_id}/visibility sets the visibility of a
capability via VisibilityService. Requires producer or admin role.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from registry.api.auth.context import ROLE_ADMIN, ROLE_PRODUCER, require_roles
from registry.api.errors import map_catalog_error
from registry.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers._common import edge_to_item as _edge_to_item_shared
from registry.api.routers._common import get_service, to_response
from registry.api.schemas import (
    ArtifactResponse,
    CapabilityDetailResponse,
    CapabilityResponse,
    CreateCapabilityRequest,
    EdgeRefItem,
    Links,
    SetVisibilityRequest,
    UpdateEntityRequest,
)
from registry.exceptions import CatalogError, NotFoundError, ValidationError
from registry.service.temporal import normalize_utc
from registry.service.visibility import VisibilityService
from registry.types import CapabilityRecord, EdgeRef, TenantContext

# Producer or admin required to mutate visibility.
_producer_or_admin = require_roles([ROLE_PRODUCER, ROLE_ADMIN])

router = APIRouter(prefix="/v1/capabilities", tags=["capabilities"])

# Keys accepted in the `?include=` query param on GET /v1/capabilities/{handle}.
# Each value adds a sub-object to the response with bounded result counts —
# callers needing the full set use the dedicated endpoints documented in
# the truncation pointer.
_VALID_INCLUDES: frozenset[str] = frozenset(
    {"components", "depends_on", "external_ids", "interface"},
)

# Per-include cap. When the result set hits this, the response carries
# truncated=true plus a `next` URL pointing at the dedicated endpoint.
# Raised from 50 to 200 in ERG-T07 to cover real-world capability fan-out
# (design systems with 100+ components, services with 50+ dependencies).
_INCLUDE_CAP: int = 200


def _visibility_service(request: Request) -> VisibilityService:
    svc: VisibilityService = request.app.state.visibility
    return svc


def _edge_to_item(edge: EdgeRef, *, audit: bool = False) -> EdgeRefItem:
    """Convert an EdgeRef to the response item.

    Delegates to the shared helper in ``_common`` so the same audit-shape
    logic is used consistently across all routers that emit edges.
    """
    return _edge_to_item_shared(edge, audit=audit)


def _fact_to_artifact(f: object, *, audit: bool = False) -> ArtifactResponse:
    common = {
        "fact_id": f.fact_id,  # type: ignore[attr-defined]
        "category": f.category,  # type: ignore[attr-defined]
        "title": getattr(f, "title", None),
        "body": f.body,  # type: ignore[attr-defined]
        "body_format": getattr(f, "body_format", None),
        "created_at": f.t_ingested_at,  # type: ignore[attr-defined]
    }
    if audit:
        return ArtifactResponse(
            **common,
            tenant_id=f.tenant_id,  # type: ignore[attr-defined]
            entity_id=f.entity_id,  # type: ignore[attr-defined]
            is_authoritative=f.is_authoritative,  # type: ignore[attr-defined]
            valid_from=f.t_valid_from,  # type: ignore[attr-defined]
            valid_to=f.t_valid_to,  # type: ignore[attr-defined]
            ingested_at=f.t_ingested_at,  # type: ignore[attr-defined]
            invalidated_at=f.t_invalidated_at,  # type: ignore[attr-defined]
        )
    return ArtifactResponse(**common)


def _to_detail_response(
    record: CapabilityRecord,
    as_of_dt: object | None = None,
    *,
    audit: bool = False,
    handle: str | None = None,
    facts_categories: frozenset[str] | None = None,
    facts_limit: int | None = None,
) -> CapabilityDetailResponse:
    # Apply the facts filter before serialising. Default (no filter) keeps
    # every authoritative fact; categories filter selects a subset; limit
    # caps the count after filtering.
    facts = record.facts
    if facts_categories is not None:
        facts = [f for f in facts if f.category in facts_categories]
    if facts_limit is not None and facts_limit >= 0:
        facts = facts[:facts_limit]

    base_kwargs: dict[str, object] = {
        "entity_id": record.entity.entity_id,
        "entity_type": record.entity.entity_type,
        "name": record.entity.name,
        "external_id": record.entity.external_id,
        "created_at": record.entity.created_at,
        "lifecycle": record.lifecycle,
        "attributes": record.attributes,
        "facts": [_fact_to_artifact(f, audit=audit) for f in facts],
        "edges_out": [_edge_to_item(e, audit=audit) for e in record.edges_out],
        "edges_in": [_edge_to_item(e, audit=audit) for e in record.edges_in],
    }
    if audit:
        base_kwargs.update(
            tenant_id=record.entity.tenant_id,
            is_active=record.entity.is_active,
            superseded_facts_count=record.superseded_facts_count,
            as_of=as_of_dt,
        )
    # Compute _links from the address form the caller used (slug or UUID),
    # so slug callers get back slug URLs and UUID callers get back UUID URLs.
    h = handle if handle is not None else str(record.entity.entity_id)
    base_kwargs["links"] = Links(
        self=f"/v1/capabilities/{h}",
        artifacts=f"/v1/capabilities/{h}/artifacts",
        dependencies=f"/v1/capabilities/{h}/dependencies",
        interface=f"/v1/capabilities/{h}/interface",
    )
    return CapabilityDetailResponse(**base_kwargs)  # type: ignore[arg-type]


def _parse_includes(include: str | None) -> set[str]:
    """Parse the `?include=` CSV. Raise 422 on unknown values."""
    if include is None or not include.strip():
        return set()
    requested = {v.strip() for v in include.split(",") if v.strip()}
    unknown = requested - _VALID_INCLUDES
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"unknown include values: {sorted(unknown)}. " f"Known: {sorted(_VALID_INCLUDES)}"),
        )
    return requested


@router.post("", response_model=CapabilityResponse, status_code=status.HTTP_201_CREATED)
async def create_capability(
    body: CreateCapabilityRequest,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_producer_or_admin),
) -> CapabilityResponse:
    """Create a new capability.

    Honours ``X-Idempotency-Key`` (optional). Resend with the same key
    + same body → returns the original response. Same key + different
    body → 409 with ``code: "idempotency_key_conflict"``.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    service = get_service(request)
    try:
        entity_ref = await service.create_entity(
            ctx,
            entity_type="capability",
            name=body.name,
            external_id=body.external_id,
            capability_type=body.capability_type,
            attributes=body.attributes,
            valid_from=body.valid_from,
        )
        record = await service.get_full_capability(ctx, entity_ref.entity_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    response = to_response(record)
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


@router.get(
    "/{entity_id}",
    response_model=CapabilityDetailResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
)
async def get_capability(
    entity_id: Annotated[
        str,
        Path(description="Capability UUID or slug-form name (e.g. 'salt-design-system')"),
    ],
    request: Request,
    as_of: Annotated[
        str | None,
        Query(description="ISO-8601 UTC datetime for bi-temporal time-travel"),
    ] = None,
    include: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated list of sub-resources to expand. "
                "Known values: components, depends_on, external_ids, interface. "
                "Each expansion is capped at 200 items — `truncated: true` + a `next` URL signal overflow."
            ),
        ),
    ] = None,
    view: Annotated[
        str,
        Query(
            description=(
                "Response shape. `default` (UI-flavoured) is the standard "
                "minimal shape every endpoint returns. `audit` adds "
                "bitemporal columns (valid_from / valid_to / ingested_at / "
                "invalidated_at), tenant_id, and supersession metadata for "
                "audit / compliance consumers."
            ),
        ),
    ] = "default",
    facts_categories: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated list of fact categories to include in the "
                "`facts` field (e.g. `release_note,overview`). Default: no "
                "filter — every category is returned. Use this to narrow a "
                "fat detail response, e.g. fetch Salt + just its release "
                "notes in one call."
            ),
        ),
    ] = None,
    facts_limit: Annotated[
        int | None,
        Query(
            ge=0,
            le=500,
            description=(
                "Cap the number of facts returned (applied after the "
                "category filter). Default: no cap. Useful when a capability "
                "has hundreds of facts and the UI only renders the top N."
            ),
        ),
    ] = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> CapabilityDetailResponse:
    """Return the full capability record.

    The path segment accepts either a UUID or a slug-form name — they
    resolve to the same record. Slugs are case-insensitive against the
    stored `name` column.

    Optional `?as_of=` activates bi-temporal time-travel.

    Optional `?include=` adds bounded sub-resources to the response. Use
    this to collapse "fetch capability + components + facts + external
    IDs" from four round-trips into one.

    Optional `?view=audit` returns the full bitemporal + tenant-id +
    supersession audit shape. Default `view=default` omits those fields.
    """
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    audit = view == "audit"

    service = get_service(request)
    as_of_dt = None
    if as_of is not None:
        from datetime import datetime  # noqa: PLC0415

        try:
            as_of_dt = normalize_utc(datetime.fromisoformat(as_of))
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"as_of must be a timezone-aware ISO-8601 datetime: {exc}",
            ) from exc

    include_set = _parse_includes(include)

    try:
        resolved = await service.resolve_entity_handle(ctx, entity_id, as_of=as_of_dt)
        record = await service.get_full_capability(ctx, resolved.entity_id, as_of=as_of_dt)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    facts_categories_set: frozenset[str] | None = None
    if facts_categories is not None:
        facts_categories_set = frozenset(c.strip() for c in facts_categories.split(",") if c.strip())

    response = _to_detail_response(
        record,
        as_of_dt,
        audit=audit,
        handle=entity_id,
        facts_categories=facts_categories_set,
        facts_limit=facts_limit,
    )

    if include_set:
        # The handle used in `next` URLs is whatever the caller passed in —
        # so a caller addressing by slug gets back slug URLs, a caller
        # using a UUID gets back UUID URLs. Less surprising.
        includes = request.app.state.includes
        if "components" in include_set:
            response.components = await includes.expand_components(
                ctx,
                resolved.entity_id,
                handle_for_next=entity_id,
            )
        if "depends_on" in include_set:
            response.depends_on = await includes.expand_depends_on(
                ctx,
                resolved.entity_id,
                handle_for_next=entity_id,
            )
        if "external_ids" in include_set:
            response.external_ids = await includes.expand_external_ids(ctx, resolved.entity_id)
        if "interface" in include_set:
            response.interface = await includes.expand_interface(ctx, resolved.entity_id, as_of=as_of_dt)

    # Compute the ETag from the most-recent transaction timestamp this
    # response reflects: the entity row + the latest fact + the latest edge.
    latest = latest_timestamp(
        record.entity.created_at,
        *(f.t_ingested_at for f in record.facts),
        *(e.t_ingested_at for e in record.edges_out),
        *(e.t_ingested_at for e in record.edges_in),
    )
    etag = compute_etag(record.entity.entity_id, latest)
    # Build a JSONResponse so we can attach the ETag header alongside the
    # serialised body. FastAPI's default response builder uses the
    # response_model + exclude_unset + by_alias rules; we re-dump here
    # mirroring them.
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    body = response.model_dump(by_alias=True, exclude_unset=True, mode="json")
    return JSONResponse(content=body, headers={"ETag": etag})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Mutation handlers — registered via HttpMethodRouter
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Visibility mutation handler
# ---------------------------------------------------------------------------


async def set_visibility_handler(
    entity_id: str,
    body: SetVisibilityRequest,
    request: Request,
    ctx: TenantContext = Depends(_producer_or_admin),
) -> CapabilityResponse:
    """PATCH /v1/capabilities/{entity_id} — update visibility and shared_with_tenants.

    Path segment accepts UUID or slug-form name.

    Honours the ``If-Match`` request header (advisory): if present and
    stale, returns 412 Precondition Failed; if absent, logs a warning
    and accepts the write.

    Requires producer or admin role. Ownership is enforced by VisibilityService
    (only the owning tenant may change visibility).

    Errors:
    - 403 if caller lacks producer/admin role.
    - 403 if caller is not the owning tenant (PermissionError from service).
    - 404 if entity not found for the calling tenant.
    - 412 if `If-Match` was supplied and does not match the current ETag.
    - 422 if visibility value is invalid or tenant-shared without shared_with_tenants.
    """
    visibility_svc = _visibility_service(request)
    catalog_svc = get_service(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, entity_id)
        # Compute the pre-write ETag and check If-Match before the
        # service runs — fail fast on stale precondition.
        pre_record = await catalog_svc.get_full_capability(ctx, resolved.entity_id)
        pre_latest = latest_timestamp(
            pre_record.entity.created_at,
            *(f.t_ingested_at for f in pre_record.facts),
        )
        pre_etag = compute_etag(pre_record.entity.entity_id, pre_latest)
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="capability",
        )

        await visibility_svc.set_visibility(
            ctx,
            resolved.entity_id,
            body.visibility,
            shared_with_tenants=body.shared_with_tenants,
        )
        record = await catalog_svc.get_full_capability(ctx, resolved.entity_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return to_response(record)


async def patch_capability(
    entity_id: str,
    body: UpdateEntityRequest,
    request: Request,
    ctx: TenantContext = Depends(_producer_or_admin),
) -> CapabilityResponse:
    """Update mutable attributes on a capability.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a warning and accepts the
    write.  ETag is computed from the entity row before the write so a stale
    precondition fails fast without executing the mutation.

    When there is no detail GET that a client can use to acquire the ETag,
    the client may compute it from the list response or from a prior PATCH
    response body — but that is uncommon.  The recommended flow is:
    GET /v1/capabilities/{id} → ETag header → PATCH with If-Match.
    """
    service = get_service(request)
    try:
        resolved = await service.resolve_entity_handle(ctx, entity_id)
        # Compute the pre-write ETag so a stale If-Match fails before
        # the mutation runs.
        pre_record = await service.get_full_capability(ctx, resolved.entity_id)
        pre_latest = latest_timestamp(
            pre_record.entity.created_at,
            *(f.t_ingested_at for f in pre_record.facts),
        )
        pre_etag = compute_etag(pre_record.entity.entity_id, pre_latest)
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="capability",
        )
        await service.update_entity(ctx, resolved.entity_id, body.updates, valid_from=body.valid_from)
        record = await service.get_full_capability(ctx, resolved.entity_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return to_response(record)


async def delete_capability(
    entity_id: str,
    request: Request,
    ctx: TenantContext = Depends(_producer_or_admin),
) -> Response:
    """Soft-delete idempotency:
    - Row exists (active or already-invalidated) → 204 No Content.
    - Row never existed → 404 Not Found (service raises NotFoundError).
    """
    service = get_service(request)
    try:
        resolved = await service.resolve_entity_handle(ctx, entity_id)
        await service.delete_entity(ctx, resolved.entity_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Mutation router — included separately in main.py
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/capabilities", tags=["capabilities"])
_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mr.add_mutation_route(
    path="/{entity_id}",
    action="update",
    handler=patch_capability,
    verb="PATCH",
    response_model=CapabilityResponse,
)

_mr.add_mutation_route(
    path="/{entity_id}",
    action="delete",
    handler=delete_capability,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)

# Visibility mutation — PATCH /v1/capabilities/{entity_id}/visibility
# (and POST-tunneled alias when mode includes post_only/both).
_mr.add_mutation_route(
    path="/{entity_id}/visibility",
    action="set-visibility",
    handler=set_visibility_handler,
    verb="PATCH",
    response_model=CapabilityResponse,
)

mutation_router = _mutation_base
