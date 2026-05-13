"""Workspace and workspace-entry REST endpoints.

Surface:

  POST   /v1/workspaces                                  → 201 WorkspaceResponse
  GET    /v1/workspaces                                   → 200 {items, next_cursor}
  GET    /v1/workspaces/search                            → 200 SearchResponse
  GET    /v1/workspaces/{workspace_id}                    → 200 WorkspaceResponse
  PATCH  /v1/workspaces/{workspace_id}                    → 200 WorkspaceResponse
  DELETE /v1/workspaces/{workspace_id}                    → 204 No Content (idempotent)
  POST   /v1/workspaces/{workspace_id}/entries            → 201 EntryResponse
  GET    /v1/workspaces/{workspace_id}/entries            → 200 {items, next_cursor}
  PATCH  /v1/workspaces/{workspace_id}/entries/{entry_id} → 200 EntryResponse
  DELETE /v1/workspaces/{workspace_id}/entries/{entry_id} → 204 No Content (idempotent)
  GET    /v1/workspaces/{workspace_id}/shares             → 200 {items: [ShareResponse]}
  POST   /v1/workspaces/{workspace_id}/shares             → 201 ShareResponse
  DELETE /v1/workspaces/{workspace_id}/shares/{share_id}  → 204 No Content (idempotent)

PATCH and DELETE are registered via HttpMethodRouter so the
``REGISTRY_HTTP_METHODS_MODE`` env var controls whether POST-tunneled
aliases are also exposed.

Authorization
-------------
- All endpoints: any authenticated consumer, producer, or admin can call
  workspace + entry endpoints. Ownership and visibility enforcement lives in
  WorkspaceService — the service raises 403/404 before touching any content.
- PATCH workspace / DELETE workspace: requires the caller to be the owning
  actor or an admin in the workspace's owning tenant (enforced in service).
- PATCH entry / DELETE entry: requires the caller to own the workspace or
  hold an active contributor share (enforced in service).

Error mapping
-------------
- HTTPException(403/404/422) raised by service propagates as-is.
- Pydantic RequestValidationError → 422 via global handler in main.py.

warnings field
--------------
``EntryResponse.warnings`` is omitted from the serialized response when
``None`` — not rendered as ``null`` or ``[]``. Clients must treat the field's
absence as equivalent to an empty list. The service populates warnings only
when the PII scanner resolves policy=warn on one or more entry fields.

Absent fields
-------------
``encryption_tier``, ``encryption_status``, ``body_ciphertext``, ``kek_id``,
``wrapped_dek`` are intentionally absent from all response schemas. These
fields are internal forward-compatibility columns or ENC-phase artifacts.
Exposing them before encryption ships creates a vestigial client surface that
adds noise without benefit.

Service wiring
--------------
``get_workspace_service`` is a FastAPI dependency that returns the singleton
``WorkspaceService`` stored on ``app.state.workspace_service``. The singleton
is built once at app startup by ``_build_workspace_service``. Tests override
this dependency via ``app.dependency_overrides`` to inject a mock — no live
database needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, Field

from registry.api.auth.context import ROLE_ADMIN, ROLE_CONSUMER, ROLE_PRODUCER, require_roles
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.service.workspace import SearchResult, ShareRef, WorkspaceEntryRef, WorkspaceRef, WorkspaceService
from registry.types import SystemClock, TenantContext

# ---------------------------------------------------------------------------
# Auth shortcuts
# ---------------------------------------------------------------------------

# Any authenticated actor can call workspace and entry endpoints.
_any_roles = [ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN]
_any_required = require_roles(_any_roles)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class WorkspaceCreateRequest(BaseModel):
    """Request body for POST /v1/workspaces."""

    name: str = Field(..., min_length=1, description="Workspace name (min 1 character).")
    owner_kind: str = Field(
        ...,
        description="Workspace ownership model. Must be 'actor' (personal) or 'tenant' (team).",
    )
    description: str | None = Field(default=None, description="Optional description.")


class WorkspaceUpdateRequest(BaseModel):
    """Request body for PATCH /v1/workspaces/{workspace_id}."""

    name: str | None = Field(default=None, description="New workspace name.")
    description: str | None = Field(default=None, description="New description. Pass null to clear.")
    archived_at: datetime | None = Field(
        default=None,
        description="ISO-8601 timestamp to archive. Pass null to un-archive.",
    )


class EntryCreateRequest(BaseModel):
    """Request body for POST /v1/workspaces/{workspace_id}/entries."""

    kind: str = Field(
        ...,
        description=(
            "Entry kind. Must be one of: note, decision, open_question, "
            "saved_query, saved_view, private_annotation."
        ),
    )
    body_md: str = Field(..., min_length=1, description="Entry body in Markdown (min 1 character).")
    reference_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Optional list of entity UUIDs this entry references.",
    )
    references_jsonb: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured references (arbitrary JSON object).",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Optional ISO-8601 expiry timestamp for time-limited entries.",
    )


class EntryUpdateRequest(BaseModel):
    """Request body for PATCH /v1/workspaces/{workspace_id}/entries/{entry_id}."""

    body_md: str | None = Field(default=None, description="New entry body.")
    reference_ids: list[uuid.UUID] | None = Field(
        default=None, description="New reference_ids list. Replaces the existing list."
    )
    references_jsonb: dict[str, Any] | None = Field(
        default=None, description="New structured references. Replaces the existing object."
    )


class ShareCreateRequest(BaseModel):
    """Request body for POST /v1/workspaces/{workspace_id}/shares."""

    grantee_actor_id: uuid.UUID = Field(..., description="UUID of the actor being granted access.")
    grantee_tenant_id: uuid.UUID = Field(..., description="UUID of the grantee's tenant.")
    role: str = Field(
        ...,
        description="Share role. Must be 'reader' or 'contributor'.",
    )


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class WarningEntry(BaseModel):
    """A single PII warning returned when the scanner fires policy=warn."""

    field: str
    categories: list[str]


class WorkspaceResponse(BaseModel):
    """Full workspace resource shape returned by create (201), get (200), and update (200).

    Absent fields: encryption_tier, encryption_status, kek_id, wrapped_dek.
    These are internal forward-compatibility or ENC-phase columns that are not
    surfaced to clients until encryption ships.
    """

    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None = None
    owner_kind: str
    owner_actor_id: uuid.UUID | None = None
    archived_at: str | None = None
    created_at: str
    updated_at: str
    created_by: uuid.UUID | None = None
    t_invalidated_at: str | None = None

    model_config = {"populate_by_name": True}


class EntryResponse(BaseModel):
    """Full workspace entry resource shape.

    ``warnings`` is serialized via model_dump(exclude_none=True) at the call
    site, so when it is None the key is absent from the JSON body. Clients
    must treat key-absence as equivalent to no warnings.

    Absent fields: encryption_status, body_ciphertext, body_nonce, and any
    other ENC-phase artifacts. The entry body is always plaintext in this phase.
    """

    entry_id: uuid.UUID
    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    body_md: str
    references_jsonb: dict[str, Any] | None = None
    reference_ids: list[uuid.UUID]
    expires_at: str | None = None
    created_at: str
    updated_at: str
    created_by: uuid.UUID | None = None
    warnings: list[WarningEntry] | None = None

    model_config = {"populate_by_name": True}


class ShareResponse(BaseModel):
    """Shape returned by share grant (201) and list (200) endpoints.

    revoked_at is included on the POST 201 response (active shares have None).
    The GET list endpoint returns only active shares (revoked_at IS NULL), so
    revoked_at will always be null in that context, but the field is kept for
    a consistent shape across both endpoints.

    Absent fields: encryption_tier, encryption_status, body_ciphertext, kek_id,
    wrapped_dek — same exclusions as WorkspaceResponse and EntryResponse.
    """

    share_id: uuid.UUID
    workspace_id: uuid.UUID
    grantee_actor_id: uuid.UUID
    grantee_tenant_id: uuid.UUID
    role: str
    granted_at: str
    revoked_at: str | None = None

    model_config = {"populate_by_name": True}


class SearchResponse(BaseModel):
    """Shape returned by GET /v1/workspaces/search.

    items contains EntryResponse objects for the current page.
    next_cursor is non-None when a next page exists — pass it back as cursor.
    total_count is None when the service omits it for performance; clients must
    treat it as advisory and use next_cursor as the canonical pagination signal.
    """

    items: list[dict[str, Any]]
    next_cursor: str | None = None
    total_count: int | None = None


# ---------------------------------------------------------------------------
# Response converters
# ---------------------------------------------------------------------------


def _workspace_ref_to_response(ref: WorkspaceRef) -> WorkspaceResponse:
    """Convert a WorkspaceRef returned by the service to the REST response shape."""
    return WorkspaceResponse(
        workspace_id=ref.workspace_id,
        tenant_id=ref.tenant_id,
        name=ref.name,
        description=ref.description,
        owner_kind=ref.owner_kind,
        owner_actor_id=ref.owner_actor_id,
        archived_at=ref.archived_at.isoformat() if ref.archived_at is not None else None,
        created_at=ref.created_at.isoformat(),
        updated_at=ref.updated_at.isoformat(),
        created_by=ref.created_by,
        t_invalidated_at=ref.t_invalidated_at.isoformat() if ref.t_invalidated_at is not None else None,
    )


def _entry_ref_to_response(ref: WorkspaceEntryRef) -> EntryResponse:
    """Convert a WorkspaceEntryRef returned by the service to the REST response shape.

    warnings is converted from the service's list[dict] form to typed WarningEntry
    instances. When warnings is None the field is excluded by the caller using
    model_dump(exclude_none=True).
    """
    warnings: list[WarningEntry] | None = None
    if ref.warnings:
        warnings = [
            WarningEntry(field=w["field"], categories=w["categories"])
            for w in ref.warnings
        ]
    return EntryResponse(
        entry_id=ref.entry_id,
        workspace_id=ref.workspace_id,
        tenant_id=ref.tenant_id,
        kind=ref.kind,
        body_md=ref.body_md,
        references_jsonb=ref.references_jsonb,
        reference_ids=ref.reference_ids,
        expires_at=ref.expires_at.isoformat() if ref.expires_at is not None else None,
        created_at=ref.created_at.isoformat(),
        updated_at=ref.updated_at.isoformat(),
        created_by=ref.created_by,
        warnings=warnings,
    )


def _share_ref_to_response(ref: ShareRef) -> ShareResponse:
    """Convert a ShareRef returned by the service to the REST ShareResponse shape."""
    return ShareResponse(
        share_id=ref.share_id,
        workspace_id=ref.workspace_id,
        grantee_actor_id=ref.grantee_actor_id,
        grantee_tenant_id=ref.grantee_tenant_id,
        role=ref.role,
        granted_at=ref.granted_at.isoformat(),
        revoked_at=ref.revoked_at.isoformat() if ref.revoked_at is not None else None,
    )


def _search_result_to_response(result: SearchResult) -> dict[str, Any]:
    """Convert a SearchResult returned by the service to the REST SearchResponse shape."""
    items = [_entry_ref_to_response(e).model_dump(exclude_none=True) for e in result.items]
    return {
        "items": items,
        "next_cursor": result.next_cursor,
        "total_count": result.total_count,
    }


# ---------------------------------------------------------------------------
# Service factory + dependency
# ---------------------------------------------------------------------------


class _AuditWriterAdapter:
    """Concrete implementation of the AuditWriter protocol for the workspace router.

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


