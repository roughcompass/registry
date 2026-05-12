"""Admin PII pattern + field-policy endpoints.

pii-patterns:
  POST   /v1/admin/pii-patterns           — register a custom tenant PII pattern
  GET    /v1/admin/pii-patterns           — list all patterns (including system-seeded)
  PATCH  /v1/admin/pii-patterns/{id}      — partial-update (is_system=True rows return 403)
  DELETE /v1/admin/pii-patterns/{id}      — hard-delete (is_system=True rows return 403)

pii-field-policies:
  POST   /v1/admin/pii-field-policies     — create a per-field policy override
  GET    /v1/admin/pii-field-policies     — list all per-field policy overrides
  DELETE /v1/admin/pii-field-policies/{id} — hard-delete a policy override

PATCH and DELETE on mutation routes are registered via HttpMethodRouter so
REGISTRY_HTTP_METHODS_MODE controls the verb vs. POST-tunneled surface.
Auth: admin role required on all endpoints.
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from registry.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.routers._admin_common import _admin_required
from registry.storage.models import PiiFieldPolicyRow, PiiPatternRow
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin")

_VALID_POLICIES: frozenset[str] = frozenset({"advisory", "warn", "block"})


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class PiiPatternCreate(BaseModel):
    name: str
    category: str
    regex: str
    policy_override: str | None = None
    is_enabled: bool = True


class PiiPatternPatch(BaseModel):
    category: str | None = None
    regex: str | None = None
    policy_override: str | None = None
    is_enabled: bool | None = None


class PiiPatternResponse(BaseModel):
    pattern_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    category: str
    regex: str
    is_system: bool
    detector_module: str | None
    policy_override: str | None
    is_enabled: bool
    created_at: datetime.datetime
    created_by: uuid.UUID | None


class PiiFieldPolicyCreate(BaseModel):
    field_type: str
    pattern_id: uuid.UUID | None = None
    policy: str


class PiiFieldPolicyResponse(BaseModel):
    policy_id: uuid.UUID
    tenant_id: uuid.UUID
    field_type: str
    pattern_id: uuid.UUID | None
    policy: str
    created_at: datetime.datetime


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _pattern_to_response(row: PiiPatternRow) -> PiiPatternResponse:
    return PiiPatternResponse(
        pattern_id=row.pattern_id,
        tenant_id=row.tenant_id,
        name=row.name,
        category=row.category,
        regex=row.regex,
        is_system=row.is_system,
        detector_module=row.detector_module,
        policy_override=row.policy_override,
        is_enabled=row.is_enabled,
        created_at=row.created_at,
        created_by=row.created_by,
    )


def _field_policy_to_response(row: PiiFieldPolicyRow) -> PiiFieldPolicyResponse:
    return PiiFieldPolicyResponse(
        policy_id=row.policy_id,
        tenant_id=row.tenant_id,
        field_type=row.field_type,
        pattern_id=row.pattern_id,
        policy=row.policy,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# pii-patterns — POST / GET
# ---------------------------------------------------------------------------


@router.post(
    "/pii-patterns",
    response_model=PiiPatternResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin: pii"],
)
async def create_pii_pattern(
    body: PiiPatternCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> PiiPatternResponse:
    """Register a custom tenant PII pattern.

    ``is_system`` is always ``False`` for tenant-created patterns.
    Validates that ``regex`` is a syntactically valid Python regex.
    Validates ``policy_override`` is one of ``advisory | warn | block`` when
    provided.  Returns ``422`` on validation failure.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    import re as _re  # noqa: PLC0415

    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    if body.policy_override is not None and body.policy_override not in _VALID_POLICIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"policy_override must be one of {sorted(_VALID_POLICIES)}",
        )
    try:
        _re.compile(body.regex)
    except _re.error as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid regex: {exc}",
        ) from exc

    now = datetime.datetime.now(tz=datetime.UTC)
    pattern_id = uuid.uuid4()

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        session.add(
            PiiPatternRow(
                pattern_id=pattern_id,
                tenant_id=ctx.tenant_id,
                name=body.name,
                category=body.category,
                regex=body.regex,
                is_system=False,
                detector_module=None,
                policy_override=body.policy_override,
                is_enabled=body.is_enabled,
                created_at=now,
                created_by=ctx.actor_id,
            )
        )
        await session.flush()

    async with factory() as session:
        row = await session.get(PiiPatternRow, pattern_id)
        if row is None:
            raise HTTPException(status_code=500, detail="pii_pattern row missing after insert")
        response = _pattern_to_response(row)
        await idem.persist(ctx, 201, response.model_dump(mode="json"))
        return response


