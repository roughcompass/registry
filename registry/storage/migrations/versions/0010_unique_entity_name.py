"""Make (tenant_id, lower(name)) unique on entities.

Revision ID: 0010_unique_entity_name
Revises: 0009_phase7_provider_consumer
Create Date: 2026-05-11

Before: ``idx_entities_tenant_name`` was a non-unique index on
``(tenant_id, lower(name))`` — a tenant could have multiple entities with
the same name (modulo case). That blocked addressing capabilities by
name in URLs / MCP tools / dependency lookups, since one name could
resolve to several entity_ids.

After: a unique index ``uq_entities_tenant_name`` enforces that
``lower(name)`` is unique within each tenant. Slug validation in the
service layer rejects non-slug names at write time
(``catalog/service/slugs.py``).

Pre-flight guard: if any duplicate ``(tenant_id, lower(name))`` pairs
exist when this migration runs, the upgrade fails fast with the
conflicting rows listed — never silently corrupts data.

Existing rows are NOT rewritten. The constraint applies on writes
going forward.

Downgrade: drops the unique index and recreates the original non-unique
``idx_entities_tenant_name`` index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0010_unique_entity_name"
down_revision: str | Sequence[str] | None = "0009_phase7_provider_consumer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    duplicates = list(
        conn.execute(
            text(
                "SELECT tenant_id, lower(name) AS lname, COUNT(*) AS n "
                "FROM entities "
                "GROUP BY tenant_id, lower(name) "
                "HAVING COUNT(*) > 1"
            )
        )
    )
    if duplicates:
        formatted = ", ".join(f"(tenant={r[0]!s}, name={r[1]!s}, n={r[2]})" for r in duplicates)
        msg = (
            "Cannot enforce unique (tenant_id, lower(name)) on entities: "
            f"{len(duplicates)} conflicting group(s) — {formatted}. "
            "Rename one of each pair before re-running the migration."
        )
        raise RuntimeError(msg)

    op.execute("DROP INDEX IF EXISTS idx_entities_tenant_name")
    op.execute("CREATE UNIQUE INDEX uq_entities_tenant_name ON entities (tenant_id, lower(name))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_entities_tenant_name")
    op.execute("CREATE INDEX idx_entities_tenant_name ON entities (tenant_id, lower(name))")
