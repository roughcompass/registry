"""Admin endpoint for workspace actor personal-data purge (RTBF).

Exposes a single endpoint:

  DELETE /v1/admin/actors/{actor_id}/personal-data

This is a hard-delete operation (not a soft-delete). It physically removes all
workspace content authored by the target actor and revokes any active shares
granted to that actor. The operation is idempotent: a second invocation on the
same actor_id returns counts of 0 (nothing left to purge).

Requires admin role. The requesting admin is recorded in the audit log.

The endpoint returns 200 (not 204) because the counts in PurgeResult are
informative for the admin caller — they confirm what was actually deleted.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from registry.api.routers._admin_common import _admin_required
from registry.service.workspace import PurgeResult, WorkspaceService
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: workspaces"])


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class PurgeResultResponse(BaseModel):
    """JSON shape returned by DELETE /v1/admin/actors/{actor_id}/personal-data."""

    purged_entries: int
    purged_workspaces: int
    revoked_shares: int


# ---------------------------------------------------------------------------
# Service dependency
# ---------------------------------------------------------------------------


def _get_workspace_service(request: Request) -> WorkspaceService:
    """Return the singleton WorkspaceService stored on app.state.

    The singleton is built once at app startup by the workspace router's
    _build_workspace_service factory. All callers — this RTBF endpoint and
    the main workspace/entry CRUD router — share the same instance.
    """
    return request.app.state.workspace_service


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.delete(
    "/actors/{actor_id}/personal-data",
    response_model=PurgeResultResponse,
    status_code=status.HTTP_200_OK,
    summary="Purge all workspace personal data for an actor (RTBF).",
)
async def delete_actor_personal_data(
    actor_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> PurgeResultResponse:
    """Physically delete all workspace content authored by actor_id.

    This endpoint fulfills right-to-be-forgotten (RTBF) requests for workspace
    data. It performs a hard DELETE (not a soft-delete) across:

    - workspace_entries created by the actor
    - actor-owned workspaces that are now empty after entry deletion
    - active workspace shares granted TO the actor (revoke, not delete)

    workspace_share_acceptances rows are retained as an audit trail of
    historical cross-tenant access events. They contain an opaque actor
    identifier, not authored content, and their retention is consistent with
    audit-integrity requirements.

    The operation is idempotent. A second call returns counts of 0.

    Returns 200 with PurgeResult counts (not 204) so the admin caller can
    confirm what was actually purged.

    Raises 403 if the caller does not hold the admin role.
    """
    workspace_svc = _get_workspace_service(request)
    result: PurgeResult = await workspace_svc.purge_actor_personal_data(
        ctx,
        target_actor_id=actor_id,
    )
    return PurgeResultResponse(
        purged_entries=result.purged_entries,
        purged_workspaces=result.purged_workspaces,
        revoked_shares=result.revoked_shares,
    )
