"""JIT tenant materialization for RSAM SEAL IDs.

When a SEAL token arrives for a SEAL ID not yet known to the registry, this
module inserts a tenants row on first sight and emits a `tenant.jit_created`
audit event.  Subsequent arrivals of the same SEAL ID hit the DO NOTHING path
— no duplicate row, no duplicate audit event.

Only this module (plus the pre-existing auth/resolver.py and the platform
management handler) is permitted to INSERT INTO tenants.  Keeping the JIT
path isolated here makes the lint-gate allowlist surgical and keeps
claim_source.py focused on token-to-identity resolution.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


async def upsert_rsam_tenant(session: AsyncSession, seal_id: str) -> uuid.UUID:
    """JIT-materialize a registry tenant for an RSAM SEAL ID.

    On first sight, INSERT a tenants row with provider='jit',
    external_tenant_id=seal_id, slug=seal_id,
    display_name='SEAL <seal_id>', is_active=True. Emit a
    `tenant.jit_created` audit event with payload
    {tenant_id, external_tenant_id, provider='jit', source='rsam'}.

    On re-sight (DO NOTHING path), no audit event is emitted.
    Returns the catalog-internal tenant_id UUID from RETURNING or a
    follow-up SELECT.

    The audit row is written in the same session transaction as the
    tenant INSERT so that tenant creation and its audit record are
    committed atomically — the system operation that creates a tenant
    has no pre-existing actor context, and the audit row must not be
    silently lost on a connection failure after the INSERT commits.
    """
    new_tenant_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)

    result = await session.execute(
        text(
            "INSERT INTO tenants "
            "(tenant_id, slug, display_name, created_at, is_active, "
            " external_tenant_id, provider) "
            "VALUES "
            "(:tenant_id, :slug, :display_name, :created_at, :is_active, "
            " :external_tenant_id, 'jit') "
            "ON CONFLICT (external_tenant_id, provider) "
            "WHERE external_tenant_id IS NOT NULL "
            "DO NOTHING "
            "RETURNING tenant_id"
        ),
        {
            "tenant_id": new_tenant_id,
            "slug": seal_id,
            "display_name": f"SEAL {seal_id}",
            "created_at": now,
            "is_active": True,
            "external_tenant_id": seal_id,
        },
    )
    row = result.fetchone()

    if row is not None:
        # First sight — the INSERT succeeded; emit audit in the same transaction.
        tenant_id: uuid.UUID = row[0]
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "(audit_id, tenant_id, actor_id, action, target_type, "
                " target_id, before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES "
                "(:audit_id, :tenant_id, NULL, 'tenant.jit_created', 'tenant', "
                " :target_id, NULL, CAST(:after_jsonb AS jsonb), :ts, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "target_id": tenant_id,
                "after_jsonb": (
                    f'{{"tenant_id": "{tenant_id}", '
                    f'"external_tenant_id": "{seal_id}", '
                    f'"provider": "jit", '
                    f'"source": "rsam"}}'
                ),
                "ts": now,
            },
        )
        _log.info(
            "rsam_tenant_jit_created",
            extra={"tenant_id": str(tenant_id), "seal_id": seal_id},
        )
        return tenant_id

    # Re-sight — the DO NOTHING path; return the existing tenant's UUID.
    existing = await session.execute(
        text("SELECT tenant_id FROM tenants " "WHERE external_tenant_id = :seal_id AND provider = 'jit'"),
        {"seal_id": seal_id},
    )
    existing_row = existing.fetchone()
    if existing_row is None:
        msg = f"upsert_rsam_tenant: DO NOTHING conflict but no row found for seal_id={seal_id!r}"
        raise RuntimeError(msg)
    return existing_row[0]  # type: ignore[no-any-return]


async def upsert_rsam_actor(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    oidc_subject: str,
) -> None:
    """JIT-upsert an actor row for an IDA-authenticated user.

    On first sight, INSERT a row with actor_kind='human', display_name
    equal to oidc_subject (a stable fallback until an admin updates it),
    and email=NULL. Emits an `actor.jit_created` audit event in the same
    transaction so the creation is recorded atomically.

    On re-sight (DO NOTHING path, because idx_actors_oidc is a partial
    unique index on (tenant_id, oidc_subject) WHERE oidc_subject IS NOT
    NULL), no row is inserted and no audit event is emitted.

    Returns nothing — the caller queries actors directly for the row
    after this call returns.
    """
    new_actor_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)

    result = await session.execute(
        text(
            "INSERT INTO actors "
            "(actor_id, tenant_id, oidc_subject, display_name, email, created_at, actor_kind) "
            "VALUES "
            "(:actor_id, :tenant_id, :oidc_subject, :display_name, NULL, :created_at, 'human') "
            "ON CONFLICT (tenant_id, oidc_subject) "
            "WHERE oidc_subject IS NOT NULL "
            "DO NOTHING "
            "RETURNING actor_id"
        ),
        {
            "actor_id": new_actor_id,
            "tenant_id": tenant_id,
            "oidc_subject": oidc_subject,
            "display_name": oidc_subject,
            "created_at": now,
        },
    )
    row = result.fetchone()

    if row is not None:
        # First sight — INSERT succeeded; emit audit in the same transaction.
        actor_id: uuid.UUID = row[0]
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "(audit_id, tenant_id, actor_id, action, target_type, "
                " target_id, before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES "
                "(:audit_id, :tenant_id, NULL, 'actor.jit_created', 'actor', "
                " :target_id, NULL, CAST(:after_jsonb AS jsonb), :ts, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "target_id": actor_id,
                "after_jsonb": (
                    f'{{"actor_id": "{actor_id}", '
                    f'"tenant_id": "{tenant_id}", '
                    f'"oidc_subject": "{oidc_subject}", '
                    f'"source": "rsam"}}'
                ),
                "ts": now,
            },
        )
        _log.info(
            "rsam_actor_jit_created",
            extra={"actor_id": str(actor_id), "tenant_id": str(tenant_id)},
        )


__all__ = ["upsert_rsam_tenant", "upsert_rsam_actor"]