def _build_workspace_service(app: object) -> WorkspaceService:
    """Build the singleton WorkspaceService from app state.

    Called once by the app factory at startup; the result is stored on
    app.state.workspace_service and returned by get_workspace_service on every
    request. WorkspaceService opens its own sessions per method call via its
    session_factory, so this builder does not need a per-request session.

    Wiring the singleton here (rather than constructing per-request in
    admin_workspaces.py) means T17b share/search/admin endpoints can import
    get_workspace_service and reuse the same instance without re-building the
    dependency graph on every call.
    """
    state = app.state  # type: ignore[union-attr]
    visibility = state.visibility
    session_factory = state.session_factory
    pii_scanner = getattr(state, "pii_scanner", None)
    if pii_scanner is None:
        from registry.security.pii_scanner import build_builtin_scanner  # noqa: PLC0415

        pii_scanner = build_builtin_scanner()
    clock = SystemClock()
    audit_writer = _AuditWriterAdapter(session_factory=session_factory, clock=clock)
    return WorkspaceService(
        session_factory=session_factory,
        visibility_svc=visibility,
        pii_scanner=pii_scanner,
        audit_writer=audit_writer,
        clock=clock,
    )


def get_workspace_service(request: Request) -> WorkspaceService:
    """Return the singleton WorkspaceService stored on app.state.

    The singleton is built once at app startup by _build_workspace_service.
    Tests override this dependency via ``app.dependency_overrides`` to inject a
    mock WorkspaceService — no live database needed.
    """
    return request.app.state.workspace_service


