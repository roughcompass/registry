"""Admin lifecycle endpoint.

  PATCH /v1/capabilities/{entity_id}/lifecycle — lifecycle state transition

This endpoint lives under /v1/capabilities/{entity_id}/lifecycle but requires
elevated roles (admin or producer) and is wired here alongside the other
admin routers to keep the privileged surface grouped.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from registry.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.routers._admin_common import _admin_or_producer_required
from registry.api.routers._common import get_service
from registry.exceptions import LifecycleError, NotFoundError
from registry.service.lifecycle import LifecycleService
from registry.types import TenantContext

# Read-only router (no GET endpoints) — kept for structural consistency
# with the standard two-router pattern used across admin sub-modules.
router = APIRouter(prefix="/v1/capabilities", tags=["lifecycle"])


class LifecycleTransitionRequest(BaseModel):
    new_state: str
    # successor encodes the three-way deprecation choice: a UUID names the
    # replacement entity; the sentinel string "none" marks the entity as
    # explicitly deprecated without a successor.  The field is required so
    # the caller must make a deliberate choice — omitting it is a 422.
    successor: uuid.UUID | Literal["none"] = Field(
        ...,
        description=('UUID of the replacement entity, or the string "none" to ' "deprecate without a successor."),
    )
    valid_from: datetime.datetime | None = None


class LifecycleTransitionResponse(BaseModel):
    entity_id: uuid.UUID
    new_state: str
    # None when successor was "none"; UUID when a replacement was named.
    replaced_by: uuid.UUID | None


async def patch_capability_lifecycle(
    entity_id: Annotated[str, Path(description="Capability UUID or slug")],
    body: LifecycleTransitionRequest,
    request: Request,
    ctx: TenantContext = Depends(_admin_or_producer_required),
) -> LifecycleTransitionResponse:
    """Apply a lifecycle state transition to a capability.

    The path segment accepts a UUID or slug-form name.

    Requires ``admin`` or ``producer`` role.  When ``successor`` is a UUID the
    service creates a ``replaced_by`` edge via ``CatalogService.create_edge``
    after committing the attribute row.  When ``successor`` is ``"none"`` the
    entity is deprecated without a replacement.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a debug warning and
    accepts the write.  ETag is computed from the entity's current state before
    the transition so a stale precondition fails fast.

    Returns 422 on policy violation (invalid transition, invalid successor value),
    404 if the entity does not exist.
    """
    catalog_svc = get_service(request)
    lifecycle_svc = LifecycleService(
        session_factory=request.app.state.session_factory,
        clock=request.app.state.clock,
        catalog=catalog_svc,
    )

    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, entity_id)
        pre_record = await catalog_svc.get_full_capability(ctx, resolved.entity_id)
        pre_latest = latest_timestamp(
            pre_record.entity.created_at,
            *(f.t_ingested_at for f in pre_record.facts),
        )
        pre_etag = compute_etag(pre_record.entity.entity_id, pre_latest)
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="capability_lifecycle",
        )
        await lifecycle_svc.transition(
            ctx,
            resolved.entity_id,
            new_state=body.new_state,
            successor=body.successor,
            valid_from=body.valid_from,
        )
    except LifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    replaced_by = body.successor if isinstance(body.successor, uuid.UUID) else None
    return LifecycleTransitionResponse(
        entity_id=resolved.entity_id,
        new_state=body.new_state,
        replaced_by=replaced_by,
    )


# ---------------------------------------------------------------------------
# Mutation router (PATCH via HttpMethodRouter)
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/capabilities", tags=["lifecycle"])
_mode, _sep = get_mode_settings()
_mutation_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mutation_mr.add_mutation_route(
    path="/{entity_id}/lifecycle",
    action="update",
    handler=patch_capability_lifecycle,
    verb="PATCH",
    response_model=LifecycleTransitionResponse,
    status_code=status.HTTP_200_OK,
)

mutation_router = _mutation_base

# Backward-compatibility alias: the original admin.py exposed both
# `lifecycle_mutation_router` and `lifecycle_router` as names. Both point
# to the same router so importing code from either name still works.
lifecycle_router = mutation_router
