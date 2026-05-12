"""Add `title` and `body_format` columns to facts for UI consumption.

Revision ID: 0011_artifact_ui_columns
Revises: 0010_unique_entity_name
Create Date: 2026-05-11

UIs rendering a list of artifacts need a short title (for breadcrumbs /
list rows) and the body's format (markdown / html / plain) to choose
the renderer. The Fact table had neither — UIs were inferring titles
from the first line of body and assuming markdown.

This migration adds both as nullable columns, backfills existing rows
(title from the first markdown H1 of body, else the first 80 non-WS
chars; body_format = 'markdown' for everything we don't otherwise
know about), and adds a CHECK constraint on body_format ∈ ('markdown',
'html', 'plain').

New writes are required to provide title (service-layer validation);
body_format defaults to 'markdown' if not specified.

Downgrade: drop both columns + the CHECK constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0011_artifact_ui_columns"
down_revision: str | Sequence[str] | None = "0010_unique_entity_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE facts ADD COLUMN IF NOT EXISTS title TEXT")
    op.execute("ALTER TABLE facts ADD COLUMN IF NOT EXISTS body_format TEXT")

    # Backfill: title from first markdown H1 line of body, else first 80
    # non-whitespace chars. Skip rows that already have a title.
    op.execute(
        text(
            "UPDATE facts SET title = "
            "  COALESCE("
            "    (regexp_match(body, E'^#\\\\s+(.+)$', 'n'))[1], "
            "    btrim(substring(regexp_replace(body, E'\\\\s+', ' ', 'g') from 1 for 80))"
            "  ) "
            "WHERE title IS NULL"
        )
    )
    op.execute(text("UPDATE facts SET body_format = 'markdown' WHERE body_format IS NULL"))

    # CHECK constraint — three known formats, mirrored by service-layer validation.
    op.execute(
        "ALTER TABLE facts ADD CONSTRAINT ck_facts_body_format " "CHECK (body_format IN ('markdown', 'html', 'plain'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE facts DROP CONSTRAINT IF EXISTS ck_facts_body_format")
    op.execute("ALTER TABLE facts DROP COLUMN IF EXISTS body_format")
    op.execute("ALTER TABLE facts DROP COLUMN IF EXISTS title")
