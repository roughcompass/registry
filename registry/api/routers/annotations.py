"""Annotation REST endpoints.

Surface:

  POST   /v1/capabilities/{capability_id}/annotations  → 201 + AnnotationResponse
  GET    /v1/capabilities/{capability_id}/annotations  → 200 + {items, next_cursor}
  PATCH  /v1/annotations/{annotation_id}               → 200 + AnnotationResponse
  DELETE /v1/annotations/{annotation_id}               → 204 No Content (idempotent)

PATCH and DELETE are registered via :class:`HttpMethodRouter` so the
``REGISTRY_HTTP_METHODS_MODE`` env var controls whether POST-tunneled
aliases are also exposed.

Authorization
-------------
- POST/GET: any actor whose tenant can see the capability (AnnotationService
  calls assert_visible internally on create; list applies author-path filtering
  on the result set).
- PATCH: producer or admin in the capability's owner tenant (enforced in
  AnnotationService.triage_annotation).
- DELETE: author of the annotation OR producer/admin in the capability's owner
  tenant (enforced in AnnotationService.delete_annotation).

Error mapping
-------------
- HTTPException(403) raised by service → propagated as-is.
- HTTPException(404) raised by service → propagated as-is.
- HTTPException(422) raised by service → propagated as-is (PII block, invalid
  category/status, empty body).
- Pydantic RequestValidationError → 422 via global handler in main.py.

warnings field
--------------
``AnnotationResponse.warnings`` is omitted from the serialized response when
``None`` — not rendered as ``null`` or ``[]``. Clients must treat the field's
absence as equivalent to an empty list. This preserves a clean "no warnings"
contract without a vestigial null key.

Service wiring
--------------
``get_annotation_service`` is a FastAPI dependency that builds a per-request
``AnnotationService`` from app state components (session factory, visibility,
PII scanner, audit writer, clock). Tests override this dependency directly via
``app.dependency_overrides`` to inject a mock — no live database is needed.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, Field

from registry.api.auth.context import ROLE_ADMIN, ROLE_CONSUMER, ROLE_PRODUCER, require_roles
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.service.annotations import VALID_CATEGORIES, VALID_STATUSES, AnnotationRef, AnnotationService
from registry.types import SystemClock, TenantContext

# ---------------------------------------------------------------------------
# Auth shortcuts
# ---------------------------------------------------------------------------

# Any authenticated consumer, producer, or admin can submit or list annotations.
_any_roles = [ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN]
_any_required = require_roles(_any_roles)

# Triage requires producer or admin in the capability-owner tenant.
_triage_roles = [ROLE_PRODUCER, ROLE_ADMIN]
_triage_required = require_roles(_triage_roles)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

_CATEGORY_DESCRIPTION = "Annotation category. Must be one of: " + ", ".join(sorted(VALID_CATEGORIES)) + "."
_STATUS_DESCRIPTION = "Annotation triage status. Must be one of: " + ", ".join(sorted(VALID_STATUSES)) + "."


class AnnotationCreateRequest(BaseModel):
    """Request body for POST /v1/capabilities/{capability_id}/annotations."""

    body: str = Field(..., min_length=1, description="Annotation text (min 1 character).")
    category: str = Field(..., description=_CATEGORY_DESCRIPTION)
    triage_note: str | None = Field(default=None, description="Optional provider triage note.")
    version_target: str | None = Field(default=None, description="Optional version string this annotation targets.")


class AnnotationTriageRequest(BaseModel):
    """Request body for PATCH /v1/annotations/{annotation_id}."""

    status: str = Field(..., description=_STATUS_DESCRIPTION)
    triage_note: str | None = Field(default=None, description="Optional provider triage note.")
    version_target: str | None = Field(default=None, description="Optional version string this annotation targets.")


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class WarningEntry(BaseModel):
    """A single PII warning entry returned when scan policy=warn fires."""

    field: str
    categories: list[str]


class AnnotationResponse(BaseModel):
    """Full annotation resource shape returned by POST (201) and PATCH (200).

    ``warnings`` is serialized via model_dump(exclude_none=True) at the call
    site, so when it is None the key is absent from the JSON body rather than
    rendered as null. Clients must treat key-absence as equivalent to no warnings.

    The temporal columns (t_valid_from, t_valid_to, t_ingested_at) are present
    in the schema for forward-compatibility. In the current plaintext service they
    will be None; the ENC-phase addition of those fields to AnnotationRef will
    cause them to flow through automatically.
    """

    annotation_id: uuid.UUID
    capability_id: uuid.UUID
    author_actor_id: uuid.UUID | None = None
    author_tenant_id: uuid.UUID
    body: str
    triage_note: str | None = None
    category: str
    status: str
    version_target: str | None = None
    created_at: str
    updated_at: str
    t_valid_from: str | None = None
    t_valid_to: str | None = None
    t_ingested_at: str | None = None
    warnings: list[WarningEntry] | None = None

    model_config = {"populate_by_name": True}


def _ref_to_response(ref: AnnotationRef) -> AnnotationResponse:
    """Convert an AnnotationRef returned by the service to the REST response shape.

    Temporal columns (t_valid_from, t_valid_to, t_ingested_at) are read from the
    ref only when present — the AN-phase AnnotationRef does not carry them so they
    default to None. The ENC phase can add them to AnnotationRef and they will flow
    through here automatically.

    warnings is converted from the service's list[dict] form to typed WarningEntry
    instances. When warnings is None the field is excluded by the caller using
    model_dump(exclude_none=True).
    """
    warnings: list[WarningEntry] | None = None
    if ref.warnings:
        warnings = [WarningEntry(field=w["field"], categories=w["categories"]) for w in ref.warnings]

    # Read temporal fields defensively — they are not on AnnotationRef in this phase.
    raw_t_valid_from = getattr(ref, "t_valid_from", None)
    raw_t_valid_to = getattr(ref, "t_valid_to", None)
    raw_t_ingested_at = getattr(ref, "t_ingested_at", None)

    return AnnotationResponse(
        annotation_id=ref.annotation_id,
        capability_id=ref.capability_id,
        author_actor_id=ref.author_actor_id,
        author_tenant_id=ref.author_tenant_id,
        body=ref.body,
        triage_note=ref.triage_note,
        category=ref.category,
        status=ref.status,
        version_target=ref.version_target,
        created_at=ref.created_at.isoformat(),
        updated_at=ref.updated_at.isoformat(),
        t_valid_from=raw_t_valid_from.isoformat() if raw_t_valid_from else None,
        t_valid_to=raw_t_valid_to.isoformat() if raw_t_valid_to else None,
        t_ingested_at=raw_t_ingested_at.isoformat() if raw_t_ingested_at else None,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Service dependency
# ---------------------------------------------------------------------------


class _AuditWriterAdapter:
    """Concrete implementation of the AuditWriter protocol for the annotation router.

    Wraps ``registry.api.audit.emit`` with the session_factory bound at
    construction time. Failures inside ``emit`` are swallowed and counted by the
    Prometheus counter in the audit module — they never propagate to the caller.
    """

    def __init__(self, session_factory: object, clock: SystemClock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def emit(
        self,
        ctx: TenantContext,
        *,
        action: str,
        target_type: str,
        target_id: uuid.UUID,
        after: object = None,
    ) -> None:
        from registry.api.audit import emit as _emit  # noqa: PLC0415

        await _emit(
            self._session_factory,  # type: ignore[arg-type]
            ctx,
            self._clock,
            action=action,
            target_type=target_type,
            target_id=target_id,
            after=after,  # type: ignore[arg-type]
        )


def _build_annotation_service(app) -> AnnotationService:  # type: ignore[type-arg]
    """Build the singleton AnnotationService from app state.

    Called once by the app factory at startup; the result is stored on
    app.state.annotation_service and returned by get_annotation_service
    on every request. AnnotationService now opens its own sessions per
    method call via its session_factory, so this builder no longer needs
    a per-request session.
    """
    visibility = app.state.visibility
    session_factory = app.state.session_factory
    pii_scanner = getattr(app.state, "pii_scanner", None)
    if pii_scanner is None:
        from registry.security.pii_scanner import build_builtin_scanner  # noqa: PLC0415

        pii_scanner = build_builtin_scanner()
    clock = SystemClock()
    audit_writer = _AuditWriterAdapter(session_factory=session_factory, clock=clock)
    return AnnotationService(
        session_factory=session_factory,
        visibility_svc=visibility,
        pii_scanner=pii_scanner,
        audit_writer=audit_writer,
        clock=clock,
    )


def get_annotation_service(request: Request) -> AnnotationService:
    """Return the singleton AnnotationService stored on app.state.

    The singleton is built once at app startup by _build_annotation_service.
    Tests override this dependency via ``app.dependency_overrides`` to inject a
    mock AnnotationService — no live database needed.
    """
    return request.app.state.annotation_service


# ---------------------------------------------------------------------------
# Mode settings for HttpMethodRouter
# ---------------------------------------------------------------------------

_mode, _sep = get_mode_settings()


# ---------------------------------------------------------------------------
# Capability-scoped POST + GET
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/v1/capabilities", tags=["annotations"])


@router.post(
    "/{capability_id}/annotations",
    status_code=status.HTTP_201_CREATED,
    summary="Submit an annotation on a capability",
)
async def create_annotation(
    capability_id: Annotated[uuid.UUID, Path(description="Capability UUID")],
    body: AnnotationCreateRequest,
    svc: AnnotationService = Depends(get_annotation_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Submit a new annotation on a capability.

    The caller must be able to see the capability — the service enforces this
    via assert_visible before writing the row.

    Returns 201 with the full AnnotationResponse. When the PII scanner fires a
    warn-level policy on the annotation body, the response includes a top-level
    ``warnings`` list; clients should surface this to the submitting actor.

    Pydantic rejects empty body strings and unknown category values with 422
    before the service is called. Category values outside the closed vocabulary
    are also rejected by the service with 422 as a defense-in-depth check.
    """
    ref = await svc.create_annotation(
        ctx,
        capability_id=capability_id,
        body=body.body,
        category=body.category,
        version_target=body.version_target,
    )
    response = _ref_to_response(ref)
    return response.model_dump(exclude_none=True)


