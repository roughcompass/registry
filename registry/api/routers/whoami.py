"""GET /v1/whoami — session context for the calling bearer token.

One read: resolve the bearer token / OIDC JWT to actor + tenant + roles +
display fields so a UI / MCP client can render permission-gated UI
elements without inferring identity from error responses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from registry.api.middleware.tenant import get_tenant_context
from registry.api.schemas import Links, WhoAmIResponse
from registry.service.identity import resolve_whoami
from registry.types import TenantContext

router = APIRouter(tags=["whoami"])


@router.get(
    "/v1/whoami",
    response_model=WhoAmIResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
)
async def whoami(
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
) -> WhoAmIResponse:
    """Return the actor + tenant + roles the current credential resolves to.

    The roles list is the same list the tenant-middleware attaches to
    the TenantContext (sourced from `api_tokens.roles` for bearer tokens
    or from `actor_roles` for OIDC JWTs).
    """
    session_factory = request.app.state.session_factory
    payload = await resolve_whoami(session_factory, ctx)

    return WhoAmIResponse(
        actor_id=payload.actor_id,
        actor_display_name=payload.actor_display_name,
        actor_email=payload.actor_email,
        tenant_id=payload.tenant_id,
        tenant_slug=payload.tenant_slug,
        tenant_display_name=payload.tenant_display_name,
        roles=payload.roles,
        token_id=payload.token_id,
        token_expires_at=payload.token_expires_at,
        _links=Links(
            self="/v1/whoami",
            # Forward-looking pointers — these endpoints don't all exist
            # yet, but UI clients can plan around the stable shape.
            tenant=f"/v1/admin/tenants/{payload.tenant_slug}" if payload.tenant_slug else None,
            actor=f"/v1/admin/actors/{payload.actor_id}",
        ),
    )


__all__ = ["router"]
