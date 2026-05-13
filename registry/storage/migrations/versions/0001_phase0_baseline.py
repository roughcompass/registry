"""Baseline schema — tenants, actors, api_tokens, vocabulary_values, entities,
attributes, facts, edges, episodes, provenance, audit_log.

Revision ID: 0001_phase0_baseline
Revises:
Create Date: 2026-05-06

Two cross-cutting decisions are wired here:

* `audit_log` is partitioned by `RANGE (ts)` from creation, with 12 monthly
  child partitions pre-created starting at the month of migration. The
  partition-cutover migration extends this set rather than converting the
  table (audit retention is handled by detaching old partitions, not
  by deleting rows).

* `facts.sync_run_id` is a nullable UUID column with no FK constraint
  here — the `sync_runs` table is created later by the sync-infra migration,
  which also activates the FK.

A default tenant (UUID 00000000-0000-0000-0000-000000000000, slug
'default') is seeded so the controlled-vocabulary table can carry its
29 system rows. New tenants receive their own vocabulary copy through
service code (admin endpoints).

Statements are issued one-per-`op.execute` because asyncpg requires
single statements at the prepare layer; multi-statement scripts fail.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_phase0_baseline"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


DEFAULT_TENANT_UUID = "00000000-0000-0000-0000-000000000000"


def _monthly_partition_bounds(start: datetime.date, count: int) -> Iterator[tuple[str, str, str]]:
    """Yield (partition_name, from_iso, to_iso) for monthly partitions."""
    year, month = start.year, start.month
    for _ in range(count):
        from_d = datetime.date(year, month, 1)
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        to_d = datetime.date(next_year, next_month, 1)
        partition_name = f"audit_log_{from_d.year:04d}_{from_d.month:02d}"
        yield partition_name, from_d.isoformat(), to_d.isoformat()
        year, month = next_year, next_month


_TENANTS_DDL = """
CREATE TABLE tenants (
    tenant_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
)
"""

_ACTORS_DDL = """
CREATE TABLE actors (
    actor_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    display_name TEXT NOT NULL,
    email        TEXT,
    oidc_subject TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_API_TOKENS_DDL = """
CREATE TABLE api_tokens (
    token_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id    UUID NOT NULL REFERENCES actors(actor_id),
    token_hash  TEXT NOT NULL UNIQUE,
    roles       TEXT[] NOT NULL DEFAULT '{}',
    description TEXT,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
)
"""

_VOCAB_DDL = """
CREATE TABLE vocabulary_values (
    vocab_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    kind          TEXT NOT NULL,
    value         TEXT NOT NULL,
    is_system     BOOLEAN NOT NULL DEFAULT FALSE,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_ENTITIES_DDL = """
CREATE TABLE entities (
    entity_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_type TEXT NOT NULL,
    name        TEXT NOT NULL,
    external_id TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  UUID REFERENCES actors(actor_id)
)
"""

_ATTRIBUTES_DDL = """
CREATE TABLE attributes (
    attr_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_id        UUID NOT NULL REFERENCES entities(entity_id),
    key              TEXT NOT NULL,
    value            JSONB NOT NULL,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

# sync_run_id is a nullable UUID with no FK here; the sync-infra migration adds the FK.
_FACTS_DDL = """
CREATE TABLE facts (
    fact_id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_id                   UUID NOT NULL REFERENCES entities(entity_id),
    category                    TEXT NOT NULL,
    body                        TEXT NOT NULL,
    is_authoritative            BOOLEAN NOT NULL DEFAULT TRUE,
    is_authoritative_superseded BOOLEAN NOT NULL DEFAULT FALSE,
    sync_run_id                 UUID,
    t_valid_from                TIMESTAMPTZ NOT NULL,
    t_valid_to                  TIMESTAMPTZ,
    t_ingested_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at            TIMESTAMPTZ,
    created_by                  UUID REFERENCES actors(actor_id)
)
"""

_EDGES_DDL = """
CREATE TABLE edges (
    edge_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    src_entity_id    UUID NOT NULL REFERENCES entities(entity_id),
    rel              TEXT NOT NULL,
    dst_entity_id    UUID NOT NULL REFERENCES entities(entity_id),
    properties       JSONB,
    is_authoritative BOOLEAN NOT NULL DEFAULT TRUE,
    sync_run_id      UUID,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

_EPISODES_DDL = """
CREATE TABLE episodes (
    episode_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    episode_type    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    actor_id        UUID REFERENCES actors(actor_id),
    content_summary TEXT,
    ts              TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_PROVENANCE_DDL = """
CREATE TABLE provenance (
    prov_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_type  TEXT NOT NULL,
    claim_id    UUID NOT NULL,
    episode_id  UUID NOT NULL REFERENCES episodes(episode_id),
    source_url  TEXT,
    commit_sha  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# audit_log is partitioned by RANGE(ts) from creation. The PK includes ts
# because Postgres requires the partition key to be part of any PK on a
# partitioned table.
_AUDIT_LOG_DDL = """
CREATE TABLE audit_log (
    audit_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id     UUID REFERENCES actors(actor_id),
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


_INDEXES: list[str] = [
    "CREATE INDEX idx_actors_tenant ON actors (tenant_id)",
    "CREATE UNIQUE INDEX idx_actors_oidc ON actors (tenant_id, oidc_subject) WHERE oidc_subject IS NOT NULL",
    "CREATE INDEX idx_tokens_tenant ON api_tokens (tenant_id)",
    "CREATE INDEX idx_tokens_hash   ON api_tokens (token_hash)",
    "CREATE UNIQUE INDEX idx_vocab_kind_value ON vocabulary_values (tenant_id, kind, value)",
    "CREATE INDEX idx_vocab_tenant_kind ON vocabulary_values (tenant_id, kind)",
    "CREATE INDEX idx_entities_tenant_type ON entities (tenant_id, entity_type)",
    "CREATE INDEX idx_entities_tenant_name ON entities (tenant_id, lower(name))",
    "CREATE UNIQUE INDEX idx_entities_external_id ON entities (tenant_id, entity_type, external_id) "
    "WHERE external_id IS NOT NULL",
    "CREATE INDEX idx_attr_entity_current ON attributes (tenant_id, entity_id, key) " "WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_attr_entity_temporal ON attributes (tenant_id, entity_id, t_valid_from, t_valid_to)",
    "CREATE INDEX idx_facts_entity_current ON facts (tenant_id, entity_id, category) " "WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_facts_entity_temporal ON facts (tenant_id, entity_id, t_valid_from, t_valid_to)",
    "CREATE INDEX idx_facts_sync_run ON facts (sync_run_id) WHERE sync_run_id IS NOT NULL",
    "CREATE INDEX idx_edges_src_current ON edges (tenant_id, src_entity_id, rel) " "WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_edges_dst_current ON edges (tenant_id, dst_entity_id, rel) " "WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_edges_temporal ON edges (tenant_id, src_entity_id, t_valid_from, t_valid_to)",
    "CREATE UNIQUE INDEX idx_episodes_source ON episodes (tenant_id, source_id)",
    "CREATE INDEX idx_episodes_tenant_ts ON episodes (tenant_id, ts DESC)",
    "CREATE INDEX idx_prov_claim ON provenance (tenant_id, claim_type, claim_id)",
    "CREATE INDEX idx_prov_episode ON provenance (tenant_id, episode_id)",
    "CREATE INDEX idx_audit_tenant_ts ON audit_log (tenant_id, ts DESC)",
    "CREATE INDEX idx_audit_target    ON audit_log (tenant_id, target_type, target_id, ts DESC)",
    "CREATE INDEX idx_audit_actor     ON audit_log (tenant_id, actor_id, ts DESC)",
]


_SEED_ROWS: list[tuple[str, str]] = [
    ("entity_type", "capability"),
    ("entity_type", "concept"),
    ("entity_type", "operation"),
    ("entity_type", "person"),
    ("entity_type", "system"),
    ("fact_category", "overview"),
    ("fact_category", "concept_glossary"),
    ("fact_category", "limits"),
    ("fact_category", "security_model"),
    ("fact_category", "pricing"),
    ("fact_category", "release_note"),
    ("fact_category", "faq"),
    ("fact_category", "adr"),
    ("fact_category", "rfc"),
    ("fact_category", "dev_doc"),
    ("fact_category", "api_doc"),
    ("fact_category", "catalog_entry"),
    ("edge_rel", "concept_of"),
    ("edge_rel", "operation_of"),
    ("edge_rel", "depends_on"),
    ("edge_rel", "integrates_with"),
    ("edge_rel", "event_source"),
    ("edge_rel", "replaced_by"),
    ("edge_rel", "instance_of"),
    ("lifecycle_state", "alpha"),
    ("lifecycle_state", "beta"),
    ("lifecycle_state", "ga"),
    ("lifecycle_state", "deprecated"),
    ("lifecycle_state", "retired"),
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    for ddl in (
        _TENANTS_DDL,
        _ACTORS_DDL,
        _API_TOKENS_DDL,
        _VOCAB_DDL,
        _ENTITIES_DDL,
        _ATTRIBUTES_DDL,
        _FACTS_DDL,
        _EDGES_DDL,
        _EPISODES_DDL,
        _PROVENANCE_DDL,
        _AUDIT_LOG_DDL,
    ):
        op.execute(ddl)

    for index_sql in _INDEXES:
        op.execute(index_sql)

    # Pin the partition origin so the generated DDL is deterministic across
    # environments (same fix shape as DEF-T21 for migration 0006). The
    # 24-month window covers 2025-01 through 2026-12, encompassing the
    # fixed-clock dates used across the test suite.
    start = datetime.date(2025, 1, 1)
    for partition_name, from_iso, to_iso in _monthly_partition_bounds(start, 24):
        op.execute(
            f"CREATE TABLE {partition_name} PARTITION OF audit_log " f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
        )

    op.execute(
        f"INSERT INTO tenants (tenant_id, slug, display_name) "
        f"VALUES ('{DEFAULT_TENANT_UUID}', 'default', 'Default Tenant')"
    )

    for kind, value in _SEED_ROWS:
        op.execute(
            f"INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
            f"VALUES ('{DEFAULT_TENANT_UUID}', '{kind}', '{value}', TRUE)"
        )


def downgrade() -> None:
    # Drop tables in reverse dependency order.
    for table in (
        "audit_log",
        "provenance",
        "episodes",
        "edges",
        "facts",
        "attributes",
        "entities",
        "vocabulary_values",
        "api_tokens",
        "actors",
        "tenants",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    # Extensions are left in place — they may be shared with other databases.
