"""Admin token management endpoints.

POST   /v1/admin/tokens           — mint a new API token
DELETE /v1/admin/tokens/{id}      — revoke a token
"""

from __future__ import annotations

import datetime
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from registry.api.auth.tokens import hash_token
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.routers._admin_common import _admin_required
from registry.storage.models import ApiToken
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin")


class MintTokenRequest(BaseModel):
    actor_id: uuid.UUID
    roles: list[str] = Field(default_factory=list)
    description: str | None = None
    expires_days: int | None = None


class MintTokenResponse(BaseModel):
    token_id: uuid.UUID
    actor_id: uuid.UUID
    plaintext_token: str  # surfaced exactly once on mint
    roles: list[str]
    expires_at: datetime.datetime | None


@router.post(
    "/tokens",
    response_model=MintTokenResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin: tokens"],
)
async def mint_token(
    body: MintTokenRequest,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> MintTokenResponse:
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    raw_token = secrets.token_urlsafe(32)
    token_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    expires_at = now + datetime.timedelta(days=body.expires_days) if body.expires_days is not None else None

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        session.add(
            ApiToken(
                token_id=token_id,
                tenant_id=ctx.tenant_id,
                actor_id=body.actor_id,
                token_hash=hash_token(raw_token),
                roles=body.roles,
                description=body.description,
                expires_at=expires_at,
                created_at=now,
                revoked_at=None,
            )
        )

    response = MintTokenResponse(
        token_id=token_id,
        actor_id=body.actor_id,
        plaintext_token=raw_token,
        roles=body.roles,
        expires_at=expires_at,
    )
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


async def revoke_token(
    token_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        result = await session.execute(
            select(ApiToken).where(ApiToken.token_id == token_id, ApiToken.tenant_id == ctx.tenant_id)
        )
        token = result.scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
        token.revoked_at = datetime.datetime.now(tz=datetime.UTC)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Mutation router (PATCH/DELETE via HttpMethodRouter)
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/admin")
_mode, _sep = get_mode_settings()
_mutation_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mutation_mr.add_mutation_route(
    path="/tokens/{token_id}",
    action="delete",
    handler=revoke_token,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["admin: tokens"],
)

mutation_router = _mutation_base
