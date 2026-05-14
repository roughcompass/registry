"""Factory for parent-anchored entity CRUD routers (concepts, operations, and future types).

Concepts and operations share an identical four-handler shape (POST, GET,
PATCH, DELETE).  The only variation between them is:

- ``entity_type``     — the string stored in the entity row ("concept" / "operation").
- ``parent_edge_rel`` — the edge relation created when ``parent_capability_id``
                        is supplied ("concept_of" / "operation_of").
- ``prefix``          — the URL prefix ("/v1/concepts" / "/v1/operations").
- ``tag``             — the OpenAPI tag string ("concepts" / "operations").
- ``create_request_model`` — the Pydantic body model for POST; kept distinct
                             per entity type so OpenAPI documents the correct
                             ``entity_type`` discriminator literal.

The ``CreateConceptRequest`` and ``CreateOperationRequest`` models carry the
same fields: ``name``, ``external_id``, ``parent_capability_id``,
``attributes``, ``valid_from``.  The factory reads those common fields by
name; if a new request model diverges (adds a field unique to its entity
type), a subclass override of ``make_entity_router`` is the right extension
point — not a change to the shared handlers here.

Adding a new parent-anchored entity type (e.g. "system") means creating a
``CreateSystemRequest`` and calling ``make_entity_router`` with the
appropriate edge relation.  No changes to this module are required.

Note: this module intentionally omits ``from __future__ import annotations``.
The factory builds annotated types at call time (e.g.
``Annotated[str, Path(description=...)]``), which requires annotations to be
evaluated eagerly.  PEP 563 lazy-string annotations would turn those
constructions into unresolvable forward references.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel

from registry.api.auth.context import ROLE_ADMIN, ROLE_PRODUCER, require_roles
from registry.api.errors import map_catalog_error
from registry.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers._common import get_service, to_response
from registry.api.schemas import (
    CapabilityResponse,
    EntityDetailResponse,
    Links,
    UpdateEntityRequest,
)
from registry.exceptions import CatalogError, NotFoundError
from registry.types import TenantContext

# Producer or admin required to create, update, or delete entities.
# Consumers are read-only; auditors are read-only. This guard is applied
# to every state-modifying handler built by this factory.
_producer_or_admin = require_roles([ROLE_PRODUCER, ROLE_ADMIN])


def make_entity_router(
    *,
    entity_type: Literal["concept", "operation"],
    parent_edge_rel: str,
    prefix: str,
    tag: str,
    create_request_model: type[BaseModel],
    update_request_model: type[BaseModel] = UpdateEntityRequest,
) -> tuple[APIRouter, APIRouter]:
    """Build the CRUD router (POST + GET) and mutation router (PATCH + DELETE)
    for a parent-anchored entity type.

    Concepts attach to capabilities via ``concept_of``; operations via
    ``operation_of``.  Both share the same four-handler body — only the
    entity-type string and edge relation differ.

    Returns ``(router, mutation_router)`` for inclusion in the FastAPI app.
    The caller re-exports both names so the app's include-router calls stay
    unchanged.
    """
    router = APIRouter(prefix=prefix, tags=[tag])

    # Build the path-param annotation once — entity_type is a runtime value
    # so it cannot appear inside a PEP 563 lazy string annotation.
    EntityIdParam = Annotated[str, Path(description=f"{entity_type.capitalize()} UUID or slug")]

    # ------------------------------------------------------------------
    # POST — create entity + optional parent edge
    # ------------------------------------------------------------------

    @router.post("", response_model=CapabilityResponse, status_code=status.HTTP_201_CREATED)
    async def _create(
        body: create_request_model,  # type: ignore[valid-type]
        request: Request,
        idem: IdempotencyContext = Depends(get_idempotency_context),
        ctx: TenantContext = Depends(_producer_or_admin),
    ) -> CapabilityResponse:
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        hit = await idem.lookup(ctx)
        if hit is not None:
            return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

        service = get_service(request)
        try:
            # Dynamic request models supply name/external_id/attributes/valid_from
            # at runtime; the factory's parameterized create_request_model erases
            # those attributes from the static type.
            entity_ref = await service.create_entity(
                ctx,
                entity_type=entity_type,
                name=body.name,  # type: ignore[attr-defined]
                external_id=body.external_id,  # type: ignore[attr-defined]
                attributes=body.attributes,  # type: ignore[attr-defined]
                valid_from=body.valid_from,  # type: ignore[attr-defined]
            )
            parent_id = getattr(body, "parent_capability_id", None)
            if parent_id is not None:
                await service.create_edge(
                    ctx,
                    src_entity_id=entity_ref.entity_id,
                    rel=parent_edge_rel,
                    dst_entity_id=parent_id,
                    valid_from=body.valid_from,  # type: ignore[attr-defined]
                )
            record = await service.get_full_capability(ctx, entity_ref.entity_id)
        except CatalogError as exc:
            raise map_catalog_error(exc) from exc
        response = to_response(record)
        await idem.persist(ctx, 201, response.model_dump(mode="json"))
        return response

    # ------------------------------------------------------------------
    # GET /{entity_id} — fetch + emit ETag + _links.self
    # ------------------------------------------------------------------

    @router.get(
        "/{entity_id}",
        response_model=EntityDetailResponse,
        response_model_by_alias=True,
        response_model_exclude_unset=True,
    )
    async def _get(
        entity_id: EntityIdParam,
        request: Request,
        ctx: TenantContext = Depends(get_tenant_context),
    ) -> EntityDetailResponse:
        """Return a single entity record.

        Emits an ``ETag`` header computed from the entity identifier and the
        most recent transaction timestamp.  Clients can echo this value back
        as ``If-Match`` on subsequent PATCH calls for optimistic concurrency.
        """
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        service = get_service(request)
        try:
            resolved = await service.resolve_entity_handle(ctx, entity_id)
            record = await service.get_full_capability(ctx, resolved.entity_id)
        except CatalogError as exc:
            raise map_catalog_error(exc) from exc
        base = to_response(record)
        detail = EntityDetailResponse(
            entity_id=base.entity_id,
            tenant_id=base.tenant_id,
            name=base.name,
            external_id=base.external_id,
            lifecycle=base.lifecycle,
            attributes=base.attributes,
            created_at=base.created_at,
            _links=Links(self=f"{prefix}/{entity_id}"),
        )
        latest = latest_timestamp(
            record.entity.created_at,
            *(f.t_ingested_at for f in record.facts),
        )
        etag = compute_etag(record.entity.entity_id, latest)
        body = detail.model_dump(by_alias=True, exclude_unset=True, mode="json")
        return JSONResponse(content=body, headers={"ETag": etag})  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # PATCH /{entity_id} — update with If-Match check
    # ------------------------------------------------------------------

    async def _patch(
        entity_id: EntityIdParam,
        body: update_request_model,  # type: ignore[valid-type]
        request: Request,
        ctx: TenantContext = Depends(_producer_or_admin),
    ) -> CapabilityResponse:
        """Update mutable attributes on the entity.

        Honours the ``If-Match`` request header (advisory): if present and
        stale, returns 412 Precondition Failed; if absent, logs a debug
        warning and accepts the write.  ETag is computed before the write so
        a stale precondition fails fast.
        """
        service = get_service(request)
        try:
            resolved = await service.resolve_entity_handle(ctx, entity_id)
            pre_record = await service.get_full_capability(ctx, resolved.entity_id)
            pre_latest = latest_timestamp(
                pre_record.entity.created_at,
                *(f.t_ingested_at for f in pre_record.facts),
            )
            pre_etag = compute_etag(pre_record.entity.entity_id, pre_latest)
            check_if_match(
                request.headers.get("if-match"),
                pre_etag,
                resource_kind=entity_type,
            )
            # Dynamic update_request_model exposes updates/valid_from at runtime
            # but the parameterized type erases them.
            await service.update_entity(
                ctx,
                resolved.entity_id,
                body.updates,  # type: ignore[attr-defined]
                valid_from=body.valid_from,  # type: ignore[attr-defined]
            )
            record = await service.get_full_capability(ctx, resolved.entity_id)
        except CatalogError as exc:
            raise map_catalog_error(exc) from exc
        return to_response(record)

    # ------------------------------------------------------------------
    # DELETE /{entity_id} — soft-delete, idempotent
    # ------------------------------------------------------------------

    async def _delete(
        entity_id: EntityIdParam,
        request: Request,
        ctx: TenantContext = Depends(_producer_or_admin),
    ) -> Response:
        """Soft-delete idempotency: 204 on first or repeat delete; 404 on never-existing."""
        service = get_service(request)
        try:
            resolved = await service.resolve_entity_handle(ctx, entity_id)
            await service.delete_entity(ctx, resolved.entity_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except CatalogError as exc:
            raise map_catalog_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------
    # Mutation router — returned for separate include in main.py
    # ------------------------------------------------------------------

    _mutation_base = APIRouter(prefix=prefix, tags=[tag])
    _mode, _sep = get_mode_settings()
    _mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

    _mr.add_mutation_route(
        path="/{entity_id}",
        action="update",
        handler=_patch,
        verb="PATCH",
        response_model=CapabilityResponse,
    )

    _mr.add_mutation_route(
        path="/{entity_id}",
        action="delete",
        handler=_delete,
        verb="DELETE",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )

    return router, _mutation_base


__all__ = ["make_entity_router"]
