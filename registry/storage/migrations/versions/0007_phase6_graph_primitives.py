"""Graph primitives — 7 new tables + vocab & system PII seeds.

Revision ID: 0007_phase6_graph_primitives
Revises: 0006_phase5_partitions
Create Date: 2026-05-10

Adds tables for graph traversal, external-ID mapping, and PII detection:

* ``edge_property_schemas``  — bi-temporal edge-property schema registry
* ``closure_cache``          — materialized transitive-closure cache
* ``entity_external_ids``    — external-ID mapping; hard-delete only, no bi-temporal
* ``external_systems``       — registry of upstream systems
* ``pii_patterns``           — PII pattern store; built-in system rows seeded here
* ``pii_field_policies``     — per-field policy overrides
* ``pii_detection_log``      — always-on detection log, partitioned monthly

Vocabulary seeds (idempotent INSERT ... ON CONFLICT DO NOTHING):
  edge_rel   : requires, conflicts_with, composes, provides_to
  entity_type: integration
  pii_category: email, phone, ssn, aws_access_key, aws_secret_key, jwt_token, credit_card

System PII pattern rows — seven built-in patterns seeded with is_system=TRUE against the
default tenant.  Each pattern has a corresponding Python module for the implementation.
Entropy-based patterns (aws_secret_key) use regex='__entropy__' + detector_module to
signal that the Python module is the authoritative implementation.

downgrade() drops all new tables and removes the seeded vocab/system-PII rows.

Statements are issued one-per-``op.execute`` (asyncpg single-statement requirement).
"""

from __future__ import annotations

import datetime

from alembic import op

revision = "0007_phase6_graph_primitives"
down_revision: str | None = "0006_phase5_partitions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TENANT_UUID = "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# DDL — edge_property_schemas
# ---------------------------------------------------------------------------

