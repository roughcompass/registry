"""Admin RBAC + tenant/actor read endpoints.

Tenant + Actor read endpoints:
  GET /v1/admin/tenants/{slug}    — own tenant record (404 on cross-tenant)
  GET /v1/admin/actors            — list actors in calling tenant (paginated)
  GET /v1/admin/actors/{id}       — one actor in calling tenant (404 on cross-tenant)

Role management endpoints:
  GET    /v1/admin/roles                             — list available roles for tenant
  POST   /v1/admin/actors/{actor_id}/roles           — assign role to actor (admin)
  DELETE /v1/admin/actors/{actor_id}/roles/{role_id} — remove role assignment (admin)

Tenant/actor endpoints resolve the _links.tenant and _links.actor pointers
that GET /v1/whoami emits.  All are admin-only, tenant-scoped, and return
404 (not 403) on cross-tenant or unknown resources so that existence of
other tenants/actors is never confirmed through this surface.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from registry.api.cursor import decode_cursor, encode_cursor
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.routers._admin_common import _admin_required
from registry.api.schemas import ActorListResponse, ActorResponse, Links, TenantResponse
from registry.storage.models import Actor, ActorRole, Role, Tenant
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin")

_ACTOR_MAX_PAGE_SIZE = 200
_ACTOR_DEFAULT_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class RoleResponse(BaseModel):
    role_id: uuid.UUID
    name: str
    permissions: list[str]
    created_at: datetime.datetime


class AssignRoleRequest(BaseModel):
    role_id: uuid.UUID


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _role_to_response(r: Role) -> RoleResponse:
    return RoleResponse(
        role_id=r.role_id,
        name=r.name,
        permissions=list(r.permissions) if r.permissions else [],
        created_at=r.created_at,
    )


# ---------------------------------------------------------------------------
# Tenant endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/tenants/{slug}",
    response_model=TenantResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    tags=["admin: tenants"],
)
async def get_tenant(
    slug: Annotated[str, Path(description="Tenant slug or UUID string")],
    request: Request,
    view: str | None = Query(default=None),
    ctx: TenantContext = Depends(_admin_required),
) -> TenantResponse:
    """Return the calling tenant's record.

    The path accepts the tenant's slug or its UUID in string form.
    Returns 404 if the resolved tenant is not the caller's own tenant —
    cross-tenant lookup is not permitted through this surface (404, not 403,
    so that the existence of other tenants is not confirmed).

    Pass ``?view=audit`` to include ``is_active`` in the response.
    """
    # Resolve slug-or-UUID: try UUID parse first, fall back to slug lookup.
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant: Tenant | None = None
        try:
            tid = uuid.UUID(str(slug))
            tenant = (await session.execute(select(Tenant).where(Tenant.tenant_id == tid))).scalar_one_or_none()
        except ValueError:
            tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()

    # 404 if not found OR if the resolved tenant is not the caller's own tenant.
    if tenant is None or tenant.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    response = TenantResponse(
        tenant_id=tenant.tenant_id,
        slug=tenant.slug,
        display_name=tenant.display_name,
        created_at=tenant.created_at,
        links=Links(self=f"/v1/admin/tenants/{tenant.slug}"),
    )
    if view == "audit":
        response.is_active = tenant.is_active
    return response


# ---------------------------------------------------------------------------
# Actor endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/actors/{actor_id}",
    response_model=ActorResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    tags=["admin: actors"],
)
async def get_actor(
    actor_id: uuid.UUID,
    request: Request,
    view: str | None = Query(default=None),
    ctx: TenantContext = Depends(_admin_required),
) -> ActorResponse:
    """Return one actor record in the calling tenant.

    Returns 404 if the actor does not exist or belongs to a different tenant
    so that cross-tenant actor existence is not confirmed through this surface.

    Pass ``?view=audit`` to include ``oidc_subject`` in the response.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        actor = (await session.execute(select(Actor).where(Actor.actor_id == actor_id))).scalar_one_or_none()

    if actor is None or actor.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="actor not found")

    response = ActorResponse(
        actor_id=actor.actor_id,
        tenant_id=actor.tenant_id,
        display_name=actor.display_name,
        email=actor.email,
        actor_kind=actor.actor_kind,
        created_at=actor.created_at,
        links=Links(self=f"/v1/admin/actors/{actor.actor_id}"),
    )
    if view == "audit":
        response.oidc_subject = actor.oidc_subject
    return response


