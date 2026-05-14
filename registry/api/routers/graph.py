"""Graph router — graph-primitive endpoints.

Admin edge-property-schema stubs:
  POST   /v1/admin/edge-property-schemas        — register schema (admin, 201)
  GET    /v1/admin/edge-property-schemas        — list schemas (admin)
  PATCH  /v1/admin/edge-property-schemas/{id}   — supersede schema (admin, HttpMethodRouter)

Reverse traversal:
  GET    /v1/capabilities/{entity_id}/dependents — reverse CTE traversal

Blast-radius:
  GET    /v1/capabilities/{entity_id}/blast-radius  — cache-first transitive closure
  POST   /v1/capabilities/{entity_id}:blast-radius  — POST-tunneled alias (same handler)

Edge-property-schema PATCH is registered via HttpMethodRouter.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from registry.api.auth.context import ROLE_ADMIN, require_roles
from registry.api.cursor import InvalidCursorError, decode_cursor, encode_cursor
from registry.api.errors import build_error, map_catalog_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers._common import edge_to_item, get_service
from registry.api.schemas import (
    EntityRefItem,
    ProjectionResponse,
    TraversalResultResponse,
)
from registry.exceptions import CatalogError, NotFoundError
from registry.service.projections import ProjectionService
from registry.service.retrieval import RetrievalService
from registry.service.temporal import normalize_utc
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: edge-schemas"])

# Separate router for capability sub-routes that live outside /v1/admin.
capability_graph_router = APIRouter(tags=["graph"])

_admin_required = require_roles([ROLE_ADMIN])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _retrieval(request: Request) -> RetrievalService:
    service: RetrievalService = request.app.state.retrieval
    return service


def _projections(request: Request) -> ProjectionService:
    service: ProjectionService = request.app.state.projections
    return service


def _parse_as_of_dt(as_of: str | None) -> datetime.datetime | None:
    """Parse an optional ISO-8601 as_of string.  Raises HTTP 422 on naive datetimes."""
    if as_of is None:
        return None
    try:
        dt = datetime.datetime.fromisoformat(as_of)
        return normalize_utc(dt)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"as_of must be a timezone-aware ISO-8601 datetime: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Edge-property schema stubs — full implementation pending
# ---------------------------------------------------------------------------


@router.post(
    "/edge-property-schemas",
    status_code=status.HTTP_201_CREATED,
)
async def create_edge_property_schema(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> dict[str, Any]:
    """Register a JSON Schema for an edge_rel.  Edge-property schema management is not yet implemented."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="edge-property schema management is not yet implemented",
    )


