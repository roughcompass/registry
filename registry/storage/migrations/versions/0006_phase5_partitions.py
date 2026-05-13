"""Partitioned shadow tables for audit_log, episodes, and embeddings.

Revision ID: 0006_phase5_partitions
Revises: 0005_phase4_rbac_oidc
Create Date: 2026-05-07

Creates three ``_new`` tables alongside the existing unpartitioned tables:

* ``audit_log_new``   — RANGE partitioned by ``ts``, 12 monthly forward partitions
* ``episodes_new``    — RANGE partitioned by ``ts``, 12 monthly forward partitions
* ``embeddings_new``  — HASH partitioned by ``tenant_id``, 8 buckets (modulus 8,
                        remainder 0–7)

Schema of each ``_new`` table is identical to its source table (same columns,
same constraints, same indexes) so that ``partition_migrate.py`` can copy rows
with ``INSERT INTO … SELECT *``.

This migration does NOT rename tables and does NOT copy rows.  Both operations
are the responsibility of ``scripts/partition_migrate.py``.

Statements are issued one-per-``op.execute`` (asyncpg single-statement
requirement).
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

from alembic import op

revision = "0006_phase5_partitions"
down_revision: str | None = "0005_phase4_rbac_oidc"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Fixed origin date for monthly partitions. The original code used
# datetime.date.today() which made the generated DDL non-deterministic — two
# environments running the migration in different calendar months produced
# different partition names and value ranges. Pinned to 2025-01 so the
# 24-month window covers 2025-01 through 2026-12, which encompasses the
# fixed-clock values used across the test suite (FakeClock typically yields
# 2026-01-01 through 2026-12) and gives operators a year of headroom on
# either side.
_PARTITION_START: datetime.date = datetime.date(2025, 1, 1)
_PARTITION_COUNT: int = 24


def _monthly_bounds(start: datetime.date, count: int) -> Iterator[tuple[str, str, str]]:
    """Yield (partition_suffix, from_iso, to_iso) for *count* consecutive months."""
    year, month = start.year, start.month
    for _ in range(count):
        from_d = datetime.date(year, month, 1)
        if month == 12:
            to_d = datetime.date(year + 1, 1, 1)
        else:
            to_d = datetime.date(year, month + 1, 1)
        suffix = f"{from_d.year:04d}_{from_d.month:02d}"
        yield suffix, from_d.isoformat(), to_d.isoformat()
        year, month = to_d.year, to_d.month


# ---------------------------------------------------------------------------
# audit_log_new — RANGE by ts
# ---------------------------------------------------------------------------

_AUDIT_LOG_NEW_DDL = """
CREATE TABLE audit_log_new (
    audit_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL,
    actor_id     UUID,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    UUID NOT NULL,
    before_jsonb JSONB,
    after_jsonb  JSONB,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id   TEXT,
    error_code   TEXT,
    PRIMARY KEY (audit_id, ts)
) PARTITION BY RANGE (ts)
"""

_AUDIT_LOG_NEW_INDEXES: list[str] = [
    "CREATE INDEX idx_audit_new_tenant_ts ON audit_log_new (tenant_id, ts DESC)",
    "CREATE INDEX idx_audit_new_target    ON audit_log_new (tenant_id, target_type, target_id, ts DESC)",
    "CREATE INDEX idx_audit_new_actor     ON audit_log_new (tenant_id, actor_id, ts DESC)",
]

# ---------------------------------------------------------------------------
# episodes_new — RANGE by ts
# ---------------------------------------------------------------------------

_EPISODES_NEW_DDL = """
CREATE TABLE episodes_new (
    episode_id      UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    episode_type    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    actor_id        UUID,
    content_summary TEXT,
    ts              TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, ts)
) PARTITION BY RANGE (ts)
"""

_EPISODES_NEW_INDEXES: list[str] = [
    "CREATE INDEX idx_episodes_new_tenant_ts ON episodes_new (tenant_id, ts DESC)",
    "CREATE UNIQUE INDEX idx_episodes_new_source ON episodes_new (tenant_id, source_id, ts)",
]

# ---------------------------------------------------------------------------
# embeddings_new — HASH by tenant_id, 8 buckets
# ---------------------------------------------------------------------------

_EMBEDDINGS_NEW_DDL = """
CREATE TABLE embeddings_new (
    embedding_id  UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,
    claim_type    TEXT NOT NULL,
    claim_id      UUID NOT NULL,
    chunk_index   INTEGER NOT NULL DEFAULT 0,
    model_id      TEXT NOT NULL DEFAULT 'all-MiniLM-L6-v2',
    vector        VECTOR(384) NOT NULL,
    text_chunk    TEXT NOT NULL,
    ts_fact       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ts_vector     TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text_chunk)) STORED,
    PRIMARY KEY (embedding_id, tenant_id)
) PARTITION BY HASH (tenant_id)
"""

_EMBEDDINGS_NEW_INDEXES: list[str] = [
    "CREATE INDEX idx_embed_new_claim ON embeddings_new (tenant_id, claim_type, claim_id)",
    "CREATE INDEX idx_embed_new_model ON embeddings_new (model_id)",
    "CREATE INDEX idx_embed_new_fts   ON embeddings_new USING GIN (ts_vector)",
]

# Per-partition HNSW indexes.
# Created on each child partition *after* the Alembic migration runs but
# *before* partition_migrate.py copies data and renames tables.
# Building HNSW before row copy is safe (empty partitions, zero cost);
# the index is then populated incrementally as rows are inserted during copy.
#
# Each child partition p{0..7} gets its own HNSW index on the vector column:
#   idx_embed_new_hnsw_p{n}
# Parameters: m=16, ef_construction=64 (good balance of build cost and recall).
#
# After the rename (partition_migrate.py Step 4), these indexes are attached
# to the live embeddings_p{n} partitions via the renamed parent embeddings.
#
# Operator verification (cannot run in CI without Docker):
#   EXPLAIN ANALYZE SELECT * FROM embeddings WHERE tenant_id = '<uuid>' LIMIT 10;
#   -- Expected: "Append -> Index Scan using idx_embed_new_hnsw_p<n>"
#   -- Only 1 of 8 partitions scanned (partition pruning on tenant_id hash).
_EMBEDDINGS_HNSW_INDEX_TEMPLATE = (
    "CREATE INDEX idx_embed_new_hnsw_p{n} "
    "ON embeddings_new_p{n} "
    "USING hnsw (vector vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)"
)

# ---------------------------------------------------------------------------
# Downgrade constants
# ---------------------------------------------------------------------------

_DROP_EMBEDDINGS_NEW = "DROP TABLE IF EXISTS embeddings_new CASCADE"
_DROP_EPISODES_NEW = "DROP TABLE IF EXISTS episodes_new CASCADE"
_DROP_AUDIT_LOG_NEW = "DROP TABLE IF EXISTS audit_log_new CASCADE"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # --- audit_log_new ---
    op.execute(_AUDIT_LOG_NEW_DDL)
    for idx_sql in _AUDIT_LOG_NEW_INDEXES:
        op.execute(idx_sql)

    start = _PARTITION_START
    for suffix, from_iso, to_iso in _monthly_bounds(start, _PARTITION_COUNT):
        op.execute(
            f"CREATE TABLE audit_log_new_{suffix} "
            f"PARTITION OF audit_log_new "
            f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
        )

    # --- episodes_new ---
    op.execute(_EPISODES_NEW_DDL)
    for idx_sql in _EPISODES_NEW_INDEXES:
        op.execute(idx_sql)

    for suffix, from_iso, to_iso in _monthly_bounds(start, _PARTITION_COUNT):
        op.execute(
            f"CREATE TABLE episodes_new_{suffix} "
            f"PARTITION OF episodes_new "
            f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
        )

    # --- embeddings_new ---
    op.execute(_EMBEDDINGS_NEW_DDL)
    for idx_sql in _EMBEDDINGS_NEW_INDEXES:
        op.execute(idx_sql)

    for remainder in range(8):
        op.execute(
            f"CREATE TABLE embeddings_new_p{remainder} "
            f"PARTITION OF embeddings_new "
            f"FOR VALUES WITH (modulus 8, remainder {remainder})"
        )

    # Per-partition HNSW indexes.
    # Built on empty partitions immediately after creation.  Data will be
    # copied by partition_migrate.py later; building before copy avoids the
    # full-table HNSW rebuild cost at rename time.
    for remainder in range(8):
        op.execute(_EMBEDDINGS_HNSW_INDEX_TEMPLATE.format(n=remainder))


def downgrade() -> None:
    # NOTE: cutover is not reversible via Alembic downgrade once
    # partition_migrate.py has renamed the tables.  If a cutover has already
    # been performed, restore from ``audit_log_archive`` (and the equivalent
    # ``episodes_archive``) manually before running this downgrade.
    op.execute(_DROP_EMBEDDINGS_NEW)
    op.execute(_DROP_EPISODES_NEW)
    op.execute(_DROP_AUDIT_LOG_NEW)
