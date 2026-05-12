"""Closure outbox table for the transitive-closure cache-refresh worker.

Revision ID: 0008_closure_outbox
Revises: 0007_phase6_graph_primitives
Create Date: 2026-05-10

Adds `closure_outbox` — an edge-oriented transactional outbox.
`embedding_outbox` cannot be reused for closure_refresh rows because its
`fact_id` column is NOT NULL with a FK to `facts`; edge mutations carry an
`edge_id`, not a `fact_id`.

The `closure_refresh` worker drains this table with FOR UPDATE SKIP LOCKED
and upserts into `closure_cache`.

downgrade() drops the table and its index.
"""

from __future__ import annotations

from alembic import op

revision = "0008_closure_outbox"
down_revision: str | None = "0007_phase6_graph_primitives"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_CLOSURE_OUTBOX_DDL = """
CREATE TABLE closure_outbox (
    outbox_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID        NOT NULL REFERENCES tenants(tenant_id),
    edge_id      UUID        NOT NULL,
    enqueued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts     INTEGER     NOT NULL DEFAULT 0,
    last_error   TEXT,
    last_attempt_at TIMESTAMPTZ
)
"""

_CLOSURE_OUTBOX_ENQUEUED_IDX = "CREATE INDEX idx_closure_outbox_enqueued ON closure_outbox (enqueued_at)"


def upgrade() -> None:
    op.execute(_CLOSURE_OUTBOX_DDL)
    op.execute(_CLOSURE_OUTBOX_ENQUEUED_IDX)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS closure_outbox CASCADE")