@router.get("/pii-patterns", response_model=list[PiiPatternResponse], tags=["admin: pii"])
async def list_pii_patterns(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[PiiPatternResponse]:
    """List all PII patterns for the tenant, including system-seeded rows."""
    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(
            select(PiiPatternRow).where(PiiPatternRow.tenant_id == ctx.tenant_id).order_by(PiiPatternRow.created_at)
        )
        rows = list(result.scalars().all())
    return [_pattern_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# pii-patterns — PATCH / DELETE via HttpMethodRouter
# ---------------------------------------------------------------------------

_pii_pattern_base = APIRouter(prefix="/v1/admin", tags=["admin: pii"])
_mode, _sep = get_mode_settings()
_pii_pattern_mr = HttpMethodRouter(_pii_pattern_base, mode=_mode, separator=_sep)


async def _patch_pii_pattern(
    pattern_id: uuid.UUID,
    body: PiiPatternPatch,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> PiiPatternResponse:
    """Partial-update a tenant PII pattern.

    ``is_system=True`` rows return ``403 Forbidden``.
    ``policy_override`` is validated as ``advisory | warn | block`` when
    provided; ``422`` on invalid value.
    ``regex`` is validated as a syntactically valid Python regex; ``422`` on
    invalid pattern.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a debug warning and
    accepts the write.  ETag is computed from pattern_id + created_at before
    the write so a stale precondition fails fast.
    """
    import re as _re  # noqa: PLC0415

    if body.policy_override is not None and body.policy_override not in _VALID_POLICIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"policy_override must be one of {sorted(_VALID_POLICIES)}",
        )
    if body.regex is not None:
        try:
            _re.compile(body.regex)
        except _re.error as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid regex: {exc}",
            ) from exc

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        row = await session.get(PiiPatternRow, pattern_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pii_pattern not found")
        if row.is_system:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="system PII patterns cannot be modified",
            )
        pre_etag = compute_etag(row.pattern_id, latest_timestamp(row.created_at))
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="pii_pattern",
        )
        if body.category is not None:
            row.category = body.category
        if body.regex is not None:
            row.regex = body.regex
        if body.policy_override is not None:
            row.policy_override = body.policy_override
        if body.is_enabled is not None:
            row.is_enabled = body.is_enabled
        await session.flush()
        return _pattern_to_response(row)


async def _delete_pii_pattern(
    pattern_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Hard-delete a tenant PII pattern.

    ``is_system=True`` rows return ``403 Forbidden``.
    Returns ``204 No Content`` on success or if the row is already absent
    (idempotent).
    """
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        row = await session.get(PiiPatternRow, pattern_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pii_pattern not found")
        if row.is_system:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="system PII patterns cannot be deleted",
            )
        await session.delete(row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_pii_pattern_mr.add_mutation_route(
    path="/pii-patterns/{pattern_id}",
    action="update",
    handler=_patch_pii_pattern,
    verb="PATCH",
    response_model=PiiPatternResponse,
)

_pii_pattern_mr.add_mutation_route(
    path="/pii-patterns/{pattern_id}",
    action="delete",
    handler=_delete_pii_pattern,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)

# Expose as a separate router so main.py can include it.
pii_pattern_router = _pii_pattern_base


# ---------------------------------------------------------------------------
# pii-field-policies — POST / GET / DELETE
# ---------------------------------------------------------------------------


@router.post(
    "/pii-field-policies",
    response_model=PiiFieldPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin: pii"],
)
async def create_pii_field_policy(
    body: PiiFieldPolicyCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> PiiFieldPolicyResponse:
    """Create a per-field (optionally per-pattern) PII policy override.

    ``policy`` must be one of ``advisory | warn | block``; returns ``422`` on
    invalid value.  The DB unique index ``uq_field_policy`` ensures at most one
    NULL-pattern row per ``(tenant_id, field_type)``; duplicate insert returns
    ``409 Conflict``.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    if body.policy not in _VALID_POLICIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"policy must be one of {sorted(_VALID_POLICIES)}",
        )

    now = datetime.datetime.now(tz=datetime.UTC)
    policy_id = uuid.uuid4()

    factory = request.app.state.session_factory
    try:
        async with factory() as session, session.begin():
            session.add(
                PiiFieldPolicyRow(
                    policy_id=policy_id,
                    tenant_id=ctx.tenant_id,
                    field_type=body.field_type,
                    pattern_id=body.pattern_id,
                    policy=body.policy,
                    created_at=now,
                )
            )
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a field policy for this (field_type, pattern_id) already exists",
        ) from exc

    async with factory() as session:
        row = await session.get(PiiFieldPolicyRow, policy_id)
        if row is None:
            raise HTTPException(status_code=500, detail="pii_field_policy row missing after insert")
        response = _field_policy_to_response(row)
        await idem.persist(ctx, 201, response.model_dump(mode="json"))
        return response


@router.get("/pii-field-policies", response_model=list[PiiFieldPolicyResponse], tags=["admin: pii"])
async def list_pii_field_policies(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[PiiFieldPolicyResponse]:
    """List all per-field PII policy overrides for the tenant."""
    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(
            select(PiiFieldPolicyRow)
            .where(PiiFieldPolicyRow.tenant_id == ctx.tenant_id)
            .order_by(PiiFieldPolicyRow.created_at)
        )
        rows = list(result.scalars().all())
    return [_field_policy_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# pii-field-policies — DELETE via HttpMethodRouter
# ---------------------------------------------------------------------------

_pii_field_policy_base = APIRouter(prefix="/v1/admin", tags=["admin: pii"])
_pii_field_policy_mr = HttpMethodRouter(_pii_field_policy_base, mode=_mode, separator=_sep)


async def _delete_pii_field_policy(
    policy_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Hard-delete a per-field PII policy override.  Returns 204 on success."""
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        row = await session.get(PiiFieldPolicyRow, policy_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pii_field_policy not found")
        await session.delete(row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_pii_field_policy_mr.add_mutation_route(
    path="/pii-field-policies/{policy_id}",
    action="delete",
    handler=_delete_pii_field_policy,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)

pii_field_policy_router = _pii_field_policy_base
