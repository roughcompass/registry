"""GET /v1/search, GET /v1/capabilities (list), GET /v1/capabilities/{entity_id},
GET /v1/capabilities/{entity_id}/dependencies — consumer read surface.

Routers are thin adapters over RetrievalService; all business logic (fusion,
temporal filtering, tenant assertion) lives in the service layer. These handlers
only translate HTTP ↔ service types.

RetrievalService is pulled from app.state.retrieval (wired in main.py).
`as_of` query params are normalised to UTC-aware datetimes via normalize_utc();
a ValueError (naive datetime string) maps to HTTP 422.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from registry.api.cursor import InvalidCursorError, decode_cursor, encode_cursor
from registry.api.errors import build_error, map_catalog_error
from registry.api.middleware.tenant import get_tenant_context
from registry.api.schemas import (
    ArtifactResponse,
    CapabilityListResponse,
    DependencyResponse,
    EdgeRefItem,
    EntityRefItem,
    SearchResponse,
    SearchResultItem,
)
from registry.exceptions import CatalogError
from registry.service.catalog import CatalogService
from registry.service.retrieval import RetrievalService
from registry.service.temporal import normalize_utc
from registry.types import (
    EdgeRef,
    EntityRef,
    FactRef,
    SearchResult,
    TemporalFilter,
    TenantContext,
)

router = APIRouter(tags=["retrieval"])


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _retrieval(request: Request) -> RetrievalService:
    service: RetrievalService = request.app.state.retrieval
    return service


def _catalog(request: Request) -> CatalogService:
    service: CatalogService = request.app.state.catalog
    return service


def _parse_as_of(as_of: str | None) -> TemporalFilter:
    """Parse an optional ISO-8601 as_of string into a TemporalFilter.

    Raises HTTP 422 on naive (timezone-unaware) datetimes.
    """
    if as_of is None:
        return TemporalFilter(as_of=None)
    from datetime import datetime  # noqa: PLC0415

    try:
        dt = datetime.fromisoformat(as_of)
        normalised = normalize_utc(dt)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"as_of must be a timezone-aware ISO-8601 datetime: {exc}",
        ) from exc
    return TemporalFilter(as_of=normalised)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _fact_ref_to_artifact(fact: FactRef) -> ArtifactResponse:
    return ArtifactResponse(
        fact_id=fact.fact_id,
        tenant_id=fact.tenant_id,
        entity_id=fact.entity_id,
        category=fact.category,
        body=fact.body,
        is_authoritative=fact.is_authoritative,
        t_valid_from=fact.t_valid_from,
        t_valid_to=fact.t_valid_to,
        t_ingested_at=fact.t_ingested_at,
        t_invalidated_at=fact.t_invalidated_at,
    )


def _edge_ref_to_item(edge: EdgeRef) -> EdgeRefItem:
    return EdgeRefItem(
        edge_id=edge.edge_id,
        tenant_id=edge.tenant_id,
        src_entity_id=edge.src_entity_id,
        rel=edge.rel,
        dst_entity_id=edge.dst_entity_id,
        properties=edge.properties,
        t_valid_from=edge.t_valid_from,
        t_valid_to=edge.t_valid_to,
        t_ingested_at=edge.t_ingested_at,
        t_invalidated_at=edge.t_invalidated_at,
    )


def _entity_ref_to_item(entity: EntityRef) -> EntityRefItem:
    return EntityRefItem(
        entity_id=entity.entity_id,
        tenant_id=entity.tenant_id,
        entity_type=entity.entity_type,
        name=entity.name,
        external_id=entity.external_id,
        is_active=entity.is_active,
        created_at=entity.created_at,
    )


def _search_result_to_item(result: SearchResult) -> SearchResultItem:
    return SearchResultItem(
        entity_id=result.entity.entity_id,
        tenant_id=result.entity.tenant_id,
        name=result.entity.name,
        entity_type=result.entity.entity_type,
        score=result.score,
        retrieval_arms=result.retrieval_arms,
        matching_facts=[_fact_ref_to_artifact(f) for f in result.matching_facts],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/v1/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, description="Free-text search query")],
    top_k: Annotated[int, Query(ge=1, le=100)] = 10,
    as_of: Annotated[str | None, Query(description="ISO-8601 UTC datetime for time-travel")] = None,
    entity_type: Annotated[str | None, Query()] = None,
    lifecycle: Annotated[str | None, Query()] = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> SearchResponse:
    """Hybrid search across capabilities, concepts, operations, and artifact bodies."""
    service = _retrieval(request)
    temporal_filter = _parse_as_of(as_of)

    t_start = time.monotonic()
    try:
        results = await service.search(
            ctx,
            q=q,
            top_k=top_k,
            temporal_filter=temporal_filter,
            entity_type=entity_type,
            lifecycle=lifecycle,
        )
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    took_ms = (time.monotonic() - t_start) * 1000.0

    items = [_search_result_to_item(r) for r in results]
    return SearchResponse(items=items, total=len(items), took_ms=took_ms)


@router.get("/v1/capabilities", response_model=CapabilityListResponse)
async def list_capabilities(
    request: Request,
    lifecycle: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor returned by the previous page. Omit to start from the first page."),
    ] = None,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    page: Annotated[
        int | None,
        Query(
            description="Deprecated offset page number. Not accepted — use cursor= instead.",
            include_in_schema=False,
        ),
    ] = None,
    as_of: Annotated[str | None, Query(description="ISO-8601 UTC datetime for time-travel")] = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> CapabilityListResponse:
    """Paginated list of capabilities visible to the caller's tenant.

    Pagination is keyset-based: the response carries ``next_cursor`` (or null
    when no further pages exist). Pass ``cursor=<value>`` on the next request
    to retrieve the following page.

    The legacy ``?page=N`` offset parameter is no longer accepted. Clients
    that send it receive a 422 with code ``page_param_deprecated``.
    """
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

    service = _retrieval(request)
    temporal_filter = _parse_as_of(as_of)

    try:
        items, next_cursor_payload = await service.list_capabilities(
            ctx,
            lifecycle=lifecycle,
            entity_type=entity_type,
            cursor=cursor_payload,
            page_size=page_size,
            temporal_filter=temporal_filter,
        )
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    next_cursor = encode_cursor(next_cursor_payload) if next_cursor_payload else None
    return CapabilityListResponse(
        items=[_entity_ref_to_item(e) for e in items],
        next_cursor=next_cursor,
    )


@router.get(
    "/v1/capabilities/{entity_id}/dependencies",
    response_model=DependencyResponse,
)
async def get_dependencies(
    entity_id: Annotated[
        str,
        Path(description="Capability UUID or slug-form name (e.g. 'salt-design-system')"),
    ],
    request: Request,
    depth: Annotated[int, Query(ge=1, le=5)] = 2,
    as_of: Annotated[str | None, Query(description="ISO-8601 UTC datetime for time-travel")] = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> DependencyResponse:
    """k-hop dependency traversal from entity_id.

    Path segment accepts UUID or slug-form name. Depth capped at 5 by
    the service layer.
    """
    service = _retrieval(request)
    catalog_svc = _catalog(request)
    temporal_filter = _parse_as_of(as_of)

    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, entity_id)
        edge_refs = await service.get_dependencies(
            ctx,
            entity_id=resolved.entity_id,
            depth=depth,
            temporal_filter=temporal_filter,
        )
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    return DependencyResponse(
        root_entity_id=resolved.entity_id,
        depth=depth,
        as_of=temporal_filter.as_of,
        edges=[_edge_ref_to_item(e) for e in edge_refs],
    )
