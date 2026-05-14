"""Provider/consumer model — 5 new tables + column extensions + seeds.

Revision ID: 0009_phase7_provider_consumer
Revises: 0008_closure_outbox
Create Date: 2026-05-11

New tables:

* ``adoption_events``         — bi-temporal cross-tenant adoption audit trail
* ``subscriptions``           — bi-temporal subscription records
* ``notifications``           — payload-minimal event inbox, partitioned monthly
* ``notification_deliveries`` — webhook delivery attempts, partitioned monthly
* ``integration_pairs``       — denormalized pair-discoverability index

Column extensions:
* ``entities.visibility``                — NOT NULL DEFAULT 'private' + CHECK + index
* ``tenants.is_regulated``               — BOOLEAN DEFAULT FALSE
* ``tenants.notification_digest_window`` — TEXT DEFAULT 'none' + CHECK

Vocabulary seeds (idempotent INSERT ... ON CONFLICT DO NOTHING):
  visibility:  private, tenant-shared, public-in-fabric
  event_kind:  version_published, deprecation, breaking_change, conflict_added, integration_added

capability_type_schemas seed (idempotent):
  Row for type_name='integration' seeded against the default system tenant.
  JSON Schema: config_template (string, optional), runbook_url (string, uri, optional),
               known_issues (array, optional).

integration_pairs trigger:
  ``populate_integration_pairs()`` PL/pgSQL function + ``trg_integration_pairs`` AFTER INSERT
  trigger on ``edges``.  Fires when rel IN ('composes','depends_on') AND source entity type =
  'integration'.  Inserts rows with canonical ordering (capability_a_id < capability_b_id).
  No visibility filter in the trigger — visibility enforcement belongs at the service layer
  so that every consumer of the data (REST, projections, advisor) shares one chokepoint.

downgrade() reverses all changes in dependency-safe order.

Statements are issued one-per-``op.execute`` (asyncpg single-statement requirement).
"""

from __future__ import annotations

import datetime
import json

import sqlalchemy as sa
from alembic import op

revision = "0009_phase7_provider_consumer"
down_revision: str | None = "0008_closure_outbox"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TENANT_UUID = "00000000-0000-0000-0000-000000000000"

# Stable UUID for the integration capability-type schema row so downgrade can
# delete it without relying on the auto-generated value.
_INTEGRATION_SCHEMA_ID = "b0000007-0000-0000-0000-000000000001"

# ---------------------------------------------------------------------------
# entities — visibility column extension
# ---------------------------------------------------------------------------

_ENTITIES_ADD_VISIBILITY = "ALTER TABLE entities " "ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private'"

_ENTITIES_CHECK_VISIBILITY = (
    "ALTER TABLE entities "
    "ADD CONSTRAINT chk_entity_visibility "
    "CHECK (visibility IN ('private', 'tenant-shared', 'public-in-fabric'))"
)

# Partial index for active-row visibility lookups.  The entities table itself
# does not carry t_invalidated_at; the column comes from the attributes table.
# The index is on (tenant_id, visibility) over all entity rows (no WHERE clause
# would be correct here since entities lack per-row soft-delete flags).
_ENTITIES_VISIBILITY_IDX = "CREATE INDEX idx_entities_visibility ON entities (tenant_id, visibility)"

# ---------------------------------------------------------------------------
# tenants — is_regulated + notification_digest_window columns
# ---------------------------------------------------------------------------

_TENANTS_ADD_IS_REGULATED = (
    "ALTER TABLE tenants " "ADD COLUMN IF NOT EXISTS is_regulated BOOLEAN NOT NULL DEFAULT FALSE"
)

_TENANTS_ADD_DIGEST_WINDOW = (
    "ALTER TABLE tenants " "ADD COLUMN IF NOT EXISTS notification_digest_window TEXT NOT NULL DEFAULT 'none'"
)

_TENANTS_CHECK_DIGEST_WINDOW = (
    "ALTER TABLE tenants "
    "ADD CONSTRAINT chk_digest_window "
    "CHECK (notification_digest_window IN ('none','5m','15m','1h','6h','24h'))"
)

# ---------------------------------------------------------------------------
# adoption_events — bi-temporal, consumer tenant is tenant_id
# ---------------------------------------------------------------------------

