"""Admin CRUD endpoints for progression_definitions and progression_overrides.

Progression definition endpoints (five, require admin role):

  POST   /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}
  PUT    /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}
  DELETE /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}

PUT semantics are supersession, not in-place mutation: a new row is inserted and
the previously-active row for the same (tenant_id, entity_type) has its
t_valid_to set to now in the same transaction.

DELETE is a soft-delete: t_valid_to is set to now; t_invalidated_at remains NULL
(no successor row is created).

Progression override endpoints (two, require admin role):

  POST   /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides
  GET    /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides

Override creation uses audit-before-commit ordering: the audit_log row is written
and committed in its own transaction before the override row is inserted. If the
audit write fails the override is never created; an uncommitted override can never
exist without a committed audit record. The override row stores audit_event_id
pointing at that audit row so the two records are semantically linked.

Audit events emitted:
  - progression.definition.published    (definition POST and PUT)
  - progression.definition.soft_deleted (definition DELETE)
  - progression.override.created        (override POST)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.api.errors import build_error
from registry.api.routers._admin_common import _admin_required
from registry.audit import actions
from registry.exceptions import ValidationError
from registry.service.progression import validate_progression_definition
from registry.storage.models import Attribute, Entity, ProgressionDefinition, ProgressionOverride
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: progression"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ProgressionDefinitionCreate(BaseModel):
    entity_type: str
    definition: dict[str, Any]
    is_advisory: bool = True


class ProgressionDefinitionUpdate(BaseModel):
    definition: dict[str, Any]
    is_advisory: bool | None = None
    # Pre-flight graduation controls — only evaluated when is_advisory flips True→False.
    dry_run: bool = False
    force: bool = False
    migration_plan: str | None = None
    force_timeout_seconds: float = 30.0


class ProgressionDefinitionResponse(BaseModel):
    progression_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    definition: dict[str, Any]
    is_advisory: bool
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime | None
    t_ingested_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None


class ProgressionOverrideCreate(BaseModel):
    from_state: str
    to_state: str
    gate_id: str
    bypass_skip_rules: bool = False
    reason: str
    t_valid_to: datetime.datetime | None = None


class ProgressionOverrideResponse(BaseModel):
    override_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_id: uuid.UUID
    from_state: str
    to_state: str
    gate_id: str
    bypass_skip_rules: bool
    reason: str
    authorized_by: uuid.UUID
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime
    consumed_at: datetime.datetime | None
    audit_event_id: uuid.UUID


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _to_response(row: ProgressionDefinition) -> ProgressionDefinitionResponse:
    return ProgressionDefinitionResponse(
        progression_id=row.progression_id,
        tenant_id=row.tenant_id,
        entity_type=row.entity_type,
        definition=dict(row.definition) if row.definition else {},
        is_advisory=row.is_advisory,
        t_valid_from=row.t_valid_from,
        t_valid_to=row.t_valid_to,
        t_ingested_at=row.t_ingested_at,
        t_invalidated_at=row.t_invalidated_at,
    )


def _override_to_response(row: ProgressionOverride) -> ProgressionOverrideResponse:
    return ProgressionOverrideResponse(
        override_id=row.override_id,
        tenant_id=row.tenant_id,
        entity_id=row.entity_id,
        from_state=row.from_state,
        to_state=row.to_state,
        gate_id=row.gate_id,
        bypass_skip_rules=row.bypass_skip_rules,
        reason=row.reason,
        authorized_by=row.authorized_by,
        t_valid_from=row.t_valid_from,
        t_valid_to=row.t_valid_to,
        consumed_at=row.consumed_at,
        audit_event_id=row.audit_event_id,
    )


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


async def _emit_audit(
    session: AsyncSession,
    ctx: TenantContext,
    action: str,
    payload: dict[str, Any],
    now: datetime.datetime,
) -> None:
    """Write one audit_log row for a progression definition admin event."""
    after_jsonb_str = json.dumps(payload)
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(audit_id, tenant_id, actor_id, action, target_type, "
            " target_id, before_jsonb, after_jsonb, ts, request_id, error_code) "
            "VALUES "
            "(:audit_id, :tenant_id, :actor_id, :action, 'progression_definition', "
            " :target_id, NULL, CAST(:after_jsonb AS jsonb), :ts, NULL, NULL)"
        ),
        {
            "audit_id": uuid.uuid4(),
            "tenant_id": ctx.tenant_id,
            "actor_id": ctx.actor_id,
            "action": action,
            "target_id": uuid.UUID(payload["progression_id"]),
            "after_jsonb": after_jsonb_str,
            "ts": now,
        },
    )


async def _emit_override_audit(
    session: AsyncSession,
    ctx: TenantContext,
    entity_id: uuid.UUID,
    override_id: uuid.UUID,
    payload: dict[str, Any],
    now: datetime.datetime,
) -> uuid.UUID:
    """Write one audit_log row for a progression override creation event.

    Writes before_jsonb=null, after_jsonb=<override spec>. Returns the new
    audit_id so the caller can store it on the override row (audit-before-commit
    ordering: audit row is committed before the override row is inserted).
    """
    audit_id = uuid.uuid4()
    after_jsonb_str = json.dumps(payload)
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(audit_id, tenant_id, actor_id, action, target_type, "
            " target_id, before_jsonb, after_jsonb, ts, request_id, error_code) "
            "VALUES "
            "(:audit_id, :tenant_id, :actor_id, :action, 'progression_override', "
            " :target_id, NULL, CAST(:after_jsonb AS jsonb), :ts, NULL, NULL)"
        ),
        {
            "audit_id": audit_id,
            "tenant_id": ctx.tenant_id,
            "actor_id": ctx.actor_id,
            "action": actions.PROGRESSION_OVERRIDE_CREATED,
            "target_id": entity_id,
            "after_jsonb": after_jsonb_str,
            "ts": now,
        },
    )
    return audit_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/tenants/{tenant_id}/progression-definitions",
    response_model=ProgressionDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_progression_definition(
    tenant_id: uuid.UUID,
    body: ProgressionDefinitionCreate,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionDefinitionResponse:
    """Create the first progression definition for a (tenant, entity_type) pair.

    Validates the definition JSONB against the meta-schema before persisting.
    Returns 422 with structured error paths on schema violations.
    Returns 403 if the caller does not hold the admin role.

    The tenant_id in the URL must match the caller's tenant — the admin role
    dependency already resolves the tenant from the token; cross-tenant writes
    are rejected because ctx.tenant_id will not match a different tenant_id path
    parameter (enforced below).
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    try:
        validate_progression_definition(body.definition)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    now = datetime.datetime.now(tz=datetime.UTC)
    progression_id = uuid.uuid4()

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        session.add(
            ProgressionDefinition(
                progression_id=progression_id,
                tenant_id=ctx.tenant_id,
                entity_type=body.entity_type,
                definition=body.definition,
                is_advisory=body.is_advisory,
                t_valid_from=now,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
            )
        )
        await session.flush()
        await _emit_audit(
            session,
            ctx,
            action=actions.PROGRESSION_DEFINITION_PUBLISHED,
            payload={
                "progression_id": str(progression_id),
                "entity_type": body.entity_type,
                "is_advisory": body.is_advisory,
                "action": "created",
            },
            now=now,
        )

    async with factory() as session:
        row = await session.get(ProgressionDefinition, progression_id)
        if row is None:
            raise HTTPException(status_code=500, detail="progression definition row missing after insert")
    return _to_response(row)


