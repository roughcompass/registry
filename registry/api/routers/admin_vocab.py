"""Admin vocabulary + capability-type schema endpoints.

Vocabulary endpoints:
  GET    /v1/admin/vocabularies/{kind}           — list all values for kind
  POST   /v1/admin/vocabularies/{kind}           — add value (admin)
  PATCH  /v1/admin/vocabularies/{kind}/{value}   — update (deprecate)
  DELETE /v1/admin/vocabularies/{kind}/{value}   — soft-delete (deprecated_at = now())

Capability-type schema endpoints:
  GET    /v1/admin/capability-types              — list all type schemas
  POST   /v1/admin/capability-types             — create (admin)
  GET    /v1/admin/capability-types/{type_name} — get by name
  PATCH  /v1/admin/capability-types/{type_name} — update (flip is_advisory)
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from registry.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.routers._admin_common import _admin_required
from registry.api.schemas import Links
from registry.storage.models import CapabilityTypeSchema, VocabularyValue
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin")


# ---------------------------------------------------------------------------
# Pydantic schemas — vocabulary
# ---------------------------------------------------------------------------


class VocabularyValueResponse(BaseModel):
    vocab_id: uuid.UUID
    kind: str
    value: str
    is_system: bool
    deprecated_at: datetime.datetime | None
    created_at: datetime.datetime


class VocabularyValueCreate(BaseModel):
    value: str


class VocabularyValuePatch(BaseModel):
    deprecated_at: datetime.datetime | None = None


# ---------------------------------------------------------------------------
# Pydantic schemas — capability-type schemas
# ---------------------------------------------------------------------------


class CapabilityTypeSchemaResponse(BaseModel):
    schema_id: uuid.UUID
    type_name: str
    json_schema: dict[str, Any]
    is_advisory: bool
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime | None
    t_ingested_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class CapabilityTypeSchemaCreate(BaseModel):
    type_name: str
    json_schema: dict[str, Any]
    is_advisory: bool = True
    t_valid_from: datetime.datetime | None = None


class CapabilityTypeSchemaPatch(BaseModel):
    is_advisory: bool | None = None


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _vocab_to_response(v: VocabularyValue) -> VocabularyValueResponse:
    return VocabularyValueResponse(
        vocab_id=v.vocab_id,
        kind=v.kind,
        value=v.value,
        is_system=v.is_system,
        deprecated_at=v.deprecated_at,
        created_at=v.created_at,
    )


def _schema_to_response(s: CapabilityTypeSchema, *, include_links: bool = False) -> CapabilityTypeSchemaResponse:
    return CapabilityTypeSchemaResponse(
        schema_id=s.schema_id,
        type_name=s.type_name,
        json_schema=dict(s.json_schema) if s.json_schema else {},
        is_advisory=s.is_advisory,
        t_valid_from=s.t_valid_from,
        t_valid_to=s.t_valid_to,
        t_ingested_at=s.t_ingested_at,
        t_invalidated_at=s.t_invalidated_at,
        links=Links(self=f"/v1/admin/capability-types/{s.type_name}") if include_links else None,
    )


# ---------------------------------------------------------------------------
# Vocabulary endpoints
# ---------------------------------------------------------------------------


@router.get("/vocabularies/{kind}", response_model=list[VocabularyValueResponse], tags=["admin: vocabulary"])
async def list_vocabulary_values(
    kind: str,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[VocabularyValueResponse]:
    """List all vocabulary values for the given kind (including deprecated)."""
    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(
            select(VocabularyValue).where(
                VocabularyValue.tenant_id == ctx.tenant_id,
                VocabularyValue.kind == kind,
            )
        )
        rows = list(result.scalars().all())
    return [_vocab_to_response(v) for v in rows]


@router.post(
    "/vocabularies/{kind}",
    response_model=VocabularyValueResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin: vocabulary"],
)
async def add_vocabulary_value(
    kind: str,
    body: VocabularyValueCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> VocabularyValueResponse:
    """Add a vocabulary value for the given kind. Idempotent on exact duplicate.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    from registry.service.vocabulary import VocabularyService  # noqa: PLC0415

    vocab_svc = VocabularyService(request.app.state.session_factory)
    await vocab_svc.add_value(ctx, kind, body.value)

    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(
            select(VocabularyValue).where(
                VocabularyValue.tenant_id == ctx.tenant_id,
                VocabularyValue.kind == kind,
                VocabularyValue.value == body.value,
            )
        )
        row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=500, detail="vocabulary row missing after insert")
    response = _vocab_to_response(row)
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