@router.get(
    "/actors",
    response_model=ActorListResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    tags=["admin: actors"],
)
async def list_actors(
    request: Request,
    view: str | None = Query(default=None),
    page_size: int = Query(default=_ACTOR_DEFAULT_PAGE_SIZE, ge=1, le=_ACTOR_MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    ctx: TenantContext = Depends(_admin_required),
) -> ActorListResponse:
    """List all actors in the calling tenant, cursor-paginated.

    Results are ordered by ``created_at`` ascending then ``actor_id`` for
    stable keyset pagination.  Pass the returned ``next_cursor`` as
    ``?cursor=`` to fetch the next page.  The list is always tenant-scoped.

    Pass ``?view=audit`` to include ``oidc_subject`` on each actor item.
    """
    payload = decode_cursor(cursor)
    factory = request.app.state.session_factory

    async with factory() as session:
        query = select(Actor).where(Actor.tenant_id == ctx.tenant_id)

        if payload:
            after_ts_str = payload.get("ts")
            after_id_str = payload.get("id")
            if after_ts_str and after_id_str:
                after_ts = datetime.datetime.fromisoformat(after_ts_str)
                after_id = uuid.UUID(after_id_str)
                # Keyset: rows after (created_at, actor_id).
                query = query.where(
                    (Actor.created_at > after_ts) | ((Actor.created_at == after_ts) & (Actor.actor_id > after_id))
                )

        query = query.order_by(Actor.created_at.asc(), Actor.actor_id.asc()).limit(page_size + 1)
        rows = list((await session.execute(query)).scalars().all())

    has_more = len(rows) > page_size
    items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor({"ts": last.created_at.isoformat(), "id": str(last.actor_id)})

    actor_items = [
        ActorResponse(
            actor_id=a.actor_id,
            tenant_id=a.tenant_id,
            display_name=a.display_name,
            email=a.email,
            actor_kind=a.actor_kind,
            created_at=a.created_at,
            oidc_subject=a.oidc_subject if view == "audit" else None,
            links=Links(self=f"/v1/admin/actors/{a.actor_id}"),
        )
        for a in items
    ]
    return ActorListResponse(items=actor_items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Role management endpoints
# ---------------------------------------------------------------------------


@router.get("/roles", response_model=list[RoleResponse], tags=["admin: rbac"])
async def list_roles(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[RoleResponse]:
    """List all roles available for the tenant."""
    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(select(Role).where(Role.tenant_id == ctx.tenant_id))
        roles = list(result.scalars().all())
    return [_role_to_response(r) for r in roles]


@router.post(
    "/actors/{actor_id}/roles",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["admin: rbac"],
)
async def assign_role(
    actor_id: uuid.UUID,
    body: AssignRoleRequest,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Assign a role to an actor within the tenant.

    Records granted_at (now) and granted_by (calling actor_id).
    No-op (returns 204) if the assignment already exists.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original 204 response; same key + different body returns 409.
    """
    hit = await idem.lookup(ctx)
    if hit is not None:
        return Response(status_code=hit[0])

    factory = request.app.state.session_factory
    now = datetime.datetime.now(tz=datetime.UTC)

    async with factory() as session, session.begin():
        # Confirm role belongs to this tenant.
        role = await session.get(Role, body.role_id)
        if role is None or role.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="role not found")

        # Check for existing assignment (PK is composite: tenant_id, actor_id, role_id).
        existing = await session.execute(
            select(ActorRole).where(
                ActorRole.tenant_id == ctx.tenant_id,
                ActorRole.actor_id == actor_id,
                ActorRole.role_id == body.role_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        session.add(
            ActorRole(
                tenant_id=ctx.tenant_id,
                actor_id=actor_id,
                role_id=body.role_id,
                granted_at=now,
                granted_by=ctx.actor_id,
            )
        )

    await idem.persist(ctx, 204, {})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/actors/{actor_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["admin: rbac"],
)
async def remove_role(
    actor_id: uuid.UUID,
    role_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Remove a role assignment from an actor within the tenant."""
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        result = await session.execute(
            select(ActorRole).where(
                ActorRole.tenant_id == ctx.tenant_id,
                ActorRole.actor_id == actor_id,
                ActorRole.role_id == role_id,
            )
        )
        assignment = result.scalar_one_or_none()
        if assignment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="role assignment not found")
        await session.delete(assignment)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
