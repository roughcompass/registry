"""Embeddings, embedding_outbox, embedding_outbox_failed + FTS on facts.

Revision ID: 0003_phase2_embeddings_outbox
Revises: 0002_phase1_schema_registry
Create Date: 2026-05-06

Creates the vector store tables required by the embedding pipeline:

* `embeddings`              — one row per embedded chunk; VECTOR(384) column
* `embedding_outbox`        — transactional outbox for async embedding drain;
                              written in the same transaction as the source fact
                              so a rollback removes both atomically
* `embedding_outbox_failed` — dead-letter for rows that exceeded max_attempts

Also adds `ts_vector TSVECTOR GENERATED ALWAYS` to `facts` (body → English
lexemes) and a GIN index so the hybrid-retrieval lexical arm can run FTS
without a separate pass.

pgvector extension is created here if it does not already exist.

Statements are issued one-per-`op.execute` (asyncpg single-statement
requirement).
"""

from __future__ import annotations

from alembic import op

revision = "0003_phase2_embeddings_outbox"
down_revision: str | None = "0002_phase1_schema_registry"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# DDL constants
# ---------------------------------------------------------------------------

_EXT_VECTOR = "CREATE EXTENSION IF NOT EXISTS vector"

_EMBEDDINGS_DDL = """
CREATE TABLE embeddings (
    embedding_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_type    TEXT NOT NULL,
    claim_id      UUID NOT NULL REFERENCES facts(fact_id),
    chunk_index   INTEGER NOT NULL DEFAULT 0,
    model_id      TEXT NOT NULL DEFAULT 'all-MiniLM-L6-v2',
    vector        VECTOR(384) NOT NULL,
    text_chunk    TEXT NOT NULL,
    ts_fact       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_EMBEDDINGS_CLAIM_IDX = "CREATE INDEX idx_embed_claim ON embeddings (tenant_id, claim_type, claim_id)"

_EMBEDDINGS_MODEL_IDX = "CREATE INDEX idx_embed_model ON embeddings (model_id)"

# HNSW parameters: m=16, ef_construction=64.  These control index build cost
# vs. query recall quality; raise ef_construction for higher recall at the
# cost of slower index builds.
_EMBEDDINGS_HNSW_IDX = (
    "CREATE INDEX embeddings_hnsw ON embeddings "
    "USING hnsw (vector vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)"
)

# tsvector generated column on embeddings supports hybrid co-query (vector ANN
# + lexical FTS in one index scan).  The same column on facts enables lexical
# search on raw fact bodies without joining embeddings.
_EMBEDDINGS_TSVECTOR_COL = (
    "ALTER TABLE embeddings "
    "ADD COLUMN ts_vector TSVECTOR "
    "GENERATED ALWAYS AS (to_tsvector('english', text_chunk)) STORED"
)

_EMBEDDINGS_FTS_IDX = "CREATE INDEX idx_embed_fts ON embeddings USING GIN (ts_vector)"

_OUTBOX_DDL = """
CREATE TABLE embedding_outbox (
    outbox_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_type      TEXT NOT NULL,
    fact_id         UUID NOT NULL REFERENCES facts(fact_id),
    text_to_embed   TEXT NOT NULL,
    chunk_plan      JSONB NOT NULL,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ
)
"""

_OUTBOX_PENDING_IDX = "CREATE INDEX idx_outbox_pending ON embedding_outbox (enqueued_at) " "WHERE last_error IS NULL"

_OUTBOX_TENANT_IDX = "CREATE INDEX idx_outbox_tenant ON embedding_outbox (tenant_id)"

_OUTBOX_FAILED_DDL = """
CREATE TABLE embedding_outbox_failed (
    failed_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_type    TEXT NOT NULL,
    fact_id       UUID NOT NULL REFERENCES facts(fact_id),
    text_to_embed TEXT NOT NULL,
    chunk_plan    JSONB NOT NULL,
    failed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_text    TEXT NOT NULL,
    attempts      INTEGER NOT NULL
)
"""

_OUTBOX_FAILED_TENANT_IDX = (
    "CREATE INDEX idx_outbox_failed_tenant " "ON embedding_outbox_failed (tenant_id, failed_at DESC)"
)

# tsvector GENERATED ALWAYS on facts.body → ts_vector (lexical search on raw fact bodies).
_FACTS_TSVECTOR_COL = (
    "ALTER TABLE facts " "ADD COLUMN ts_vector TSVECTOR " "GENERATED ALWAYS AS (to_tsvector('english', body)) STORED"
)

_FACTS_FTS_IDX = "CREATE INDEX idx_facts_fts ON facts USING GIN (ts_vector)"


# ---------------------------------------------------------------------------
# Downgrade constants
# ---------------------------------------------------------------------------

_DROP_FACTS_FTS_IDX = "DROP INDEX IF EXISTS idx_facts_fts"
_DROP_FACTS_TSVECTOR_COL = "ALTER TABLE facts DROP COLUMN IF EXISTS ts_vector"
_DROP_OUTBOX_FAILED = "DROP TABLE IF EXISTS embedding_outbox_failed CASCADE"
_DROP_OUTBOX = "DROP TABLE IF EXISTS embedding_outbox CASCADE"
_DROP_EMBEDDINGS = "DROP TABLE IF EXISTS embeddings CASCADE"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # pgvector extension — idempotent; must precede VECTOR type usage.
    op.execute(_EXT_VECTOR)

    # embeddings table + standard indexes
    op.execute(_EMBEDDINGS_DDL)
    op.execute(_EMBEDDINGS_CLAIM_IDX)
    op.execute(_EMBEDDINGS_MODEL_IDX)

    # HNSW index (requires pgvector ≥0.5.0)
    op.execute(_EMBEDDINGS_HNSW_IDX)

    # tsvector generated column on embeddings (hybrid FTS co-query)
    op.execute(_EMBEDDINGS_TSVECTOR_COL)
    op.execute(_EMBEDDINGS_FTS_IDX)

    # outbox tables
    op.execute(_OUTBOX_DDL)
    op.execute(_OUTBOX_PENDING_IDX)
    op.execute(_OUTBOX_TENANT_IDX)
    op.execute(_OUTBOX_FAILED_DDL)
    op.execute(_OUTBOX_FAILED_TENANT_IDX)

    # tsvector on facts (lexical search on raw fact bodies)
    op.execute(_FACTS_TSVECTOR_COL)
    op.execute(_FACTS_FTS_IDX)


def downgrade() -> None:
    op.execute(_DROP_FACTS_FTS_IDX)
    op.execute(_DROP_FACTS_TSVECTOR_COL)
    op.execute(_DROP_OUTBOX_FAILED)
    op.execute(_DROP_OUTBOX)
    op.execute(_DROP_EMBEDDINGS)
    # Extension is not dropped — other tenants on the same cluster may use it.
