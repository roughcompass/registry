"""Unit tests for registry.auth.entitlements.actor_store.upsert_entitlement_tenant.

Covers two paths:
  - First-sight SEAL: INSERT succeeds (RETURNING yields a row), audit event
    written, UUID returned.
  - Re-sight SEAL: INSERT hits DO NOTHING (RETURNING empty), follow-up SELECT
    returns the existing UUID, no audit event written.

The session is stubbed with AsyncMock so no database is required.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.auth.entitlements.actor_store import upsert_entitlement_tenant

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(tenant_id: uuid.UUID) -> MagicMock:
    """Return a mock DB row whose first element is tenant_id."""
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda i: tenant_id if i == 0 else None)
    return row


def _session_first_sight(tenant_id: uuid.UUID) -> AsyncMock:
    """
    Session mock where:
      - First execute (INSERT ... RETURNING) returns a single row.
      - Second execute (INSERT INTO audit_log) returns nothing meaningful.
    """
    insert_result = MagicMock()
    insert_result.fetchone = MagicMock(return_value=_make_row(tenant_id))

    audit_result = MagicMock()
    audit_result.fetchone = MagicMock(return_value=None)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[insert_result, audit_result])
    return session


def _session_re_sight(existing_id: uuid.UUID) -> AsyncMock:
    """
    Session mock where:
      - First execute (INSERT ... RETURNING) returns no row (DO NOTHING path).
      - Second execute (SELECT ... WHERE external_tenant_id) returns existing row.
    """
    no_row_result = MagicMock()
    no_row_result.fetchone = MagicMock(return_value=None)

    select_result = MagicMock()
    select_result.fetchone = MagicMock(return_value=_make_row(existing_id))

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[no_row_result, select_result])
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_sight_seal_returns_new_uuid_and_emits_audit() -> None:
    """INSERT succeeds: function returns the new tenant UUID and writes audit."""
    tenant_id = uuid.uuid4()
    session = _session_first_sight(tenant_id)

    returned = await upsert_entitlement_tenant(session, "12345")

    assert returned == tenant_id

    # Two execute calls: INSERT ... RETURNING, then INSERT INTO audit_log.
    assert session.execute.call_count == 2

    # Verify the audit INSERT call includes the correct after_jsonb payload fields.
    audit_call_kwargs = session.execute.call_args_list[1]
    params = audit_call_kwargs[0][1]  # positional arg index 1 is the params dict
    assert params["tenant_id"] == tenant_id
    assert params["target_id"] == tenant_id
    after = params["after_jsonb"]
    assert str(tenant_id) in after
    assert "12345" in after
    assert '"provider": "jit"' in after
    assert '"source": "entitlement"' in after


@pytest.mark.asyncio
async def test_re_sight_seal_returns_existing_uuid_no_audit() -> None:
    """DO NOTHING path: function returns existing UUID without writing audit."""
    existing_id = uuid.uuid4()
    session = _session_re_sight(existing_id)

    returned = await upsert_entitlement_tenant(session, "12345")

    assert returned == existing_id

    # Two execute calls: INSERT ... RETURNING (no row), then SELECT.
    assert session.execute.call_count == 2

    # Confirm second call is a SELECT (not an audit INSERT).
    select_sql = str(session.execute.call_args_list[1][0][0])
    assert "SELECT" in select_sql.upper()
    assert "audit_log" not in select_sql.lower()


@pytest.mark.asyncio
async def test_first_sight_audit_payload_contains_all_required_keys() -> None:
    """Audit after_jsonb contains tenant_id, external_tenant_id, provider, source."""
    tenant_id = uuid.uuid4()
    session = _session_first_sight(tenant_id)

    await upsert_entitlement_tenant(session, "99999")

    params = session.execute.call_args_list[1][0][1]
    after = params["after_jsonb"]
    assert '"provider": "jit"' in after
    assert '"source": "entitlement"' in after
    assert '"external_tenant_id": "99999"' in after
    assert str(tenant_id) in after