@router.get(
    "/tenants/{tenant_id}/progression-definitions",
    response_model=list[ProgressionDefinitionResponse],
)
async def list_progression_definitions(
    tenant_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[ProgressionDefinitionResponse]:
    """Return all currently-active progression definitions for the tenant.

    Active means: t_valid_to IS NULL AND t_invalidated_at IS NULL.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant read rejected")

    factory = request.app.state.session_factory
    async with factory() as session:
        result = await session.execute(
            select(ProgressionDefinition).where(
                ProgressionDefinition.tenant_id == ctx.tenant_id,
                ProgressionDefinition.t_valid_to.is_(None),
                ProgressionDefinition.t_invalidated_at.is_(None),
            )
        )
        rows = list(result.scalars().all())
    return [_to_response(r) for r in rows]


@router.get(
    "/tenants/{tenant_id}/progression-definitions/{progression_id}",
    response_model=ProgressionDefinitionResponse,
)
async def get_progression_definition(
    tenant_id: uuid.UUID,
    progression_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionDefinitionResponse:
    """Return a specific progression definition by progression_id.

    Returns 404 if the row does not exist or belongs to a different tenant.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant read rejected")

    factory = request.app.state.session_factory
    async with factory() as session:
        row = await session.get(ProgressionDefinition, progression_id)
    if row is None or row.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")
    return _to_response(row)


@router.put(
    "/tenants/{tenant_id}/progression-definitions/{progression_id}",
    response_model=ProgressionDefinitionResponse,
    status_code=status.HTTP_200_OK,
)
async def supersede_progression_definition(
    tenant_id: uuid.UUID,
    progression_id: uuid.UUID,
    body: ProgressionDefinitionUpdate,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionDefinitionResponse:
    """Supersede a progression definition — inserts a new row and closes the active one.

    The progression_id in the URL identifies which active definition to supersede.
    A new row is inserted; the previously-active row for the same (tenant_id,
    entity_type) has its t_valid_to set to now. Both writes happen in a single
    transaction so there is never a gap or overlap in the validity window.

    When the incoming body flips is_advisory from True to False, a pre-flight scan
    runs before writing. The scan validates every entity of the same (tenant_id,
    entity_type) against the proposed enforcing definition and collects offenders.
    Four outcome paths:

    - dry_run=True: return 200 with offender list; do NOT write.
    - force=True + migration_plan: skip scan, write immediately; migration_plan is
      recorded in the audit payload so the bypass is discoverable.
    - force=True without migration_plan: return 400.
    - Scan times out (force_timeout_seconds exceeded): return 409 with partial results.
    - Offenders found with force=False: return 409 with offender list.
    - Zero offenders: write normally.

    Validates the new definition JSONB before writing.
    Emits audit event progression.definition.published.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    try:
        validate_progression_definition(body.definition)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    now = datetime.datetime.now(tz=datetime.UTC)
    new_progression_id = uuid.uuid4()

    factory = request.app.state.session_factory

    # Load the row being superseded first (outside the write transaction) to
    # determine whether this is an advisory→enforcing graduation.
    async with factory() as session:
        prior = await session.get(ProgressionDefinition, progression_id)
    if prior is None or prior.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")

    entity_type = prior.entity_type
    new_is_advisory = body.is_advisory if body.is_advisory is not None else prior.is_advisory
    advisory_flip = prior.is_advisory is True and new_is_advisory is False

    if advisory_flip:
        # Validate guard: force=True requires migration_plan.
        if body.force and not body.migration_plan:
            raise build_error(
                status.HTTP_400_BAD_REQUEST,
                code="migration_plan_required",
                message="force=true requires migration_plan to be provided",
            )

        if body.force and body.migration_plan:
            # Path D — operator has accepted the risk; skip pre-flight, write immediately.
            pass  # falls through to the write block below
        else:
            # Run pre-flight scan, bounded by force_timeout_seconds.
            async def _scan() -> list[dict[str, Any]]:
                offenders: list[dict[str, Any]] = []
                async with factory() as scan_session:
                    # Load all active entities of this (tenant_id, entity_type).
                    ent_result = await scan_session.execute(
                        select(Entity).where(
                            Entity.tenant_id == ctx.tenant_id,
                            Entity.entity_type == entity_type,
                            Entity.is_active.is_(True),
                        )
                    )
                    entities = list(ent_result.scalars().all())

                    for ent in entities:
                        # Load its current stage_progression attribute value.
                        attr_result = await scan_session.execute(
                            select(Attribute)
                            .where(
                                Attribute.tenant_id == ctx.tenant_id,
                                Attribute.entity_id == ent.entity_id,
                                Attribute.key == "stage_progression",
                                Attribute.t_invalidated_at.is_(None),
                                Attribute.t_valid_to.is_(None),
                            )
                            .limit(1)
                        )
                        stage_attr = attr_result.scalar_one_or_none()
                        current_state = stage_attr.value if stage_attr is not None else None

                        # Validate the current state against the proposed enforcing definition.
                        # We check whether current_state is a valid destination from itself
                        # (i.e. it exists in the definition's state list). If the entity has
                        # no stage_progression, treat it as unmanaged (pass).
                        if current_state is None:
                            continue

                        states = body.definition.get("states", [])
                        valid_state_ids = {s["id"] for s in states}
                        if current_state not in valid_state_ids:
                            offenders.append(
                                {
                                    "entity_id": str(ent.entity_id),
                                    "current_state": current_state,
                                    "validation_error": f"state '{current_state}' is not defined in the new definition",
                                }
                            )
                        else:
                            # Check gate satisfaction for the current state under the new definition.
                            state_def: dict[str, Any] = next((s for s in states if s["id"] == current_state), {})
                            gate_ids = state_def.get("gates", [])
                            # Load all active attributes for gate evaluation.
                            all_attrs_result = await scan_session.execute(
                                select(Attribute).where(
                                    Attribute.tenant_id == ctx.tenant_id,
                                    Attribute.entity_id == ent.entity_id,
                                    Attribute.t_invalidated_at.is_(None),
                                    Attribute.t_valid_to.is_(None),
                                )
                            )
                            attr_dict = {row.key: row.value for row in all_attrs_result.scalars()}

                            from registry.service.progression import is_gate_satisfied  # noqa: PLC0415

                            failing_gates = [g for g in gate_ids if not is_gate_satisfied(g, attr_dict)]
                            if failing_gates:
                                offenders.append(
                                    {
                                        "entity_id": str(ent.entity_id),
                                        "current_state": current_state,
                                        "validation_error": f"gates not satisfied: {', '.join(failing_gates)}",
                                    }
                                )
                return offenders

            try:
                offenders = await asyncio.wait_for(_scan(), timeout=body.force_timeout_seconds)
            except TimeoutError:
                # Path C — partial results.
                raise build_error(
                    status.HTTP_409_CONFLICT,
                    code="preflight_timeout",
                    message="pre-flight scan exceeded force_timeout_seconds; no definition written",
                ) from None

            if body.dry_run:
                # Path A — report findings without writing.
                return Response(  # type: ignore[return-value]
                    content=json.dumps({"dry_run": True, "offenders": offenders}),
                    status_code=status.HTTP_200_OK,
                    media_type="application/json",
                )

            if offenders:
                # Path B — blocked; caller must use force=True.
                # Encode offenders into the message as JSON so they survive the error
                # envelope normalisation and remain accessible to the caller.
                raise build_error(
                    status.HTTP_409_CONFLICT,
                    code="preflight_offenders_present",
                    message=json.dumps(
                        {
                            "offenders": offenders,
                            "hint": "Pass force=true with migration_plan to proceed.",
                        }
                    ),
                )
            # Path E — zero offenders; fall through to write.

    # Write the supersession (Paths D and E for advisory_flip; also all non-flip PUTs).
    async with factory() as session, session.begin():
        # Reload prior within the write transaction to prevent a TOCTOU gap.
        prior_write = await session.get(ProgressionDefinition, progression_id)
        if prior_write is None or prior_write.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")

        # Close the active row for this (tenant_id, entity_type).
        result = await session.execute(
            select(ProgressionDefinition).where(
                ProgressionDefinition.tenant_id == ctx.tenant_id,
                ProgressionDefinition.entity_type == entity_type,
                ProgressionDefinition.t_valid_to.is_(None),
                ProgressionDefinition.t_invalidated_at.is_(None),
            )
        )
        active_rows = list(result.scalars().all())
        for active_row in active_rows:
            active_row.t_valid_to = now

        # Insert the new row.
        session.add(
            ProgressionDefinition(
                progression_id=new_progression_id,
                tenant_id=ctx.tenant_id,
                entity_type=entity_type,
                definition=body.definition,
                is_advisory=new_is_advisory,
                t_valid_from=now,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
            )
        )
        await session.flush()

        # Build audit payload. When the operator force-bypassed pre-flight with a
        # migration_plan, include the plan in the audit record so the bypass is
        # visible in the audit log and traceable.
        audit_payload: dict[str, Any] = {
            "progression_id": str(new_progression_id),
            "superseded_id": str(progression_id),
            "entity_type": entity_type,
            "is_advisory": new_is_advisory,
            "action": "superseded",
        }
        if advisory_flip and body.force and body.migration_plan:
            audit_payload["migration_plan"] = body.migration_plan

        await _emit_audit(
            session,
            ctx,
            action=actions.PROGRESSION_DEFINITION_PUBLISHED,
            payload=audit_payload,
            now=now,
        )

    async with factory() as session:
        row = await session.get(ProgressionDefinition, new_progression_id)
        if row is None:
            raise HTTPException(status_code=500, detail="progression definition row missing after supersession")
    return _to_response(row)


@router.delete(
    "/tenants/{tenant_id}/progression-definitions/{progression_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def soft_delete_progression_definition(
    tenant_id: uuid.UUID,
    progression_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Soft-delete a progression definition by setting t_valid_to = now.

    No successor row is inserted. t_invalidated_at remains NULL.
    Emits audit event progression.definition.soft_deleted.
    Returns 404 if not found or not owned by this tenant.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    now = datetime.datetime.now(tz=datetime.UTC)

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        row = await session.get(ProgressionDefinition, progression_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")
        row.t_valid_to = now
        await session.flush()
        await _emit_audit(
            session,
            ctx,
            action=actions.PROGRESSION_DEFINITION_SOFT_DELETED,
            payload={
                "progression_id": str(progression_id),
                "entity_type": row.entity_type,
                "action": "soft_deleted",
            },
            now=now,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Override endpoints
# ---------------------------------------------------------------------------

_ONE_HOUR = datetime.timedelta(hours=1)


@router.post(
    "/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
    response_model=ProgressionOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_progression_override(
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    body: ProgressionOverrideCreate,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionOverrideResponse:
    """Create a single-use progression gate override for a specific entity.

    Follows audit-before-commit ordering: the audit_log row is written and
    committed in its own transaction before the override row is inserted.
    If the audit write fails the override is never created — a silently-created
    override with no audit record is structurally impossible.

    Default t_valid_to: now + 1 hour when the caller omits the field.
    Default bypass_skip_rules: False — must be an explicit opt-in.
    authorized_by is always set to the actor making the request.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    now = datetime.datetime.now(tz=datetime.UTC)
    t_valid_to = body.t_valid_to if body.t_valid_to is not None else now + _ONE_HOUR
    override_id = uuid.uuid4()

    factory = request.app.state.session_factory

    # Step 1: write audit row in its own committed transaction.
    # If this fails we raise HTTP 500 and never reach the override insert.
    audit_payload: dict[str, Any] = {
        "override_id": str(override_id),
        "entity_id": str(entity_id),
        "from_state": body.from_state,
        "to_state": body.to_state,
        "gate_id": body.gate_id,
        "bypass_skip_rules": body.bypass_skip_rules,
        "reason": body.reason,
        "authorized_by": str(ctx.actor_id),
        "t_valid_to": t_valid_to.isoformat(),
    }
    try:
        async with factory() as audit_session, audit_session.begin():
            audit_id = await _emit_override_audit(
                audit_session,
                ctx,
                entity_id,
                override_id,
                audit_payload,
                now,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="audit write failed; override not created",
        ) from exc

    # Step 2: insert override row referencing the committed audit row.
    async with factory() as session, session.begin():
        session.add(
            ProgressionOverride(
                override_id=override_id,
                tenant_id=ctx.tenant_id,
                entity_id=entity_id,
                from_state=body.from_state,
                to_state=body.to_state,
                gate_id=body.gate_id,
                bypass_skip_rules=body.bypass_skip_rules,
                reason=body.reason,
                authorized_by=ctx.actor_id,
                t_valid_from=now,
                t_valid_to=t_valid_to,
                consumed_at=None,
                audit_event_id=audit_id,
            )
        )

    async with factory() as session:
        row = await session.get(ProgressionOverride, override_id)
        if row is None:
            raise HTTPException(status_code=500, detail="override row missing after insert")
    return _override_to_response(row)


@router.get(
    "/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
    response_model=list[ProgressionOverrideResponse],
)
async def list_progression_overrides(
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
    consumed: bool | None = Query(default=None),
    expired: bool | None = Query(default=None),
    from_state: str | None = Query(default=None),
    to_state: str | None = Query(default=None),
) -> list[ProgressionOverrideResponse]:
    """List progression overrides for an entity with optional filters.

    Query parameters:
      consumed=true   — only overrides where consumed_at IS NOT NULL
      consumed=false  — only overrides where consumed_at IS NULL
      expired=true    — only overrides where t_valid_to < now()
      expired=false   — only overrides where t_valid_to >= now()
      from_state      — exact match on from_state
      to_state        — exact match on to_state
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant read rejected")

    now = datetime.datetime.now(tz=datetime.UTC)

    factory = request.app.state.session_factory
    async with factory() as session:
        stmt = select(ProgressionOverride).where(
            ProgressionOverride.tenant_id == ctx.tenant_id,
            ProgressionOverride.entity_id == entity_id,
        )
        if consumed is True:
            stmt = stmt.where(ProgressionOverride.consumed_at.is_not(None))
        elif consumed is False:
            stmt = stmt.where(ProgressionOverride.consumed_at.is_(None))

        if expired is True:
            stmt = stmt.where(ProgressionOverride.t_valid_to < now)
        elif expired is False:
            stmt = stmt.where(ProgressionOverride.t_valid_to >= now)

        if from_state is not None:
            stmt = stmt.where(ProgressionOverride.from_state == from_state)
        if to_state is not None:
            stmt = stmt.where(ProgressionOverride.to_state == to_state)

        result = await session.execute(stmt)
        rows = list(result.scalars().all())
    return [_override_to_response(r) for r in rows]
