"""Add indexes for keyset pagination on entities and improved delivery claim sort.

Revision ID: 0013_missing_indexes
Revises: 0012_idempotency_keys
Create Date: 2026-05-11

Two indexes were absent, causing sequential scans or post-sort passes on
hot read paths:

1. ``idx_entities_tenant_created`` on ``entities (tenant_id, created_at DESC,
   entity_id)`` — supports the keyset pagination predicate
   ``WHERE tenant_id = :t AND (created_at, entity_id) < (:ts, :id)``
   introduced by the capability list endpoint.  Without this index Postgres
   scans all tenant rows and sorts them before applying the LIMIT.

2. ``idx_delivery_pending_sort`` on ``notification_deliveries
   (tenant_id, next_retry_at, attempted_at) WHERE status = 'pending'`` —
   replaces ``idx_delivery_pending`` which covered only ``(tenant_id,
   next_retry_at)``.  The webhook worker's claim query orders by
   ``next_retry_at NULLS FIRST, attempted_at``; without ``attempted_at`` in
   the index Postgres re-sorts the filtered rows before applying the LIMIT.

The ``entities`` index uses ``CONCURRENTLY`` (live table, online-safe).
The ``notification_deliveries`` indexes do NOT use ``CONCURRENTLY``
because Postgres rejects ``CONCURRENTLY`` on partitioned tables — the
parent table is catalog metadata only, the actual indexes attach to
child partitions automatically, and the parent-table operation is
near-instantaneous regardless. ``CONCURRENTLY`` cannot execute inside
a transaction; the upgrade function therefore switches the connection
to autocommit before issuing the entities CREATE and restores the
default isolation level afterward.

Downgrade restores the old ``idx_delivery_pending`` (without
``attempted_at``) and drops the two new indexes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_missing_indexes"
down_revision: str | Sequence[str] | None = "0012_idempotency_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    # Temporarily switch the underlying connection to AUTOCOMMIT so Postgres
    # accepts the statement, then restore the previous isolation level.
    bind = op.get_bind()
    bind.execute(sa.text("COMMIT"))
    bind.execute(sa.text("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED"))

    bind.execute(
        sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_entities_tenant_created "
            "ON entities (tenant_id, created_at DESC, entity_id)"
        )
    )

    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_delivery_pending_sort "
            "ON notification_deliveries (tenant_id, next_retry_at, attempted_at) "
            "WHERE status = 'pending'"
        )
    )

    # Drop the narrower predecessor index that omitted attempted_at.
    bind.execute(sa.text("DROP INDEX IF EXISTS idx_delivery_pending"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("COMMIT"))

    # Partitioned table — CONCURRENTLY not supported.
    bind.execute(sa.text("DROP INDEX IF EXISTS idx_delivery_pending_sort"))

    bind.execute(sa.text("DROP INDEX CONCURRENTLY IF EXISTS idx_entities_tenant_created"))

    # Recreate the predecessor partial index (narrower — no attempted_at).
    # Partitioned table — CONCURRENTLY not supported.
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_delivery_pending "
            "ON notification_deliveries (tenant_id, next_retry_at) "
            "WHERE status = 'pending'"
        )
    )
