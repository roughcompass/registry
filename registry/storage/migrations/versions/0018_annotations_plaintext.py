"""Creates the capability_annotations table (plaintext-only, AN phase).

Revision ID: 0018_annotations_plaintext
Revises: 0017_create_progression_overrides
Create Date: 2026-05-12

Adds `capability_annotations`, the bi-temporal table that records consumer
feedback, bug reports, and questions against capabilities. This migration
creates the plaintext-only schema; no ciphertext columns exist at this
stage. The encryption retrofit (body_ciphertext, triage_note_ciphertext,
kek_id, etc.) is a future phase concern and must not be added here.

Column decisions:
- `body TEXT NOT NULL` — annotation body is always required.
- `triage_note TEXT` — optional; added during provider triage.
- `category` and `status` are both TEXT with CHECK constraints rather than
  Postgres ENUM so values can be extended without a blocking ALTER TYPE.
- `author_tenant_id` records which tenant submitted the annotation, allowing
  the service layer to enforce the author-path vs. provider-path distinction
  without an extra join to capabilities.

Partial indexes carry a `WHERE t_invalidated_at IS NULL` predicate so soft-
deleted rows are excluded automatically. All three covering indexes are
defined at the service layer as well for documentation purposes; this file
is the authoritative DDL.

Vocabulary seeds use INSERT ... ON CONFLICT DO NOTHING to make the upgrade
idempotent — safe to run twice on a database where the rows already exist.

upgrade:
  1. CREATE TABLE capability_annotations
  2. CREATE INDEX idx_ann_capability
  3. CREATE INDEX idx_ann_author
  4. CREATE INDEX idx_ann_status
  5. INSERT annotation_category vocabulary rows (5 rows, idempotent)
  6. INSERT annotation_status vocabulary rows (4 rows, idempotent)

downgrade (reverse order):
  1. DELETE seeded vocabulary rows
  2. DROP TABLE capability_annotations (indexes drop with the table)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018_annotations_plaintext"
down_revision: str | None = "0017_create_progression_overrides"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# Sentinel tenant used for system-seeded vocabulary rows. Matches the value
# used by all prior migrations that seed vocabulary_values with is_system=TRUE.
# ---------------------------------------------------------------------------
DEFAULT_TENANT_UUID = "00000000-0000-0000-0000-000000000000"

# ---------------------------------------------------------------------------
# Vocabulary seeds — exported so tests can import them without re-parsing SQL.
# ---------------------------------------------------------------------------

_ANNOTATION_CATEGORY_SEEDS: list[str] = [
    "feedback",
    "bug",
    "suggestion",
    "question",
    "doc_gap",
]

_ANNOTATION_STATUS_SEEDS: list[str] = [
    "open",
    "triaged",
    "acknowledged",
    "closed",
]

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE capability_annotations (
    annotation_id       UUID PRIMARY KEY,
    tenant_id           UUID NOT NULL,
    capability_id       UUID NOT NULL,
    author_actor_id     UUID NOT NULL,
    author_tenant_id    UUID NOT NULL,
    body                TEXT NOT NULL,
    triage_note         TEXT,
    category            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',
    version_target      TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_valid_from        TIMESTAMPTZ NOT NULL,
    t_valid_to          TIMESTAMPTZ,
    t_ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at    TIMESTAMPTZ,
    CONSTRAINT chk_annotation_category CHECK (
        category IN ('feedback', 'bug', 'suggestion', 'question', 'doc_gap')
    ),
    CONSTRAINT chk_annotation_status CHECK (
        status IN ('open', 'triaged', 'acknowledged', 'closed')
    )
)
"""

# Partial indexes exclude soft-deleted rows, keeping the index small and
# ensuring the common read path (active annotations only) is always covered.
_CREATE_IDX_CAPABILITY = (
    "CREATE INDEX idx_ann_capability ON capability_annotations (capability_id) " "WHERE t_invalidated_at IS NULL"
)

_CREATE_IDX_AUTHOR = (
    "CREATE INDEX idx_ann_author ON capability_annotations (author_actor_id) " "WHERE t_invalidated_at IS NULL"
)

_CREATE_IDX_STATUS = (
    "CREATE INDEX idx_ann_status ON capability_annotations (tenant_id, capability_id, status) "
    "WHERE t_invalidated_at IS NULL"
)

_DROP_TABLE = "DROP TABLE IF EXISTS capability_annotations"

_DELETE_VOCAB = (
    "DELETE FROM vocabulary_values " "WHERE is_system = TRUE AND kind IN ('annotation_category', 'annotation_status')"
)


# ---------------------------------------------------------------------------
# Migration body
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.execute(_CREATE_TABLE)
    op.execute(_CREATE_IDX_CAPABILITY)
    op.execute(_CREATE_IDX_AUTHOR)
    op.execute(_CREATE_IDX_STATUS)

    bind = op.get_bind()
    _seed_sql = sa.text(
        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
        "VALUES (:tid, :kind, :value, TRUE) "
        "ON CONFLICT DO NOTHING"
    )
    for value in _ANNOTATION_CATEGORY_SEEDS:
        bind.execute(
            _seed_sql,
            {"tid": DEFAULT_TENANT_UUID, "kind": "annotation_category", "value": value},
        )

    for value in _ANNOTATION_STATUS_SEEDS:
        bind.execute(
            _seed_sql,
            {"tid": DEFAULT_TENANT_UUID, "kind": "annotation_status", "value": value},
        )


def downgrade() -> None:
    op.execute(_DELETE_VOCAB)
    op.execute(_DROP_TABLE)