@router.get(
    "/{capability_id}/annotations",
    summary="List annotations on a capability",
)
async def list_annotations(
    capability_id: Annotated[uuid.UUID, Path(description="Capability UUID")],
    svc: AnnotationService = Depends(get_annotation_service),
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter by annotation status (open/triaged/acknowledged/closed)."),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque keyset pagination cursor.")] = None,
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """List annotations on a capability.

    Provider path: caller's tenant owns the capability → returns all active
    annotations, optionally filtered by status.

    Author path: caller's tenant does not own the capability → returns only
    annotations where author_tenant_id == caller's tenant. Receiving an empty
    list is not a 403 — it means the caller has no authored annotations on this
    capability.

    Cursor-paginated on (t_ingested_at ASC, annotation_id ASC).
    """
    refs, next_cursor = await svc.list_annotations(
        ctx,
        capability_id=capability_id,
        status=status_filter,
        cursor=cursor,
    )
    items = [_ref_to_response(r).model_dump(exclude_none=True) for r in refs]
    return {"items": items, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Annotation-scoped PATCH + DELETE — via HttpMethodRouter
# ---------------------------------------------------------------------------

mutation_router = APIRouter(prefix="/v1/annotations", tags=["annotations"])
_mut_mr = HttpMethodRouter(mutation_router, mode=_mode, separator=_sep)


async def _triage_annotation_handler(
    annotation_id: uuid.UUID,
    body: AnnotationTriageRequest,
    svc: AnnotationService = Depends(get_annotation_service),
    ctx: TenantContext = Depends(_triage_required),
) -> dict[str, Any]:
    """Triage an annotation — update its status and optionally set a triage note.

    Authorization: the caller's tenant must own the capability the annotation
    belongs to. The service enforces this check before applying any update.

    Returns 200 with the updated AnnotationResponse. A warn-level PII hit on
    triage_note populates the top-level ``warnings`` list in the response.

    An invalid status value is rejected with 422 (service-layer vocabulary check).
    """
    ref = await svc.triage_annotation(
        ctx,
        annotation_id=annotation_id,
        new_status=body.status,
        triage_note=body.triage_note,
        version_target=body.version_target,
    )
    response = _ref_to_response(ref)
    return response.model_dump(exclude_none=True)


async def _delete_annotation_handler(
    annotation_id: uuid.UUID,
    svc: AnnotationService = Depends(get_annotation_service),
    ctx: TenantContext = Depends(_any_required),
) -> Response:
    """Soft-delete an annotation. Idempotent — second call returns 204 unchanged.

    Authorization: the annotation's author OR a producer/admin in the capability's
    owner tenant can delete. The service enforces this check and treats an already-
    deleted annotation as a no-op rather than a 404.
    """
    await svc.delete_annotation(ctx, annotation_id=annotation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_mut_mr.add_mutation_route(
    path="/{annotation_id}",
    action="update",
    handler=_triage_annotation_handler,
    verb="PATCH",
    summary="Triage an annotation (update status / triage note)",
)

_mut_mr.add_mutation_route(
    path="/{annotation_id}",
    action="delete",
    handler=_delete_annotation_handler,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Soft-delete an annotation (idempotent)",
)


__all__ = [
    "router",
    "mutation_router",
    "get_annotation_service",
    "_build_annotation_service",
    "AnnotationResponse",
    "WarningEntry",
]