# ---------------------------------------------------------------------------
# Mode settings for HttpMethodRouter
# ---------------------------------------------------------------------------

_mode, _sep = get_mode_settings()


# ---------------------------------------------------------------------------
# Workspace collection endpoints — POST + GET
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a workspace",
)
async def create_workspace(
    body: WorkspaceCreateRequest,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Create a new workspace.

    owner_kind='actor' creates a personal workspace tied to the calling actor.
    owner_kind='tenant' creates a team workspace owned by the calling actor's tenant.

    Regulated tenants (is_regulated=true) cannot create workspaces at encryption
    tier 'none' — the service returns 422 with an actionable message explaining
    that a higher encryption tier is required. This is a program constraint
    (ENC phase dependency), not a bug.

    Invalid owner_kind values are rejected with 422 by the service.
    """
    ref = await svc.create_workspace(
        ctx,
        name=body.name,
        owner_kind=body.owner_kind,
        description=body.description,
    )
    response = _workspace_ref_to_response(ref)
    return response.model_dump(exclude_none=True)


@router.get(
    "/search",
    summary="Search workspace entries visible to the caller",
)
async def search_workspaces(
    svc: WorkspaceService = Depends(get_workspace_service),
    q: Annotated[
        str | None,
        Query(description="Full-text search string (optional)."),
    ] = None,
    kind: Annotated[
        str | None,
        Query(description="Filter by entry kind (optional)."),
    ] = None,
    owner_actor_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter by workspace owner actor UUID (optional; self or admin only)."),
    ] = None,
    reference_ids: Annotated[
        str | None,
        Query(description="Comma-separated list of reference UUIDs. Entry must contain all of them."),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(description="Opaque keyset pagination cursor."),
    ] = None,
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Search workspace entries visible to the calling actor.

    Returns entries from workspaces the caller owns or holds an active share on.
    No entry from a workspace the caller cannot access is ever included.

    Filters are AND-combined:
    - q: full-text search on body_md using the GIN index.
    - kind: exact match on entry kind.
    - owner_actor_id: restrict to workspaces owned by this actor (caller or admin only).
    - reference_ids: comma-separated UUIDs; entry must contain ALL of them.

    Cursor-paginated on entry_id ascending. total_count is null when the service
    omits it for performance — use next_cursor as the canonical pagination signal.
    """
    parsed_reference_ids: list[uuid.UUID] | None = None
    if reference_ids is not None:
        parsed_reference_ids = [uuid.UUID(rid.strip()) for rid in reference_ids.split(",") if rid.strip()]

    result = await svc.search_workspaces(
        ctx,
        q=q,
        kind=kind,
        owner_actor_id=owner_actor_id,
        reference_ids=parsed_reference_ids,
        cursor=cursor,
    )
    return _search_result_to_response(result)


@router.get(
    "",
    summary="List workspaces visible to the caller",
)
async def list_workspaces(
    svc: WorkspaceService = Depends(get_workspace_service),
    include_archived: Annotated[
        bool,
        Query(description="Include archived workspaces (default false)."),
    ] = False,
    cursor: Annotated[
        str | None,
        Query(description="Opaque keyset pagination cursor."),
    ] = None,
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """List workspaces the calling actor can see.

    Returns workspaces where the caller is the owning actor, any member of the
    owning tenant, or holds an active workspace share. Excludes soft-deleted rows.
    Excludes archived rows unless include_archived=true.

    Cursor-paginated on workspace_id ascending.
    """
    refs, next_cursor = await svc.list_workspaces(
        ctx,
        include_archived=include_archived,
        cursor=cursor,
    )
    items = [_workspace_ref_to_response(r).model_dump(exclude_none=True) for r in refs]
    return {"items": items, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Workspace entry collection endpoints — POST + GET
# Entry endpoints nest under the same router (prefix /v1/workspaces).
# ---------------------------------------------------------------------------


@router.post(
    "/{workspace_id}/entries",
    status_code=status.HTTP_201_CREATED,
    summary="Create an entry in a workspace",
)
async def create_entry(
    workspace_id: Annotated[uuid.UUID, Path(description="Workspace UUID")],
    body: EntryCreateRequest,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Create a new entry in a workspace.

    The caller must own the workspace or hold an active contributor share.
    The service enforces access via get_workspace before writing.

    PII scanner runs on body_md and references_jsonb. A block-level hit raises
    422 and the entry is NOT stored. A warn-level hit stores the entry and
    returns a top-level ``warnings`` list in the response.

    Regulated tenants cannot create entries (defense-in-depth against any path
    that bypasses the workspace-create guard).
    """
    ref = await svc.create_entry(
        ctx,
        workspace_id=workspace_id,
        kind=body.kind,
        body_md=body.body_md,
        reference_ids=body.reference_ids,
        references_jsonb=body.references_jsonb,
        expires_at=body.expires_at,
    )
    response = _entry_ref_to_response(ref)
    return response.model_dump(exclude_none=True)


@router.get(
    "/{workspace_id}/entries",
    summary="List entries in a workspace",
)
async def list_entries(
    workspace_id: Annotated[uuid.UUID, Path(description="Workspace UUID")],
    svc: WorkspaceService = Depends(get_workspace_service),
    kind: Annotated[
        str | None,
        Query(description="Filter by entry kind (optional)."),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(description="Opaque keyset pagination cursor."),
    ] = None,
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """List active entries in a workspace.

    Access is gated by workspace visibility — the caller must own the workspace
    or hold an active share. The service raises 403/404 before returning entries.

    Excludes soft-deleted entries. Entries past their expires_at are still
    returned; the expiry worker invalidates them in a background run.

    Cursor-paginated on entry_id ascending.
    """
    refs, next_cursor = await svc.list_entries(
        ctx,
        workspace_id=workspace_id,
        kind=kind,
        cursor=cursor,
    )
    items = [_entry_ref_to_response(r).model_dump(exclude_none=True) for r in refs]
    return {"items": items, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Workspace mutation endpoints — PATCH + DELETE via HttpMethodRouter
# ---------------------------------------------------------------------------

mutation_router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])
_mut_mr = HttpMethodRouter(mutation_router, mode=_mode, separator=_sep)


async def _get_workspace_handler(
    workspace_id: uuid.UUID,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Return a single workspace by ID.

    The service enforces visibility: the caller must own the workspace or hold
    an active share. Raises 403 if not authorized, 404 if not found.
    """
    ref = await svc.get_workspace(ctx, workspace_id=workspace_id)
    response = _workspace_ref_to_response(ref)
    return response.model_dump(exclude_none=True)


async def _update_workspace_handler(
    workspace_id: uuid.UUID,
    body: WorkspaceUpdateRequest,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Update a workspace's name, description, or archived_at.

    Authorization: the caller must be the owning actor or an admin in the
    workspace's owning tenant. Share holders cannot update. The service enforces
    this check before applying any change.

    Pass archived_at=null to un-archive a workspace. Omit a field to leave it
    unchanged (name and description are partial-update safe).
    """
    ref = await svc.update_workspace(
        ctx,
        workspace_id=workspace_id,
        name=body.name,
        description=body.description,
        archived_at=body.archived_at,
    )
    response = _workspace_ref_to_response(ref)
    return response.model_dump(exclude_none=True)


async def _delete_workspace_handler(
    workspace_id: uuid.UUID,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> Response:
    """Soft-delete a workspace. Idempotent — a second call returns 204 unchanged.

    Authorization: the caller must be the owning actor or an admin in the
    workspace's owning tenant. The service sets t_invalidated_at and treats
    already-deleted workspaces as a no-op rather than an error.
    """
    await svc.delete_workspace(ctx, workspace_id=workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Workspace GET is a pure read — registered directly on mutation_router so it
# shares the /v1/workspaces prefix with the PATCH/DELETE routes.
mutation_router.add_api_route(
    "/{workspace_id}",
    _get_workspace_handler,
    methods=["GET"],
    summary="Get a workspace by ID",
)

_mut_mr.add_mutation_route(
    path="/{workspace_id}",
    action="update",
    handler=_update_workspace_handler,
    verb="PATCH",
    summary="Update a workspace (name, description, archived_at)",
)

_mut_mr.add_mutation_route(
    path="/{workspace_id}",
    action="delete",
    handler=_delete_workspace_handler,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Soft-delete a workspace (idempotent)",
)


# ---------------------------------------------------------------------------
# Entry mutation endpoints — PATCH + DELETE via HttpMethodRouter
# ---------------------------------------------------------------------------

entry_mutation_router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])
_entry_mut_mr = HttpMethodRouter(entry_mutation_router, mode=_mode, separator=_sep)


async def _update_entry_handler(
    workspace_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: EntryUpdateRequest,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Update a workspace entry's body, reference_ids, or references_jsonb.

    Authorization: the caller must own the workspace or hold an active
    contributor share. The service enforces this via workspace visibility before
    writing.

    PII scanner runs on any provided body_md or references_jsonb. A block hit
    returns 422 and the entry is NOT updated. A warn hit updates the entry and
    returns a ``warnings`` list in the response.

    Only supplied fields are updated; omitted fields retain their current values.
    """
    ref = await svc.update_entry(
        ctx,
        entry_id=entry_id,
        body_md=body.body_md,
        reference_ids=body.reference_ids,
        references_jsonb=body.references_jsonb,
    )
    response = _entry_ref_to_response(ref)
    return response.model_dump(exclude_none=True)


async def _delete_entry_handler(
    workspace_id: uuid.UUID,
    entry_id: uuid.UUID,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> Response:
    """Soft-delete a workspace entry. Idempotent — a second call returns 204 unchanged.

    Authorization: the caller must own the workspace or hold an active contributor
    share. The service enforces this via get_workspace before writing.
    """
    await svc.delete_entry(ctx, entry_id=entry_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_entry_mut_mr.add_mutation_route(
    path="/{workspace_id}/entries/{entry_id}",
    action="update",
    handler=_update_entry_handler,
    verb="PATCH",
    summary="Update a workspace entry",
)

_entry_mut_mr.add_mutation_route(
    path="/{workspace_id}/entries/{entry_id}",
    action="delete",
    handler=_delete_entry_handler,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Soft-delete a workspace entry (idempotent)",
)


# ---------------------------------------------------------------------------
# Share collection endpoints — GET + POST
# Shares nest under /v1/workspaces/{workspace_id}/shares.
# ---------------------------------------------------------------------------

share_router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


@share_router.get(
    "/{workspace_id}/shares",
    summary="List active shares on a workspace",
)
async def list_shares(
    workspace_id: Annotated[uuid.UUID, Path(description="Workspace UUID")],
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """List active (not revoked) shares for a workspace.

    Authorization: caller must be the workspace owner or an admin in the
    workspace's owning tenant. The service enforces this before returning data.

    Returns only shares where revoked_at IS NULL. For full audit history
    (including revocations), query the audit_log.
    """
    refs = await svc.list_shares(ctx, workspace_id=workspace_id)
    items = [_share_ref_to_response(r).model_dump(exclude_none=True) for r in refs]
    return {"items": items}


@share_router.post(
    "/{workspace_id}/shares",
    status_code=status.HTTP_201_CREATED,
    summary="Grant a share on a workspace",
)
async def grant_share(
    workspace_id: Annotated[uuid.UUID, Path(description="Workspace UUID")],
    body: ShareCreateRequest,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> dict[str, Any]:
    """Grant an actor access to a workspace.

    Authorization: caller must be the workspace owner or an admin in the
    workspace's owning tenant.

    Actor-owned workspaces may only be shared within the same tenant — a
    cross-tenant share attempt returns 422. Tenant-owned workspaces allow
    cross-tenant shares.

    A 409 is returned when an active (non-revoked) share already exists for
    the grantee. Granting after a prior revocation is allowed: a new row is
    inserted because the unique partial index only covers active rows.
    """
    ref = await svc.grant_share(
        ctx,
        workspace_id=workspace_id,
        grantee_actor_id=body.grantee_actor_id,
        grantee_tenant_id=body.grantee_tenant_id,
        role=body.role,
    )
    response = _share_ref_to_response(ref)
    return response.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Share mutation endpoint — DELETE via HttpMethodRouter
# ---------------------------------------------------------------------------

share_mutation_router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])
_share_mut_mr = HttpMethodRouter(share_mutation_router, mode=_mode, separator=_sep)


async def _delete_share_handler(
    workspace_id: uuid.UUID,
    share_id: uuid.UUID,
    svc: WorkspaceService = Depends(get_workspace_service),
    ctx: TenantContext = Depends(_any_required),
) -> Response:
    """Revoke a workspace share. Idempotent — a second call returns 204 unchanged.

    Authorization: caller must be the workspace owner or an admin in the
    workspace's owning tenant. The service enforces this before revoking.

    Raises 404 if share_id does not exist.
    """
    await svc.revoke_share(ctx, share_id=share_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_share_mut_mr.add_mutation_route(
    path="/{workspace_id}/shares/{share_id}",
    action="delete",
    handler=_delete_share_handler,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revoke a workspace share (idempotent)",
)


__all__ = [
    "router",
    "mutation_router",
    "entry_mutation_router",
    "share_router",
    "share_mutation_router",
    "get_workspace_service",
    "_build_workspace_service",
    "WorkspaceResponse",
    "EntryResponse",
    "ShareResponse",
    "SearchResponse",
    "WarningEntry",
]
