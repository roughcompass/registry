"""JIT actor + tenant materialization for entitlement-resolved callers.

The resolver calls these helpers per request when it discovers a tenant
or actor row that does not yet exist. The functions are isolated here so
the lint allowlist for ``INSERT INTO tenants`` / ``INSERT INTO actors``
stays narrow and the resolver can focus on token-to-identity logic.

Tenant upserts are operator-overridable: when a tenant is explicitly
disabled (``tenants.disabled_at IS NOT NULL``), the upsert raises
``DisabledTenantError`` rather than re-creating or modifying the row.
The caller (resolver) drops the tuple and logs a WARNING; the request
proceeds without that tenant in the resolved grants.

Actor upserts use ``DO UPDATE … RETURNING`` so the actor_id is always
returned — first sight or otherwise — without a follow-up SELECT.
``display_name`` is updated on conflict so a subsequent IDP claim that
populates ``name`` propagates without separate sync.

Schema dependency: the ``tenants.disabled_at`` column and the slimmed
``actors`` schema (no ``email``, no ``actor_kind``) are introduced by the
auth-consolidation Alembic migration. These functions assume the
migration has run; they will fail against an un-migrated database.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


class DisabledTenantError(Exception):
    """Raised when an entitlement references a tenant the operator has disabled.

    The operator's ``disabled_at`` setting is the override mechanism for
    JIT tenant materialization: the entitlement service may grant access
    to a slug, but if the operator has explicitly disabled that tenant
    in the registry, the entitlement must not produce a usable grant —
    and the disabled row must not be modified or re-created.
    """

    def __init__(self, slug: str) -> None:
        super().__init__(f"tenant {slug!r} is disabled by operator (tenants.disabled_at IS NOT NULL)")
        self.slug = slug


async def upsert_entitlement_tenant(session: AsyncSession, slug: str) -> uuid.UUID:
    """Find-or-create a tenant row by slug.

    Three cases:
    - Tenant exists with ``disabled_at IS NOT NULL``: raise
      ``DisabledTenantError``. Do not modify the row. The caller drops
      the tuple.
    - Tenant exists with ``disabled_at IS NULL``: return the existing
      ``tenant_id``. No write.
    - Tenant does not exist: INSERT a new row with ``display_name`` equal
      to the slug (operator can rename later via the tenant-admin path).
      Emit a ``tenant.jit_created`` audit row in the same transaction so
      creation is recorded atomically. Return the new ``tenant_id``.

    ``ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING
    tenant_id`` handles the race where two concurrent first-sightings
    of the same slug would otherwise both try to INSERT — one wins, the
    other reads the same ``tenant_id`` via the no-op DO UPDATE.
    """
    pre_check = await session.execute(
        text("SELECT tenant_id, disabled_at FROM tenants WHERE slug = :slug"),
        {"slug": slug},
    )
    row = pre_check.first()
    if row is not None:
        tenant_id, disabled_at = row
        if disabled_at is not None:
            raise DisabledTenantError(slug)
        return tenant_id  # type: ignore[no-any-return]

    new_tenant_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    insert = await session.execute(
        text(
            "INSERT INTO tenants (tenant_id, slug, display_name, created_at, disabled_at) "
            "VALUES (:tenant_id, :slug, :display_name, :now, NULL) "
            "ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug "
            "RETURNING tenant_id"
        ),
        {
            "tenant_id": new_tenant_id,
            "slug": slug,
            "display_name": slug,
            "now": now,
        },
    )
    inserted_row = insert.first()
    if inserted_row is None:
        raise RuntimeError(
            f"upsert_entitlement_tenant: INSERT returned no row for slug={slug!r} "
            "— ON CONFLICT DO UPDATE should always return a row"
        )
    tenant_id = inserted_row[0]

    # Only emit the JIT-created audit when the INSERT actually created
    # the row (vs. the conflict path which returns the existing UUID).
    # Compare against the UUID we generated — if it matches, this row is new.
    if tenant_id == new_tenant_id:
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
                    f'"slug": "{slug}", '
                    f'"source": "entitlement"}}'
                ),
                "ts": now,
            },
        )
        _log.info(
            "entitlement_tenant_jit_created",
            extra={"tenant_id": str(tenant_id), "slug": slug},
        )

    return tenant_id  # type: ignore[no-any-return]


async def upsert_entitlement_actor(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    oidc_subject: str,
    display_name: str | None,
) -> uuid.UUID:
    """Find-or-create an actor row, returning the actor_id.

    On first sight: INSERT a row with the supplied ``display_name`` (or
    ``oidc_subject`` if ``display_name`` is None — display_name is NOT
    NULL in the schema). Emit ``actor.jit_created`` audit row in the
    same transaction.

    On re-sight: ``ON CONFLICT (tenant_id, oidc_subject) DO UPDATE SET
    display_name = EXCLUDED.display_name`` updates the display_name to
    the latest value (so a token whose ``name`` claim has been
    populated upstream propagates without a separate sync) and returns
    the existing ``actor_id``.

    The first-sight vs. re-sight discriminator is the comparison of the
    UUID we generated (``new_actor_id``) against the RETURNING value:
    if they match, the row is new and the audit row should be emitted.
    """
    new_actor_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    effective_display = display_name or oidc_subject

    result = await session.execute(
        text(
            "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
            "VALUES (:actor_id, :tenant_id, :oidc_subject, :display_name, :now) "
            "ON CONFLICT (tenant_id, oidc_subject) "
            "DO UPDATE SET display_name = EXCLUDED.display_name "
            "RETURNING actor_id"
        ),
        {
            "actor_id": new_actor_id,
            "tenant_id": tenant_id,
            "oidc_subject": oidc_subject,
            "display_name": effective_display,
            "now": now,
        },
    )
    row = result.first()
    if row is None:
        raise RuntimeError(
            f"upsert_entitlement_actor: INSERT returned no row for "
            f"oidc_subject={oidc_subject!r}, tenant_id={tenant_id} "
            "— ON CONFLICT DO UPDATE should always return a row"
        )
    actor_id: uuid.UUID = row[0]

    if actor_id == new_actor_id:
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
                    f'"source": "entitlement"}}'
                ),
                "ts": now,
            },
        )
        _log.info(
            "entitlement_actor_jit_created",
            extra={"actor_id": str(actor_id), "tenant_id": str(tenant_id)},
        )

    return actor_id


__all__ = [
    "DisabledTenantError",
    "upsert_entitlement_actor",
    "upsert_entitlement_tenant",
]
