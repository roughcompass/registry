"""Admin audit-log query endpoint.

GET /v1/admin/audit — keyset-paginated audit log query (auditor role)
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from registry.api.cursor import InvalidCursorError, decode_cursor, encode_cursor
from registry.api.errors import build_error
from registry.api.routers._admin_common import _auditor_required
from registry.storage.models import AuditLog
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin")

_AUDIT_MAX_PAGE_SIZE = 500
_AUDIT_DEFAULT_PAGE_SIZE = 50


class AuditRow(BaseModel):
    audit_id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID
    before_jsonb: dict[str, Any] | None
    after_jsonb: dict[str, Any] | None
    ts: datetime.datetime
    request_id: str | None
    error_code: str | None


class AuditResponse(BaseModel):
    items: list[AuditRow]
    next_cursor: str | None


def _audit_to_row(a: AuditLog) -> AuditRow:
    return AuditRow(
        audit_id=a.audit_id,
        actor_id=a.actor_id,
        action=a.action,
        target_type=a.target_type,
        target_id=a.target_id,
        before_jsonb=a.before_jsonb,
        after_jsonb=a.after_jsonb,
        ts=a.ts,
        request_id=a.request_id,
        error_code=a.error_code,
    )


@router.get("/audit", response_model=AuditResponse, tags=["admin: audit"])
async def query_audit_log(
    request: Request,
    ctx: TenantContext = Depends(_auditor_required),
    actor_id: uuid.UUID | None = Query(None),
    action: str | None = Query(None),
    target_type: str | None = Query(None),
    target_id: uuid.UUID | None = Query(None),
    from_dt: datetime.datetime | None = Query(None, alias="from"),
    to_dt: datetime.datetime | None = Query(None, alias="to"),
    cursor: str | None = Query(None),
    page_size: int = Query(_AUDIT_DEFAULT_PAGE_SIZE, ge=1, le=_AUDIT_MAX_PAGE_SIZE),
) -> AuditResponse:
    """Query audit log with keyset pagination.

    tenant_id is always injected from TenantContext — callers cannot query
    another tenant's data.  Sorted DESC by (ts, audit_id).
    """
    factory = request.app.state.session_factory

    # Always scope to the caller's tenant — never allow cross-tenant queries.
    conditions = [AuditLog.tenant_id == ctx.tenant_id]

    if actor_id is not None:
        conditions.append(AuditLog.actor_id == actor_id)
    if action is not None:
        conditions.append(AuditLog.action == action)
    if target_type is not None:
        conditions.append(AuditLog.target_type == target_type)
    if target_id is not None:
        conditions.append(AuditLog.target_id == target_id)
    if from_dt is not None:
        conditions.append(AuditLog.ts >= from_dt)
    if to_dt is not None:
        conditions.append(AuditLog.ts <= to_dt)

    # Keyset: cursor encodes (ts, audit_id); page continues from rows strictly
    # before the cursor position (DESC order: ts < cursor_ts OR (ts == cursor_ts AND audit_id < cursor_id)).
    if cursor is not None:
        try:
            cursor_payload = decode_cursor(cursor, strict=True)
            cursor_ts = datetime.datetime.fromisoformat(cursor_payload["ts"])
            cursor_audit_id = uuid.UUID(cursor_payload["audit_id"])
        except (InvalidCursorError, KeyError, ValueError) as exc:
            raise build_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_cursor",
                message="invalid cursor",
            ) from exc
        conditions.append(
            or_(
                AuditLog.ts < cursor_ts,
                and_(AuditLog.ts == cursor_ts, AuditLog.audit_id < cursor_audit_id),
            )
        )

    stmt = (
        select(AuditLog)
        .where(and_(*conditions))
        .order_by(AuditLog.ts.desc(), AuditLog.audit_id.desc())
        .limit(page_size + 1)
    )

    async with factory() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        last = rows[-1]
        next_cursor = encode_cursor({"ts": last.ts.isoformat(), "audit_id": str(last.audit_id)})

    return AuditResponse(items=[_audit_to_row(r) for r in rows], next_cursor=next_cursor)
