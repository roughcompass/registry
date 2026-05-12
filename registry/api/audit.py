"""Audit log emit.

`emit()` writes one `audit_log` row in a transaction **separate** from the
service-layer write that triggered it. Failure to write the audit row is
swallowed and recorded — never re-raised — so a failed audit cannot mask
or rollback the underlying mutation. The Prometheus counter
`catalog_audit_write_failures_total` increments on each swallowed failure
so monitoring catches drift.

Audit emit must succeed even when the request has failed: service mutation
paths invoke `emit()` from a `finally:` block, passing `error_code` so
failed writes are recorded with their reason.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.storage.models import AuditLog
from registry.types import Clock, TenantContext

_log = logging.getLogger(__name__)

AUDIT_WRITE_FAILURES = Counter(
    "catalog_audit_write_failures_total",
    "Count of audit_log writes that raised an exception and were swallowed.",
)


async def emit(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    clock: Clock,
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str | None = None,
    error_code: str | None = None,
) -> None:
    """Write a single audit_log row in its own transaction. Never re-raises."""
    try:
        async with session_factory() as session, session.begin():
            session.add(
                AuditLog(
                    audit_id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    actor_id=ctx.actor_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    before_jsonb=before,
                    after_jsonb=after,
                    ts=clock.now(),
                    request_id=request_id,
                    error_code=error_code,
                )
            )
    except Exception:
        AUDIT_WRITE_FAILURES.inc()
        _log.exception(
            "audit_log_write_failed",
            extra={
                "tenant_id": str(ctx.tenant_id),
                "action": action,
                "target_type": target_type,
                "target_id": str(target_id),
            },
        )


__all__ = ["emit", "AUDIT_WRITE_FAILURES"]