async def patch_vocabulary_value(
    kind: str,
    value: str,
    body: VocabularyValuePatch,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> VocabularyValueResponse:
    """Update a vocabulary value, e.g. set deprecated_at.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a debug warning and
    accepts the write.  ETag is computed from the vocab_id + created_at before
    the write so a stale precondition fails fast.

    There is no detail-GET for individual vocabulary values; the client can
    derive the ETag from the list response or a prior PATCH response using
    the same inputs (vocab_id + created_at).
    """
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        result = await session.execute(
            select(VocabularyValue).where(
                VocabularyValue.tenant_id == ctx.tenant_id,
                VocabularyValue.kind == kind,
                VocabularyValue.value == value,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="vocabulary value not found")
        pre_etag = compute_etag(row.vocab_id, latest_timestamp(row.created_at))
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="vocabulary_value",
        )
        if body.deprecated_at is not None:
            row.deprecated_at = body.deprecated_at
        await session.flush()
        return _vocab_to_response(row)


async def delete_vocabulary_value(
    kind: str,
    value: str,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Soft-delete: sets deprecated_at = now()."""
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        result = await session.execute(
            select(VocabularyValue).where(
                VocabularyValue.tenant_id == ctx.tenant_id,
                VocabularyValue.kind == kind,
                VocabularyValue.value == value,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="vocabulary value not found")
        row.deprecated_at = datetime.datetime.now(tz=datetime.UTC)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Capability-type schema endpoints
# ---------------------------------------------------------------------------


@router.get("/capability-types", response_model=list[CapabilityTypeSchemaResponse], tags=["admin: schemas"])
async def list_capability_types(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[CapabilityTypeSchemaResponse]:
    """List all capability type schemas (current rows only: t_invalidated_at IS NULL)."""
    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(
            select(CapabilityTypeSchema).where(
                CapabilityTypeSchema.tenant_id == ctx.tenant_id,
                CapabilityTypeSchema.t_invalidated_at.is_(None),
            )
        )
        rows = list(result.scalars().all())
    return [_schema_to_response(s) for s in rows]


@router.post(
    "/capability-types",
    response_model=CapabilityTypeSchemaResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin: schemas"],
)
async def create_capability_type(
    body: CapabilityTypeSchemaCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> CapabilityTypeSchemaResponse:
    """Create a new capability type schema.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    now = datetime.datetime.now(tz=datetime.UTC)
    valid_from = body.t_valid_from if body.t_valid_from is not None else now
    schema_id = uuid.uuid4()

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        session.add(
            CapabilityTypeSchema(
                schema_id=schema_id,
                tenant_id=ctx.tenant_id,
                type_name=body.type_name,
                json_schema=body.json_schema,
                is_advisory=body.is_advisory,
                t_valid_from=valid_from,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
                created_by=ctx.actor_id,
            )
        )
        await session.flush()

    async with factory() as session:
        row = await session.get(CapabilityTypeSchema, schema_id)
        if row is None:
            raise HTTPException(status_code=500, detail="schema row missing after insert")
        response = _schema_to_response(row)
        await idem.persist(ctx, 201, response.model_dump(mode="json"))
        return response


@router.get(
    "/capability-types/{type_name}",
    response_model=CapabilityTypeSchemaResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    tags=["admin: schemas"],
)
async def get_capability_type(
    type_name: str,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> CapabilityTypeSchemaResponse:
    """Get the current schema for a given type_name.

    Emits an ``ETag`` header computed from the schema_id + t_ingested_at.
    Clients can echo this value as ``If-Match`` on subsequent PATCH calls
    for optimistic concurrency.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(
            select(CapabilityTypeSchema)
            .where(
                CapabilityTypeSchema.tenant_id == ctx.tenant_id,
                CapabilityTypeSchema.type_name == type_name,
                CapabilityTypeSchema.t_invalidated_at.is_(None),
            )
            .order_by(CapabilityTypeSchema.t_valid_from.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capability type not found")
    response = _schema_to_response(row, include_links=True)
    etag = compute_etag(row.schema_id, latest_timestamp(row.t_ingested_at))
    body = response.model_dump(by_alias=True, exclude_unset=True, mode="json")
    return JSONResponse(content=body, headers={"ETag": etag})  # type: ignore[return-value]


async def patch_capability_type(
    type_name: str,
    body: CapabilityTypeSchemaPatch,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> CapabilityTypeSchemaResponse:
    """Update a capability type schema — currently supports flipping is_advisory.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a debug warning and
    accepts the write.  ETag is computed from schema_id + t_ingested_at before
    the write so a stale precondition fails fast.

    Recommended flow: GET /v1/admin/capability-types/{name} → ETag header
    → PATCH with If-Match.
    """
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        result = await session.execute(
            select(CapabilityTypeSchema)
            .where(
                CapabilityTypeSchema.tenant_id == ctx.tenant_id,
                CapabilityTypeSchema.type_name == type_name,
                CapabilityTypeSchema.t_invalidated_at.is_(None),
            )
            .order_by(CapabilityTypeSchema.t_valid_from.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capability type not found")
        pre_etag = compute_etag(row.schema_id, latest_timestamp(row.t_ingested_at))
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="capability_type",
        )
        if body.is_advisory is not None:
            row.is_advisory = body.is_advisory
        await session.flush()
        return _schema_to_response(row)


# ---------------------------------------------------------------------------
# Mutation router (PATCH/DELETE via HttpMethodRouter)
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/admin")
_mode, _sep = get_mode_settings()
_mutation_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mutation_mr.add_mutation_route(
    path="/vocabularies/{kind}/{value}",
    action="update",
    handler=patch_vocabulary_value,
    verb="PATCH",
    response_model=VocabularyValueResponse,
    tags=["admin: vocabulary"],
)

_mutation_mr.add_mutation_route(
    path="/vocabularies/{kind}/{value}",
    action="delete",
    handler=delete_vocabulary_value,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["admin: vocabulary"],
)

_mutation_mr.add_mutation_route(
    path="/capability-types/{type_name}",
    action="update",
    handler=patch_capability_type,
    verb="PATCH",
    response_model=CapabilityTypeSchemaResponse,
    tags=["admin: schemas"],
)

mutation_router = _mutation_base