_ADOPTION_EVENTS_DDL = """
CREATE TABLE adoption_events (
    adoption_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID NOT NULL REFERENCES tenants(tenant_id),
    provider_capability_id UUID NOT NULL REFERENCES entities(entity_id),
    consumer_tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id               UUID REFERENCES actors(actor_id),
    intent                 TEXT,
    version_pin            TEXT,
    t_valid_from           TIMESTAMPTZ NOT NULL,
    t_valid_to             TIMESTAMPTZ,
    t_ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at       TIMESTAMPTZ,
    CONSTRAINT uq_adoption UNIQUE (tenant_id, provider_capability_id, consumer_tenant_id)
        DEFERRABLE INITIALLY DEFERRED
)
"""

_ADOPTION_PROVIDER_IDX = (
    "CREATE INDEX idx_adoption_provider ON adoption_events (provider_capability_id) " "WHERE t_invalidated_at IS NULL"
)

_ADOPTION_CONSUMER_IDX = (
    "CREATE INDEX idx_adoption_consumer ON adoption_events (consumer_tenant_id) " "WHERE t_invalidated_at IS NULL"
)

# ---------------------------------------------------------------------------
# subscriptions — bi-temporal
# ---------------------------------------------------------------------------

_SUBSCRIPTIONS_DDL = """
CREATE TABLE subscriptions (
    subscription_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id              UUID REFERENCES actors(actor_id),
    capability_id         UUID NOT NULL REFERENCES entities(entity_id),
    event_kinds           TEXT[] NOT NULL,
    webhook_url           TEXT,
    webhook_hmac_secret_ref TEXT,
    is_enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    digest_window         TEXT NOT NULL DEFAULT 'none',
    t_valid_from          TIMESTAMPTZ NOT NULL,
    t_valid_to            TIMESTAMPTZ,
    t_ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at      TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
# digest_window captures the tenant's notification_digest_window at
# create/auto-subscribe time and is not retroactively updated.

_SUBSCRIPTIONS_TENANT_IDX = "CREATE INDEX idx_sub_tenant ON subscriptions (tenant_id) " "WHERE t_invalidated_at IS NULL"

_SUBSCRIPTIONS_CAPABILITY_IDX = (
    "CREATE INDEX idx_sub_capability ON subscriptions (capability_id) " "WHERE t_invalidated_at IS NULL"
)

# ---------------------------------------------------------------------------
# notifications — partitioned monthly; payload-minimal (no body/fact content)
# ---------------------------------------------------------------------------

_NOTIFICATIONS_DDL = """
CREATE TABLE notifications (
    notification_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(tenant_id),
    subscription_id       UUID REFERENCES subscriptions(subscription_id),
    capability_id         UUID NOT NULL REFERENCES entities(entity_id),
    capability_slug       TEXT NOT NULL,
    event_kind            TEXT NOT NULL,
    change_classification TEXT,
    version_before        TEXT,
    version_after         TEXT,
    occurred_at           TIMESTAMPTZ NOT NULL,
    fetch_url             TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'unread',
    ts                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (notification_id, ts)
) PARTITION BY RANGE (ts)
"""

_NOTIFICATIONS_TENANT_STATUS_IDX = "CREATE INDEX idx_notif_tenant_status ON notifications (tenant_id, status, ts DESC)"

_NOTIFICATIONS_CAPABILITY_IDX = "CREATE INDEX idx_notif_capability ON notifications (tenant_id, capability_id, ts DESC)"

# ---------------------------------------------------------------------------
# notification_deliveries — partitioned monthly
# ---------------------------------------------------------------------------

_NOTIFICATION_DELIVERIES_DDL = """
CREATE TABLE notification_deliveries (
    delivery_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    notification_id UUID NOT NULL,
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    webhook_url     TEXT NOT NULL,
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL,
    http_status     INTEGER,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at   TIMESTAMPTZ,
    error_text      TEXT,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_id, ts)
) PARTITION BY RANGE (ts)
"""

_DELIVERIES_NOTIFICATION_IDX = "CREATE INDEX idx_delivery_notification ON notification_deliveries (notification_id)"

_DELIVERIES_PENDING_IDX = (
    "CREATE INDEX idx_delivery_pending ON notification_deliveries (tenant_id, next_retry_at) "
    "WHERE status = 'pending'"
)

# ---------------------------------------------------------------------------
# integration_pairs — denormalized pair-discovery index
# ---------------------------------------------------------------------------

_INTEGRATION_PAIRS_DDL = """
CREATE TABLE integration_pairs (
    pair_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    tenant_id             UUID NOT NULL REFERENCES tenants(tenant_id),
    capability_a_id       UUID NOT NULL REFERENCES entities(entity_id),
    capability_b_id       UUID NOT NULL REFERENCES entities(entity_id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_pair_order CHECK (capability_a_id < capability_b_id)
)
"""

_INTEGRATION_PAIRS_UNIQUE_IDX = (
    "CREATE UNIQUE INDEX uq_pair ON integration_pairs "
    "(tenant_id, integration_entity_id, capability_a_id, capability_b_id)"
)

_INTEGRATION_PAIRS_LOOKUP_IDX = (
    "CREATE INDEX idx_pair_lookup ON integration_pairs " "(tenant_id, capability_a_id, capability_b_id)"
)

# ---------------------------------------------------------------------------
# integration_pairs trigger function + trigger
#
# Fires AFTER INSERT on edges WHERE rel IN ('composes','depends_on') and the
# source entity has type='integration'.  Inserts with canonical ordering
# (capability_a_id < capability_b_id) to avoid (A,B)/(B,A) duplicates.
#
# No visibility filter in the trigger — visibility enforcement belongs at the
# service layer so every consumer of the data shares one chokepoint.
# ---------------------------------------------------------------------------

_INTEGRATION_PAIRS_TRIGGER_FUNC = """
CREATE OR REPLACE FUNCTION populate_integration_pairs()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_src_type TEXT;
    v_cap_a    UUID;
    v_cap_b    UUID;
BEGIN
    -- Only process composes and depends_on edges
    IF NEW.rel NOT IN ('composes', 'depends_on') THEN
        RETURN NEW;
    END IF;

    -- Check whether the source entity is of type 'integration'
    -- NOTE: the entities table column is named `entity_type` (not `type`).
    SELECT entity_type INTO v_src_type
      FROM entities
     WHERE entity_id = NEW.src_entity_id;

    IF v_src_type IS DISTINCT FROM 'integration' THEN
        RETURN NEW;
    END IF;

    -- Canonical ordering: smaller UUID goes into capability_a_id
    -- no visibility filter: visibility enforcement belongs at the service layer, not the DB trigger
    IF NEW.src_entity_id < NEW.dst_entity_id THEN
        v_cap_a := NEW.src_entity_id;
        v_cap_b := NEW.dst_entity_id;
    ELSE
        v_cap_a := NEW.dst_entity_id;
        v_cap_b := NEW.src_entity_id;
    END IF;

    INSERT INTO integration_pairs
        (integration_entity_id, tenant_id, capability_a_id, capability_b_id)
    VALUES
        (NEW.src_entity_id, NEW.tenant_id, v_cap_a, v_cap_b)
    ON CONFLICT DO NOTHING;

    RETURN NEW;
END;
$$
"""

_INTEGRATION_PAIRS_TRIGGER = """
CREATE TRIGGER trg_integration_pairs
AFTER INSERT ON edges
FOR EACH ROW EXECUTE FUNCTION populate_integration_pairs()
"""


# ---------------------------------------------------------------------------
# Partition-bounds helper (shared with tests)
# ---------------------------------------------------------------------------


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
# Vocabulary seeds — visibility states + notification event kinds
# ---------------------------------------------------------------------------

_VOCAB_SEEDS: list[tuple[str, str]] = [
    # visibility states
    ("visibility", "private"),
    ("visibility", "tenant-shared"),
    ("visibility", "public-in-fabric"),
    # notification event kinds
    ("notification_event_kind", "version_published"),
    ("notification_event_kind", "deprecation"),
    ("notification_event_kind", "breaking_change"),
    ("notification_event_kind", "conflict_added"),
    ("notification_event_kind", "integration_added"),
]

# ---------------------------------------------------------------------------
# capability_type_schemas seed — integration type
# ---------------------------------------------------------------------------

# JSON Schema for the 'integration' capability type.
# At least 2 composes/depends_on edges required (enforced at the service layer).
# Optional attributes: config_template, runbook_url, known_issues.
_INTEGRATION_TYPE_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "config_template": {"type": "string"},
            "runbook_url": {"type": "string", "format": "uri"},
            "known_issues": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    }
)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # --- entities: visibility column + constraint + index ---
    op.execute(_ENTITIES_ADD_VISIBILITY)
    op.execute(_ENTITIES_CHECK_VISIBILITY)
    op.execute(_ENTITIES_VISIBILITY_IDX)

    # --- tenants: is_regulated + notification_digest_window ---
    op.execute(_TENANTS_ADD_IS_REGULATED)
    op.execute(_TENANTS_ADD_DIGEST_WINDOW)
    op.execute(_TENANTS_CHECK_DIGEST_WINDOW)

    # --- adoption_events ---
    op.execute(_ADOPTION_EVENTS_DDL)
    op.execute(_ADOPTION_PROVIDER_IDX)
    op.execute(_ADOPTION_CONSUMER_IDX)

    # --- subscriptions ---
    op.execute(_SUBSCRIPTIONS_DDL)
    op.execute(_SUBSCRIPTIONS_TENANT_IDX)
    op.execute(_SUBSCRIPTIONS_CAPABILITY_IDX)

    # --- notifications + current-month partition ---
    op.execute(_NOTIFICATIONS_DDL)
    op.execute(_NOTIFICATIONS_TENANT_STATUS_IDX)
    op.execute(_NOTIFICATIONS_CAPABILITY_IDX)

    today = datetime.date.today()
    suffix, from_iso, to_iso = _current_month_partition_bounds(today)
    op.execute(
        f"CREATE TABLE notifications_{suffix} "
        f"PARTITION OF notifications "
        f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
    )

    # --- notification_deliveries + current-month partition ---
    op.execute(_NOTIFICATION_DELIVERIES_DDL)
    op.execute(_DELIVERIES_NOTIFICATION_IDX)
    op.execute(_DELIVERIES_PENDING_IDX)

    op.execute(
        f"CREATE TABLE notification_deliveries_{suffix} "
        f"PARTITION OF notification_deliveries "
        f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
    )

    # --- integration_pairs + trigger ---
    op.execute(_INTEGRATION_PAIRS_DDL)
    op.execute(_INTEGRATION_PAIRS_UNIQUE_IDX)
    op.execute(_INTEGRATION_PAIRS_LOOKUP_IDX)
    op.execute(_INTEGRATION_PAIRS_TRIGGER_FUNC)
    op.execute(_INTEGRATION_PAIRS_TRIGGER)

    # --- Vocabulary seeds (idempotent) ---
    bind = op.get_bind()
    _vocab_insert_sql = sa.text(
        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
        "VALUES (:tid, :kind, :value, TRUE) "
        "ON CONFLICT DO NOTHING"
    )
    for kind, value in _VOCAB_SEEDS:
        bind.execute(
            _vocab_insert_sql,
            {"tid": DEFAULT_TENANT_UUID, "kind": kind, "value": value},
        )

    # --- capability_type_schemas: integration type seed (idempotent) ---
    bind.execute(
        sa.text(
            "INSERT INTO capability_type_schemas "
            "(schema_id, tenant_id, type_name, json_schema, is_advisory, "
            " t_valid_from, t_ingested_at) "
            "VALUES (:schema_id, :tid, 'integration', CAST(:schema_json AS jsonb), "
            "        FALSE, now(), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "schema_id": _INTEGRATION_SCHEMA_ID,
            "tid": DEFAULT_TENANT_UUID,
            "schema_json": _INTEGRATION_TYPE_SCHEMA,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()

    # --- Remove capability_type_schemas seed ---
    bind.execute(
        sa.text("DELETE FROM capability_type_schemas WHERE schema_id = :schema_id"),
        {"schema_id": _INTEGRATION_SCHEMA_ID},
    )

    # --- Remove vocabulary seeds ---
    _vocab_delete_sql = sa.text(
        "DELETE FROM vocabulary_values " "WHERE tenant_id = :tid AND kind = :kind AND value = :value"
    )
    for kind, value in _VOCAB_SEEDS:
        bind.execute(
            _vocab_delete_sql,
            {"tid": DEFAULT_TENANT_UUID, "kind": kind, "value": value},
        )

    # --- Drop trigger + function before dropping integration_pairs ---
    op.execute("DROP TRIGGER IF EXISTS trg_integration_pairs ON edges")
    op.execute("DROP FUNCTION IF EXISTS populate_integration_pairs()")

    # --- Drop tables in reverse FK dependency order ---
    op.execute("DROP TABLE IF EXISTS integration_pairs CASCADE")
    op.execute("DROP TABLE IF EXISTS notification_deliveries CASCADE")
    op.execute("DROP TABLE IF EXISTS notifications CASCADE")
    op.execute("DROP TABLE IF EXISTS subscriptions CASCADE")
    op.execute("DROP TABLE IF EXISTS adoption_events CASCADE")

    # --- Revert tenants columns ---
    op.execute("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS chk_digest_window")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS notification_digest_window")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS is_regulated")

    # --- Revert entities.visibility column ---
    op.execute("DROP INDEX IF EXISTS idx_entities_visibility")
    op.execute("ALTER TABLE entities DROP CONSTRAINT IF EXISTS chk_entity_visibility")
    op.execute("ALTER TABLE entities DROP COLUMN IF EXISTS visibility")