@router.get(
    "/edge-property-schemas",
    status_code=status.HTTP_200_OK,
)
async def list_edge_property_schemas(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[Any]:
    """List all active edge property schemas for the tenant.  Edge-property schema management is not yet implemented."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="edge-property schema management is not yet implemented",
    )


# ---------------------------------------------------------------------------
# Edge-property schema PATCH — mutation via HttpMethodRouter
# ---------------------------------------------------------------------------


async def _update_edge_property_schema(
    schema_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> dict[str, Any]:
    """Supersede an existing edge property schema (bi-temporal).

    Full implementation is pending. Once wired, this endpoint will honour
    ``If-Match`` (advisory) using the schema's ``t_ingested_at`` timestamp
    as the ETag source, matching the pattern used by capability-type PATCH.
    """
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


_graph_admin_mutation_base = APIRouter(prefix="/v1/admin", tags=["admin: edge-schemas"])
_mode, _sep = get_mode_settings()
_graph_admin_mr = HttpMethodRouter(_graph_admin_mutation_base, mode=_mode, separator=_sep)

_graph_admin_mr.add_mutation_route(
    path="/edge-property-schemas/{schema_id}",
    action="update",
    handler=_update_edge_property_schema,
    verb="PATCH",
    status_code=status.HTTP_200_OK,
)

graph_admin_mutation_router = _graph_admin_mutation_base


# ---------------------------------------------------------------------------
# Reverse traversal endpoint
# ---------------------------------------------------------------------------


@capability_graph_router.get(
    "/v1/capabilities/{entity_id}/dependents",
    response_model=TraversalResultResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="Reverse traversal — who depends on this capability?",
)
async def get_dependents(
    entity_id: Annotated[str, Path(description="Capability UUID or slug")],
    request: Request,
    depth: Annotated[int, Query(ge=1, le=5, description="Max hop count (1–5; capped at 5)")] = 2,
    edge_types: Annotated[
        str | None,
        Query(description="Comma-separated edge_rel vocab values; default: all dependency rels"),
    ] = None,
    as_of: Annotated[
        str | None,
        Query(description="ISO-8601 UTC datetime for time-travel queries"),
    ] = None,
    as_of_version: Annotated[
        str | None,
        Query(
            description=(
                "Semver string. When set, traversal only follows edges whose "
                "version predicates are satisfied by this version. "
                "Edges with no predicate are always included."
            )
        ),
    ] = None,
    view: Annotated[
        str,
        Query(
            description=(
                "Response shape. ``default`` is the standard UI-flavoured shape. "
                "``audit`` adds bitemporal edge columns (valid_from / valid_to / "
                "ingested_at / invalidated_at / tenant_id) for audit consumers."
            )
        ),
    ] = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> TraversalResultResponse:
    """Return all capabilities that (transitively) depend on ``entity_id``.

    The path segment accepts a UUID or slug-form name.

    Visibility: only nodes belonging to the caller's tenant are returned
    (same-tenant only; cross-tenant visibility requires an adoption relationship).

    ``cache_hit`` is always ``False`` when the closure cache is not yet populated.
    ``version_satisfied[edge_id]`` reflects predicate evaluation against the
    target entity's current version attribute.

    Pass ``?view=audit`` to include bitemporal columns on edge items.
    """
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    audit = view == "audit"
    catalog_svc = get_service(request)
    service = _retrieval(request)
    as_of_dt = _parse_as_of_dt(as_of)

    # Parse edge_types from comma-separated query param.
    resolved_edge_types: list[str] | None = None
    if edge_types is not None:
        resolved_edge_types = [t.strip() for t in edge_types.split(",") if t.strip()]

    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, entity_id)
        result = await service.get_reverse_traversal(
            ctx=ctx,
            entity_id=resolved.entity_id,
            depth=depth,
            edge_types=resolved_edge_types,
            as_of=as_of_dt,
            as_of_version=as_of_version,
        )
    except (NotFoundError, CatalogError) as exc:
        raise map_catalog_error(exc) from exc

    return TraversalResultResponse(
        root_entity_id=result.root_entity_id,
        depth=result.depth,
        direction=result.direction,
        as_of=result.as_of,
        nodes=[
            EntityRefItem(
                entity_id=node.entity_id,
                tenant_id=node.tenant_id,
                entity_type=node.entity_type,
                name=node.name,
                external_id=node.external_id,
                is_active=node.is_active,
                created_at=node.created_at,
            )
            for node in result.nodes
        ],
        edges=[edge_to_item(e, audit=audit) for e in result.edges],
        version_satisfied={str(k): v for k, v in result.version_satisfied.items()},
        cache_hit=result.cache_hit,
    )


# ---------------------------------------------------------------------------
# Blast-radius endpoint
# ---------------------------------------------------------------------------


async def _blast_radius_handler(
    entity_id: uuid.UUID,
    request: Request,
    direction: str,
    depth: int,
    edge_types: str | None,
    as_of: str | None,
    as_of_version: str | None,
    ctx: TenantContext,
    *,
    audit: bool = False,
) -> TraversalResultResponse:
    """Shared handler for GET and POST-tunneled blast-radius.

    Primary path: closure_cache lookup.  Cold/expired cache → CTE fallback.
    ``cache_hit=True`` when served from cache; ``False`` on CTE path.
    ``as_of_version`` filters edges by version predicate satisfaction.
    ``audit=True`` populates bitemporal columns on edge items.

    Callers must pass a resolved UUID; slug resolution happens at each
    public handler entry point before this internal helper is invoked.
    """
    service = _retrieval(request)
    as_of_dt = _parse_as_of_dt(as_of)

    resolved_edge_types: list[str] | None = None
    if edge_types is not None:
        resolved_edge_types = [t.strip() for t in edge_types.split(",") if t.strip()]

    if direction not in ("forward", "reverse"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"direction must be 'forward' or 'reverse', got {direction!r}",
        )

    try:
        result = await service.get_blast_radius(
            ctx=ctx,
            entity_id=entity_id,
            direction=direction,
            depth=depth,
            edge_types=resolved_edge_types,
            as_of=as_of_dt,
            as_of_version=as_of_version,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    return TraversalResultResponse(
        root_entity_id=result.root_entity_id,
        depth=result.depth,
        direction=result.direction,
        as_of=result.as_of,
        nodes=[
            EntityRefItem(
                entity_id=node.entity_id,
                tenant_id=node.tenant_id,
                entity_type=node.entity_type,
                name=node.name,
                external_id=node.external_id,
                is_active=node.is_active,
                created_at=node.created_at,
            )
            for node in result.nodes
        ],
        edges=[edge_to_item(e, audit=audit) for e in result.edges],
        version_satisfied={str(k): v for k, v in result.version_satisfied.items()},
        cache_hit=result.cache_hit,
    )


@capability_graph_router.get(
    "/v1/capabilities/{entity_id}/blast-radius",
    response_model=TraversalResultResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="Blast-radius — transitive closure (cache-first)",
)
async def get_blast_radius(
    entity_id: Annotated[str, Path(description="Capability UUID or slug")],
    request: Request,
    direction: Annotated[
        str,
        Query(description="Traversal direction: 'forward' (dependencies) or 'reverse' (dependents)"),
    ] = "reverse",
    depth: Annotated[int, Query(ge=1, le=5, description="Max hop count (1–5; capped at 5)")] = 5,
    edge_types: Annotated[
        str | None,
        Query(description="Comma-separated edge_rel vocab values; default: all dependency rels"),
    ] = None,
    as_of: Annotated[
        str | None,
        Query(description="ISO-8601 UTC datetime; values > 90 days ago bypass cache"),
    ] = None,
    as_of_version: Annotated[
        str | None,
        Query(
            description=(
                "Semver string. When set, traversal only follows edges whose "
                "version predicates are satisfied by this version. "
                "Edges with no predicate are always included."
            )
        ),
    ] = None,
    view: Annotated[
        str,
        Query(
            description=(
                "Response shape. ``default`` is the standard UI-flavoured shape. "
                "``audit`` adds bitemporal edge columns (valid_from / valid_to / "
                "ingested_at / invalidated_at / tenant_id) for audit consumers."
            )
        ),
    ] = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> TraversalResultResponse:
    """Full transitive closure from a capability, served from ``closure_cache``.

    The path segment accepts a UUID or slug-form name.

    Falls back to the recursive CTE when:
    - ``as_of`` is before the 90-day cache horizon, OR
    - the cache has no rows for this root + direction (cold start).

    ``cache_hit=True`` indicates the result was served from the materialized
    cache; ``False`` indicates the live CTE was executed.

    A POST-tunneled alias ``POST /v1/capabilities/{entity_id}:blast-radius``
    accepts the same parameters via query string and returns an identical body.

    Pass ``?view=audit`` to include bitemporal columns on edge items.
    """
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    try:
        resolved = await get_service(request).resolve_entity_handle(ctx, entity_id)
    except (NotFoundError, CatalogError) as exc:
        raise map_catalog_error(exc) from exc
    return await _blast_radius_handler(
        entity_id=resolved.entity_id,
        request=request,
        direction=direction,
        depth=depth,
        edge_types=edge_types,
        as_of=as_of,
        as_of_version=as_of_version,
        ctx=ctx,
        audit=view == "audit",
    )


@capability_graph_router.post(
    "/v1/capabilities/{entity_id}:blast-radius",
    response_model=TraversalResultResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="Blast-radius — POST-tunneled alias (same handler as GET)",
)
async def post_blast_radius(
    entity_id: Annotated[str, Path(description="Capability UUID or slug")],
    request: Request,
    direction: Annotated[
        str,
        Query(description="Traversal direction: 'forward' or 'reverse'"),
    ] = "reverse",
    depth: Annotated[int, Query(ge=1, le=5, description="Max hop count (1–5; capped at 5)")] = 5,
    edge_types: Annotated[
        str | None,
        Query(description="Comma-separated edge_rel vocab values"),
    ] = None,
    as_of: Annotated[
        str | None,
        Query(description="ISO-8601 UTC datetime"),
    ] = None,
    as_of_version: Annotated[
        str | None,
        Query(description="Semver string for version predicate filtering"),
    ] = None,
    view: Annotated[
        str,
        Query(
            description=(
                "Response shape. ``default`` is the standard UI-flavoured shape. "
                "``audit`` adds bitemporal edge columns (valid_from / valid_to / "
                "ingested_at / invalidated_at / tenant_id) for audit consumers."
            )
        ),
    ] = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> TraversalResultResponse:
    """POST-tunneled alias for blast-radius.

    The path segment accepts a UUID or slug-form name. Accepts the same query
    parameters as the GET form.  Returns an identical response body.  Useful
    when intermediate proxies filter non-standard HTTP verbs or when clients
    can only issue POST requests.

    Pass ``?view=audit`` to include bitemporal columns on edge items.
    """
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    try:
        resolved = await get_service(request).resolve_entity_handle(ctx, entity_id)
    except (NotFoundError, CatalogError) as exc:
        raise map_catalog_error(exc) from exc
    return await _blast_radius_handler(
        entity_id=resolved.entity_id,
        request=request,
        direction=direction,
        depth=depth,
        edge_types=edge_types,
        as_of=as_of,
        as_of_version=as_of_version,
        ctx=ctx,
        audit=view == "audit",
    )


# ---------------------------------------------------------------------------
# Provider / consumer projections
# ---------------------------------------------------------------------------


projection_router = APIRouter(prefix="/v1/graph", tags=["graph"])


def _projection_to_response(proj: Any, *, audit: bool = False) -> ProjectionResponse:
    """Map ``Projection`` dataclass → ProjectionResponse pydantic model.

    Pass ``audit=True`` to populate bitemporal edge columns — used by
    ``?view=audit`` on the parent endpoint.
    """
    next_cursor_str: str | None = None
    if proj.next_cursor:
        next_cursor_str = encode_cursor(proj.next_cursor)

    return ProjectionResponse(
        nodes=[
            EntityRefItem(
                entity_id=n.entity_id,
                tenant_id=n.tenant_id,
                entity_type=n.entity_type,
                name=n.name,
                external_id=n.external_id,
                is_active=n.is_active,
                created_at=n.created_at,
            )
            for n in proj.nodes
        ],
        edges=[edge_to_item(e, audit=audit) for e in proj.edges],
        next_cursor=next_cursor_str,
    )


# Reusable view query param annotation for graph projection endpoints.
_GraphViewParam = Annotated[
    str,
    Query(
        description=(
            "Response shape. ``default`` is the standard UI-flavoured shape. "
            "``audit`` adds bitemporal edge columns (valid_from / valid_to / "
            "ingested_at / invalidated_at / tenant_id) for audit consumers."
        )
    ),
]


@projection_router.get(
    "/provider",
    response_model=ProjectionResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="Provider projection — what does my tenant ship?",
)
async def get_provider_projection(
    request: Request,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor returned by the previous page. Omit to start from the first page."),
    ] = None,
    page_size: Annotated[int, Query(ge=1, le=500, description="Items per page (max 500)")] = 20,
    page: Annotated[
        int | None,
        Query(
            description="Deprecated offset page number. Not accepted — use cursor= instead.",
            include_in_schema=False,
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        Query(description="ISO-8601 UTC datetime for time-travel queries"),
    ] = None,
    view: _GraphViewParam = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> ProjectionResponse:
    """Return entities owned by the caller's tenant plus every outgoing
    ``provides_to`` edge (the consumers that adopted my capabilities).

    Visibility is enforced at the service layer. Pagination uses keyset cursors;
    ``next_cursor`` in the response is null when no further pages exist.

    Pass ``?view=audit`` to include bitemporal columns on edge items.
    """
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    if page is not None:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="page_param_deprecated",
            message=(
                "The ?page= offset parameter is not accepted. "
                "Use cursor= pagination instead: omit cursor for the first page, "
                "then pass the next_cursor value returned in each response."
            ),
        )
    try:
        cursor_payload = decode_cursor(cursor, strict=cursor is not None)
    except InvalidCursorError as exc:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_cursor",
            message="The cursor value is invalid or has been tampered with.",
        ) from exc
    as_of_dt = _parse_as_of_dt(as_of)
    svc = _projections(request)
    try:
        proj = await svc.get_provider_projection(ctx=ctx, as_of=as_of_dt, cursor=cursor_payload, page_size=page_size)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return _projection_to_response(proj, audit=view == "audit")


@projection_router.get(
    "/consumer",
    response_model=ProjectionResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="Consumer projection — what does my tenant consume?",
)
async def get_consumer_projection(
    request: Request,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor returned by the previous page. Omit to start from the first page."),
    ] = None,
    page_size: Annotated[int, Query(ge=1, le=500, description="Items per page (max 500)")] = 20,
    page: Annotated[
        int | None,
        Query(
            description="Deprecated offset page number. Not accepted — use cursor= instead.",
            include_in_schema=False,
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        Query(description="ISO-8601 UTC datetime for time-travel queries"),
    ] = None,
    view: _GraphViewParam = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> ProjectionResponse:
    """Return own entities + adopted provider capabilities (visibility-filtered).

    Edges: own outgoing ``depends_on``/``requires``/``integrates_with`` +
    ``provides_to`` edges of adopted provider capabilities.

    Pagination uses keyset cursors; ``next_cursor`` in the response is null when
    no further pages exist.

    Pass ``?view=audit`` to include bitemporal columns on edge items.
    """
    if view not in ("default", "audit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"view must be one of 'default'/'audit'; got {view!r}",
        )
    if page is not None:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="page_param_deprecated",
            message=(
                "The ?page= offset parameter is not accepted. "
                "Use cursor= pagination instead: omit cursor for the first page, "
                "then pass the next_cursor value returned in each response."
            ),
        )
    try:
        cursor_payload = decode_cursor(cursor, strict=cursor is not None)
    except InvalidCursorError as exc:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_cursor",
            message="The cursor value is invalid or has been tampered with.",
        ) from exc
    as_of_dt = _parse_as_of_dt(as_of)
    svc = _projections(request)
    try:
        proj = await svc.get_consumer_projection(ctx=ctx, as_of=as_of_dt, cursor=cursor_payload, page_size=page_size)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return _projection_to_response(proj, audit=view == "audit")


__all__ = [
    "router",
    "capability_graph_router",
    "graph_admin_mutation_router",
    "projection_router",
]
