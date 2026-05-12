"""Breaking-change advisor REST endpoint.

  POST /v1/capabilities/{capability_id}/preview-version
       body: {proposed_version, proposed_interface, interface_format}
       → 200 + BreakingChangePreviewResponse

Read-only — never mutates state. Cross-tenant consumer identifiers in
the response are anonymised: same-tenant consumers carry full identifiers;
cross-tenant ones carry opaque counter/hash placeholders so the provider
learns impact size without learning which external tenants are affected.

Auth: ``producer`` or ``admin`` (only the producer can preview their
own capability's version bump).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel

from registry.api.auth.context import ROLE_ADMIN, ROLE_PRODUCER, require_roles
from registry.api.errors import map_catalog_error
from registry.api.routers._common import get_service
from registry.exceptions import NotFoundError, ValidationError
from registry.service.breaking_change import BreakingChangeAdvisor
from registry.types import TenantContext

_producer_or_admin = require_roles([ROLE_PRODUCER, ROLE_ADMIN])


class PreviewVersionRequest(BaseModel):
    proposed_version: str
    proposed_interface: Any  # dict | str — validated downstream
    interface_format: str


class AffectedConsumer(BaseModel):
    tenant_id: str
    entity_id: str
    name: str | None
    version_pin: str | None


class BreakingChangePreviewResponse(BaseModel):
    capability_id: str
    proposed_version: str
    diff_classification: str
    changes: list[dict[str, Any]]
    affected_consumers: list[AffectedConsumer]
    release_notes_scaffold: str


def _svc(request: Request) -> BreakingChangeAdvisor:
    return request.app.state.breaking_change  # type: ignore[no-any-return]


router = APIRouter(prefix="/v1/capabilities", tags=["breaking-change"])


@router.post(
    "/{capability_id}/preview-version",
    response_model=BreakingChangePreviewResponse,
    summary="Preview the impact of a proposed version + interface change",
)
async def preview_version(
    capability_id: Annotated[str, Path(description="Capability UUID or slug")],
    body: PreviewVersionRequest,
    request: Request,
    ctx: TenantContext = Depends(_producer_or_admin),
) -> BreakingChangePreviewResponse:
    """Read-only advisory: normalize → semver → diff → blast-radius → filter.

    The path segment accepts a UUID or slug-form name.

    Returns the diff classification, the per-element changes, the
    affected-consumer list (cross-tenant entries anonymised), and a
    plain-text release-notes scaffold.
    """
    catalog_svc = get_service(request)
    svc = _svc(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, capability_id)
        preview = await svc.preview_version(
            ctx=ctx,
            capability_id=resolved.entity_id,
            proposed_version=body.proposed_version,
            proposed_interface=body.proposed_interface,
            interface_format=body.interface_format,
        )
    except (NotFoundError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc

    return BreakingChangePreviewResponse(
        capability_id=str(preview.capability_id),
        proposed_version=preview.proposed_version,
        diff_classification=preview.diff_classification,
        changes=preview.changes,
        affected_consumers=[AffectedConsumer(**c) for c in preview.affected_consumers],
        release_notes_scaffold=preview.release_notes_scaffold,
    )


__all__ = ["router"]
