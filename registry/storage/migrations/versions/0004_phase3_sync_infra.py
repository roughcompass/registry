"""Sync infrastructure — sync_sources, sync_runs, webhook_deliveries; actors extension.

Revision ID: 0004_phase3_sync_infra
Revises: 0003_phase2_embeddings_outbox
Create Date: 2026-05-06

Creates the sync ingestion infrastructure tables:

* `sync_sources`        — one row per configured connector source
* `sync_runs`           — one row per execution of a source ingestion
* `webhook_deliveries`  — idempotency log for inbound webhook payloads

Also extends `actors`:

* `actor_kind TEXT NOT NULL DEFAULT 'human'`
* Partial unique index `uq_actors_tenant_sync_type` on
  `(tenant_id, display_name) WHERE actor_kind = 'sync_worker'` so that
  sync-worker actors have a unique (tenant, display_name) identity while
  human actors may share display names across identity providers.

Activates the deferred FK on `facts.sync_run_id → sync_runs.sync_run_id`
(the target table now exists).

`facts.is_authoritative_superseded` and `facts.sync_run_id` both exist
from the baseline migration; no ALTER TABLE is issued for them.

`source_type` vocab validation is enforced by the vocabulary service at
write time (not via DB CHECK); this matches the pattern for `entity_type`
and other vocab-bound columns.

`status` and `trigger` on `sync_runs` carry CHECK constraints:
  status  IN ('running','done','partial','failed')
  trigger IN ('scheduled','webhook','manual')

Statements are issued one-per-`op.execute` (asyncpg single-statement
requirement).
"""

from __future__ import annotations

from alembic import op

revision = "0004_phase3_sync_infra"
down_revision: str | None = "0003_phase2_embeddings_outbox"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# DDL — sync_sources
# ---------------------------------------------------------------------------

_SYNC_SOURCES_DDL = """
CREATE TABLE sync_sources (
    source_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    source_type     TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',
    credentials_ref TEXT,
    schedule        TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      UUID REFERENCES actors(actor_id)
)
"""

_SYNC_SOURCES_TENANT_IDX = "CREATE INDEX idx_sync_sources_tenant ON sync_sources (tenant_id)"

_SYNC_SOURCES_TYPE_IDX = "CREATE INDEX idx_sync_sources_type ON sync_sources (tenant_id, source_type)"

# ---------------------------------------------------------------------------
# DDL — sync_runs
# ---------------------------------------------------------------------------

_SYNC_RUNS_DDL = """
CREATE TABLE sync_runs (
    sync_run_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(tenant_id),
    source_id      UUID NOT NULL REFERENCES sync_sources(source_id),
    status         TEXT NOT NULL CHECK (status IN ('running','done','partial','failed')),
    trigger        TEXT NOT NULL CHECK (trigger IN ('scheduled','webhook','manual')),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    duration_s     INTEGER,
    artifact_count INTEGER,
    error_summary  TEXT
)
"""

_SYNC_RUNS_SOURCE_IDX = "CREATE INDEX idx_sync_runs_source ON sync_runs (tenant_id, source_id, started_at DESC)"

_SYNC_RUNS_STATUS_IDX = (
    "CREATE INDEX idx_sync_runs_status ON sync_runs (tenant_id, status) " "WHERE status IN ('running','partial')"
)

# ---------------------------------------------------------------------------
# DDL — webhook_deliveries
# ---------------------------------------------------------------------------

_WEBHOOK_DELIVERIES_DDL = """
CREATE TABLE webhook_deliveries (
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    delivery_id  TEXT NOT NULL,
    source_id    UUID NOT NULL REFERENCES sync_sources(source_id),
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, delivery_id)
)
"""

_WEBHOOK_DELIVERIES_SOURCE_IDX = (
    "CREATE INDEX idx_webhook_deliveries_source " "ON webhook_deliveries (tenant_id, source_id, received_at DESC)"
)

# ---------------------------------------------------------------------------
# DDL — actors extension: actor_kind + partial unique index
# ---------------------------------------------------------------------------

_ACTORS_ADD_KIND = "ALTER TABLE actors " "ADD COLUMN actor_kind TEXT NOT NULL DEFAULT 'human'"

# Sync-worker actors share a (tenant_id, display_name) namespace that must be
# unique per tenant so each connector type has a stable, non-ambiguous identity.
# Human actors are excluded from this constraint to allow display-name reuse
# across identity providers.
_ACTORS_PARTIAL_UNIQ = (
    "CREATE UNIQUE INDEX uq_actors_tenant_sync_type "
    "ON actors (tenant_id, display_name) "
    "WHERE actor_kind = 'sync_worker'"
)

# ---------------------------------------------------------------------------
# DDL — activate FK on facts.sync_run_id → sync_runs(sync_run_id)
#
# The column exists from 0001_phase0_baseline with no FK.  We add the
# constraint now that the target table is present.
# ---------------------------------------------------------------------------

_FACTS_SYNC_RUN_FK = (
    "ALTER TABLE facts "
    "ADD CONSTRAINT fk_facts_sync_run "
    "FOREIGN KEY (sync_run_id) REFERENCES sync_runs(sync_run_id)"
)

# ---------------------------------------------------------------------------
# Downgrade constants (reverse order)
# ---------------------------------------------------------------------------

_DROP_FACTS_SYNC_RUN_FK = "ALTER TABLE facts DROP CONSTRAINT IF EXISTS fk_facts_sync_run"

_DROP_ACTORS_PARTIAL_UNIQ = "DROP INDEX IF EXISTS uq_actors_tenant_sync_type"

_DROP_ACTORS_KIND = "ALTER TABLE actors DROP COLUMN IF EXISTS actor_kind"

_DROP_WEBHOOK_DELIVERIES = "DROP TABLE IF EXISTS webhook_deliveries CASCADE"
_DROP_SYNC_RUNS = "DROP TABLE IF EXISTS sync_runs CASCADE"
_DROP_SYNC_SOURCES = "DROP TABLE IF EXISTS sync_sources CASCADE"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # sync_sources must precede sync_runs and webhook_deliveries (FK target).
    op.execute(_SYNC_SOURCES_DDL)
    op.execute(_SYNC_SOURCES_TENANT_IDX)
    op.execute(_SYNC_SOURCES_TYPE_IDX)

    op.execute(_SYNC_RUNS_DDL)
    op.execute(_SYNC_RUNS_SOURCE_IDX)
    op.execute(_SYNC_RUNS_STATUS_IDX)

    op.execute(_WEBHOOK_DELIVERIES_DDL)
    op.execute(_WEBHOOK_DELIVERIES_SOURCE_IDX)

    # Extend actors table — column first, then index.
    op.execute(_ACTORS_ADD_KIND)
    op.execute(_ACTORS_PARTIAL_UNIQ)

    # Activate the FK on facts.sync_run_id now that sync_runs exists.
    op.execute(_FACTS_SYNC_RUN_FK)


def downgrade() -> None:
    # Remove FK before dropping its target table.
    op.execute(_DROP_FACTS_SYNC_RUN_FK)

    op.execute(_DROP_ACTORS_PARTIAL_UNIQ)
    op.execute(_DROP_ACTORS_KIND)

    op.execute(_DROP_WEBHOOK_DELIVERIES)
    op.execute(_DROP_SYNC_RUNS)
    op.execute(_DROP_SYNC_SOURCES)