_EDGE_PROPERTY_SCHEMAS_DDL = """
CREATE TABLE edge_property_schemas (
    schema_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    edge_rel         TEXT NOT NULL,
    json_schema      JSONB NOT NULL,
    is_advisory      BOOLEAN NOT NULL DEFAULT TRUE,
    advisory_until   TIMESTAMPTZ,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

_EDGE_PROPERTY_SCHEMAS_IDX = (
    "CREATE INDEX idx_epschema_tenant_rel ON edge_property_schemas (tenant_id, edge_rel) "
    "WHERE t_invalidated_at IS NULL"
)

# ---------------------------------------------------------------------------
# DDL — closure_cache
# ---------------------------------------------------------------------------

_CLOSURE_CACHE_DDL = """
CREATE TABLE closure_cache (
    cache_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    root_entity_id   UUID NOT NULL REFERENCES entities(entity_id),
    member_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    direction        TEXT NOT NULL,
    depth            INTEGER NOT NULL,
    edge_path        UUID[] NOT NULL,
    edge_rels        TEXT[] NOT NULL,
    refreshed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_direction CHECK (direction IN ('forward','reverse'))
)
"""

_CLOSURE_CACHE_UNIQUE_IDX = (
    "CREATE UNIQUE INDEX idx_closure_unique ON closure_cache "
    "(tenant_id, root_entity_id, member_entity_id, direction)"
)

_CLOSURE_CACHE_ROOT_IDX = "CREATE INDEX idx_closure_root ON closure_cache (tenant_id, root_entity_id, direction)"

_CLOSURE_CACHE_MEMBER_IDX = "CREATE INDEX idx_closure_member ON closure_cache (tenant_id, member_entity_id, direction)"

_CLOSURE_CACHE_REFRESHED_IDX = "CREATE INDEX idx_closure_refreshed ON closure_cache (refreshed_at)"

# ---------------------------------------------------------------------------
# DDL — external_systems — must be created before entity_external_ids
# ---------------------------------------------------------------------------

_EXTERNAL_SYSTEMS_DDL = """
CREATE TABLE external_systems (
    slug         TEXT NOT NULL,
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    display_name TEXT NOT NULL,
    url_template TEXT,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, slug)
)
"""

# ---------------------------------------------------------------------------
# DDL — entity_external_ids — hard-delete only, no t_invalidated_at
# ---------------------------------------------------------------------------

_ENTITY_EXTERNAL_IDS_DDL = """
CREATE TABLE entity_external_ids (
    external_id_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id            UUID NOT NULL REFERENCES entities(entity_id),
    tenant_id            UUID NOT NULL REFERENCES tenants(tenant_id),
    external_system_slug TEXT NOT NULL,
    external_id          TEXT NOT NULL,
    url                  TEXT,
    metadata_jsonb       JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_entity_external_id UNIQUE (tenant_id, external_system_slug, external_id)
)
"""

_ENTITY_EXTERNAL_IDS_ENTITY_IDX = "CREATE INDEX idx_extid_entity ON entity_external_ids (tenant_id, entity_id)"

_ENTITY_EXTERNAL_IDS_SYSTEM_IDX = (
    "CREATE INDEX idx_extid_system ON entity_external_ids (tenant_id, external_system_slug, external_id)"
)

# ---------------------------------------------------------------------------
# DDL — pii_patterns
# ---------------------------------------------------------------------------

_PII_PATTERNS_DDL = """
CREATE TABLE pii_patterns (
    pattern_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    regex           TEXT NOT NULL,
    is_system       BOOLEAN NOT NULL DEFAULT FALSE,
    detector_module TEXT,
    policy_override TEXT,
    is_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      UUID REFERENCES actors(actor_id),
    CONSTRAINT chk_policy_override
        CHECK (policy_override IS NULL OR policy_override IN ('advisory','warn','block')),
    CONSTRAINT chk_detector_module
        CHECK (detector_module IS NULL OR regex = '__entropy__')
)
"""

_PII_PATTERNS_TENANT_NAME_UNIQ = "CREATE UNIQUE INDEX uq_pii_pattern_tenant_name ON pii_patterns (tenant_id, name)"

# ---------------------------------------------------------------------------
# DDL — pii_field_policies
# ---------------------------------------------------------------------------

_PII_FIELD_POLICIES_DDL = """
CREATE TABLE pii_field_policies (
    policy_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(tenant_id),
    field_type TEXT NOT NULL,
    pattern_id UUID REFERENCES pii_patterns(pattern_id),
    policy     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_field_policy CHECK (policy IN ('advisory','warn','block'))
)
"""

# Functional unique index (pattern_id may be NULL; treat all NULLs as the zero UUID for
# uniqueness purposes).  Must be a CREATE UNIQUE INDEX statement — inline UNIQUE with
# COALESCE is not valid DDL syntax in Postgres 16.
_PII_FIELD_POLICIES_UNIQ_IDX = (
    "CREATE UNIQUE INDEX uq_field_policy ON pii_field_policies "
    "(tenant_id, field_type, COALESCE(pattern_id, '00000000-0000-0000-0000-000000000000'::uuid))"
)

_PII_FIELD_POLICIES_TENANT_IDX = (
    "CREATE INDEX idx_pii_field_policy_tenant ON pii_field_policies (tenant_id, field_type)"
)

# ---------------------------------------------------------------------------
# DDL — pii_detection_log — partitioned monthly
# ---------------------------------------------------------------------------

_PII_DETECTION_LOG_DDL = """
CREATE TABLE pii_detection_log (
    detection_id UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id     UUID REFERENCES actors(actor_id),
    target_type  TEXT NOT NULL,
    target_id    UUID,
    pattern_id   UUID REFERENCES pii_patterns(pattern_id),
    pattern_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    match_offset INTEGER,
    match_length INTEGER,
    action_taken TEXT NOT NULL,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (detection_id, ts)
) PARTITION BY RANGE (ts)
"""

_PII_DETECTION_LOG_TENANT_TS_IDX = "CREATE INDEX idx_pii_log_tenant_ts ON pii_detection_log (tenant_id, ts DESC)"

_PII_DETECTION_LOG_TARGET_IDX = (
    "CREATE INDEX idx_pii_log_target ON pii_detection_log "
    "(tenant_id, target_type, target_id, ts DESC) WHERE target_id IS NOT NULL"
)


def _current_month_partition_bounds(today: datetime.date) -> tuple[str, str, str]:
    """Return (suffix, from_iso, to_iso) for the current month partition."""
    from_d = datetime.date(today.year, today.month, 1)
    if today.month == 12:
        to_d = datetime.date(today.year + 1, 1, 1)
    else:
        to_d = datetime.date(today.year, today.month + 1, 1)
    suffix = f"{from_d.year:04d}_{from_d.month:02d}"
    return suffix, from_d.isoformat(), to_d.isoformat()


# ---------------------------------------------------------------------------
# Vocabulary seeds — new edge_rel, entity_type, pii_category values
# ---------------------------------------------------------------------------

_VOCAB_SEEDS: list[tuple[str, str]] = [
    # edge_rel
    ("edge_rel", "requires"),
    ("edge_rel", "conflicts_with"),
    ("edge_rel", "composes"),
    ("edge_rel", "provides_to"),
    # entity_type
    ("entity_type", "integration"),
    # pii_category
    ("pii_category", "email"),
    ("pii_category", "phone"),
    ("pii_category", "ssn"),
    ("pii_category", "aws_access_key"),
    ("pii_category", "aws_secret_key"),
    ("pii_category", "jwt_token"),
    ("pii_category", "credit_card"),
]

# ---------------------------------------------------------------------------
# System PII pattern rows — built-in patterns seeded as is_system=TRUE.
# Each pattern has a corresponding Python module; the DB row is the registry
# entry and the module is the authoritative implementation.
#
# Columns: name, category, regex, detector_module
# All rows: is_system=TRUE, is_enabled=TRUE, policy_override=NULL
#
# RFC5322-lite email: local-part@domain.tld (simplified but effective)
# E.164 + US/EU phone: includes +1 prefix and dash/dot/space separators
# US SSN: NNN-NN-NNNN with optional dashes
# AWS access key: well-known AKIA prefix + 16 alphanumeric chars
# AWS secret key: entropy-based — no single regex; sentinel + detector_module
# JWT token: three base64url segments separated by dots
# Credit card: major card patterns (Visa/MC/Amex/Discover), 13–16 digits
# ---------------------------------------------------------------------------

_SYSTEM_PII_PATTERNS: list[tuple[str, str, str, str | None]] = [
    (
        "email",
        "email",
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        None,
    ),
    (
        "phone",
        "phone",
        r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)?\d{3}[\s.\-]?\d{4}",
        None,
    ),
    (
        "ssn",
        "ssn",
        r"\b(?!000|666|9\d{2})\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b",
        None,
    ),
    (
        "aws_access_key",
        "aws_access_key",
        r"AKIA[0-9A-Z]{16}",
        None,
    ),
    (
        "aws_secret_key",
        "aws_secret_key",
        "__entropy__",
        "fabric.security.pii_patterns.aws_secret_key",
    ),
    (
        "jwt_token",
        "jwt_token",
        r"eyJ[a-zA-Z0-9_\-]+\.eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+",
        None,
    ),
    (
        "credit_card",
        "credit_card",
        r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b",
        None,
    ),
]

# Stable UUIDs for system PII pattern rows so downgrade can delete them cleanly.
# Generated once and hard-coded to survive round-trips without relying on
# ON CONFLICT with a unique index (which would require knowing the auto-generated UUID).
_SYSTEM_PII_PATTERN_IDS: dict[str, str] = {
    "email": "a0000001-0000-0000-0000-000000000001",
    "phone": "a0000001-0000-0000-0000-000000000002",
    "ssn": "a0000001-0000-0000-0000-000000000003",
    "aws_access_key": "a0000001-0000-0000-0000-000000000004",
    "aws_secret_key": "a0000001-0000-0000-0000-000000000005",
    "jwt_token": "a0000001-0000-0000-0000-000000000006",
    "credit_card": "a0000001-0000-0000-0000-000000000007",
}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # --- edge_property_schemas ---
    op.execute(_EDGE_PROPERTY_SCHEMAS_DDL)
    op.execute(_EDGE_PROPERTY_SCHEMAS_IDX)

    # --- closure_cache ---
    op.execute(_CLOSURE_CACHE_DDL)
    op.execute(_CLOSURE_CACHE_UNIQUE_IDX)
    op.execute(_CLOSURE_CACHE_ROOT_IDX)
    op.execute(_CLOSURE_CACHE_MEMBER_IDX)
    op.execute(_CLOSURE_CACHE_REFRESHED_IDX)

    # --- external_systems (referenced by entity_external_ids) ---
    op.execute(_EXTERNAL_SYSTEMS_DDL)

    # --- entity_external_ids ---
    op.execute(_ENTITY_EXTERNAL_IDS_DDL)
    op.execute(_ENTITY_EXTERNAL_IDS_ENTITY_IDX)
    op.execute(_ENTITY_EXTERNAL_IDS_SYSTEM_IDX)

    # --- pii_patterns ---
    op.execute(_PII_PATTERNS_DDL)
    op.execute(_PII_PATTERNS_TENANT_NAME_UNIQ)

    # --- pii_field_policies ---
    op.execute(_PII_FIELD_POLICIES_DDL)
    op.execute(_PII_FIELD_POLICIES_UNIQ_IDX)
    op.execute(_PII_FIELD_POLICIES_TENANT_IDX)

    # --- pii_detection_log + current-month partition ---
    op.execute(_PII_DETECTION_LOG_DDL)
    op.execute(_PII_DETECTION_LOG_TENANT_TS_IDX)
    op.execute(_PII_DETECTION_LOG_TARGET_IDX)

    today = datetime.date.today()
    suffix, from_iso, to_iso = _current_month_partition_bounds(today)
    op.execute(
        f"CREATE TABLE pii_detection_log_{suffix} "
        f"PARTITION OF pii_detection_log "
        f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
    )

    # --- Vocabulary seeds (idempotent) ---
    for kind, value in _VOCAB_SEEDS:
        op.execute(
            f"INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
            f"VALUES ('{DEFAULT_TENANT_UUID}', '{kind}', '{value}', TRUE) "
            f"ON CONFLICT DO NOTHING"
        )

    # --- System PII pattern rows (dual-representation, idempotent) ---
    # Regex patterns may contain ':' (e.g. in non-capturing groups `(?:...)`).
    # SQLAlchemy text() treats ':name' as bind parameters.  We embed the regex
    # as a SQL string literal using f-strings after escaping single-quotes and
    # replacing ':' with '\:' so Alembic's text() wrapper passes them through.
    for name, category, regex, detector_module in _SYSTEM_PII_PATTERNS:
        pattern_id = _SYSTEM_PII_PATTERN_IDS[name]
        # Escape single quotes and colons for safe embedding in SQL literals.
        regex_sq = regex.replace("'", "''")
        # In SQLAlchemy text(), '\:' is the escape for a literal colon.
        regex_sq = regex_sq.replace(":", r"\:")
        if detector_module is None:
            detector_expr = "NULL"
        else:
            detector_expr = f"'{detector_module}'"
        op.execute(
            f"INSERT INTO pii_patterns "
            f"(pattern_id, tenant_id, name, category, regex, is_system, detector_module, "
            f" is_enabled, created_by) "
            f"VALUES ("
            f"  '{pattern_id}'::uuid,"
            f"  '{DEFAULT_TENANT_UUID}'::uuid,"
            f"  '{name}',"
            f"  '{category}',"
            f"  '{regex_sq}',"
            f"  TRUE,"
            f"  {detector_expr},"
            f"  TRUE,"
            f"  NULL"
            f") ON CONFLICT DO NOTHING"
        )


def downgrade() -> None:
    # Remove system PII pattern rows (seeded above)
    pattern_ids_csv = ", ".join(f"'{pid}'::uuid" for pid in _SYSTEM_PII_PATTERN_IDS.values())
    op.execute(f"DELETE FROM pii_patterns WHERE pattern_id IN ({pattern_ids_csv})")

    # Remove vocabulary seeds
    for kind, value in _VOCAB_SEEDS:
        op.execute(
            f"DELETE FROM vocabulary_values "
            f"WHERE tenant_id = '{DEFAULT_TENANT_UUID}' "
            f"AND kind = '{kind}' AND value = '{value}'"
        )

    # Drop tables in reverse dependency order (CASCADE handles child FKs)
    op.execute("DROP TABLE IF EXISTS pii_detection_log CASCADE")
    op.execute("DROP TABLE IF EXISTS pii_field_policies CASCADE")
    op.execute("DROP TABLE IF EXISTS pii_patterns CASCADE")
    op.execute("DROP TABLE IF EXISTS entity_external_ids CASCADE")
    op.execute("DROP TABLE IF EXISTS external_systems CASCADE")
    op.execute("DROP TABLE IF EXISTS closure_cache CASCADE")
    op.execute("DROP TABLE IF EXISTS edge_property_schemas CASCADE")
