"""Idempotency-key table for safe retry of POST requests.

Revision ID: 0012_idempotency_keys
Revises: 0011_artifact_ui_columns
Create Date: 2026-05-11

POST requests on this API can succeed at the server but fail to reach
the client (network drop, gateway timeout). Retrying the request
without coordination risks a duplicate row. The conventional solution
is `X-Idempotency-Key`: the client sends an opaque string; the server
remembers the first response and replays it on retry.

Schema:
- Composite PK ``(tenant_id, key, method, path)`` — keys are scoped to
  the tenant, the HTTP method, and the route path. A POST to
  /v1/capabilities with key "abc" is independent of a POST to
  /v1/subscriptions with the same key.
- ``request_hash`` (sha256 of the body) catches the
  same-key-different-body case → 409 on mismatch.
- ``response_status`` + ``response_body`` are the replay payload.
- ``expires_at`` enforces a 24h TTL — keys cycle freely after that.

A periodic job (out of scope here) sweeps expired rows; a UNIQUE
index on the PK makes lookups O(1).

Downgrade: drop the table.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_idempotency_keys"
down_revision: str | Sequence[str] | None = "0011_artifact_ui_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE idempotency_keys (
            tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
            key              TEXT NOT NULL,
            method           TEXT NOT NULL,
            path             TEXT NOT NULL,
            request_hash     TEXT NOT NULL,
            response_status  INTEGER NOT NULL,
            response_body    JSONB,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at       TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (tenant_id, key, method, path)
        )
        """
    )
    op.execute("CREATE INDEX idx_idempotency_keys_expires ON idempotency_keys (expires_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_idempotency_keys_expires")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
