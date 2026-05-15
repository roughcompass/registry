"""Unit tests for upsert_entitlement_tenant + upsert_entitlement_actor.

Covers the three tenant paths (existing-active, existing-disabled, new)
and the two actor paths (first-sight, re-sight) plus the audit-emission
discriminator (the UUID-comparison trick used to tell INSERT from
ON CONFLICT DO UPDATE).

The session is stubbed with ``AsyncMock`` — no database. Tests assert on
the SQL text and parameter dicts handed to ``session.execute``, plus on
the return values, plus on whether the audit-row INSERT was issued.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.auth.entitlements.actor_store import (
    DisabledTenantError,
    upsert_entitlement_actor,
    upsert_entitlement_tenant,
)


def _session_with_results(*results: Any) -> AsyncMock:
    """Build an AsyncMock session whose .execute() returns the given
    results in order. Each result should be the value yielded by
    ``Result.first()`` (a row tuple, or None)."""
    session = AsyncMock()

    call_results = []
    for r in results:
        m = MagicMock()
        m.first = MagicMock(return_value=r)
        m.fetchone = MagicMock(return_value=r)
        call_results.append(m)

    session.execute = AsyncMock(side_effect=call_results)
    return session


@pytest.mark.asyncio
class TestUpsertTenant:
    async def test_existing_active_tenant_returns_id_no_writes(self):
        existing_id = uuid.uuid4()
        # Pre-check returns (tenant_id, disabled_at) where disabled_at is None.
        session = _session_with_results((existing_id, None))

        result = await upsert_entitlement_tenant(session, "111")

        assert result == existing_id
        # Exactly one execute call (the SELECT pre-check); no INSERT, no audit.
        assert session.execute.await_count == 1

    async def test_existing_disabled_tenant_raises(self):
        existing_id = uuid.uuid4()
        import datetime
        disabled_at = datetime.datetime.now(tz=datetime.UTC)
        session = _session_with_results((existing_id, disabled_at))

        with pytest.raises(DisabledTenantError) as exc_info:
            await upsert_entitlement_tenant(session, "111")

        assert exc_info.value.slug == "111"
        # Pre-check ran; nothing else.
        assert session.execute.await_count == 1

    async def test_new_tenant_inserts_and_emits_audit(self):
        # Pre-check returns None (no existing row).
        # INSERT RETURNING returns a tuple of (tenant_id,) — same UUID as
        # generated, so the audit row should be emitted.
        # The audit INSERT itself returns nothing.
        # Note: we cannot pre-determine the generated UUID, so we capture
        # the first INSERT call's parameter to know which UUID was used.
        first_insert_uuid: list[uuid.UUID] = []

        session = AsyncMock()

        async def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            result = MagicMock()
            if "SELECT tenant_id, disabled_at" in sql:
                result.first = MagicMock(return_value=None)
            elif "INSERT INTO tenants" in sql:
                # Return the same UUID we were given — simulates an
                # actual INSERT (not a conflict).
                first_insert_uuid.append(params["tenant_id"])
                result.first = MagicMock(return_value=(params["tenant_id"],))
            elif "INSERT INTO audit_log" in sql:
                result.first = MagicMock(return_value=None)
            else:
                result.first = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        returned_id = await upsert_entitlement_tenant(session, "999")

        # The pre-check, the INSERT, and the audit INSERT.
        assert session.execute.await_count == 3
        assert returned_id == first_insert_uuid[0]

    async def test_concurrent_first_sight_returns_existing_uuid_no_audit(self):
        """Race: two coroutines pre-check, both miss, both INSERT — the
        first wins, the second's RETURNING returns the winner's UUID
        (because of `ON CONFLICT DO UPDATE SET slug = EXCLUDED.slug
        RETURNING tenant_id`). The loser must NOT emit a duplicate
        audit row — the UUID-comparison discriminator catches this."""
        winning_uuid = uuid.uuid4()

        session = AsyncMock()
        execute_calls: list[str] = []

        async def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            execute_calls.append(sql)
            result = MagicMock()
            if "SELECT tenant_id, disabled_at" in sql:
                result.first = MagicMock(return_value=None)
            elif "INSERT INTO tenants" in sql:
                # Conflict path: RETURNING yields a DIFFERENT UUID than
                # the one we tried to insert (the winner's).
                result.first = MagicMock(return_value=(winning_uuid,))
            elif "INSERT INTO audit_log" in sql:
                result.first = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        returned_id = await upsert_entitlement_tenant(session, "555")

        assert returned_id == winning_uuid
        # Pre-check + INSERT only — NO audit log INSERT (the audit was
        # emitted by the winning coroutine, not us).
        assert any("INSERT INTO audit_log" in s for s in execute_calls) is False
        assert session.execute.await_count == 2


@pytest.mark.asyncio
class TestUpsertActor:
    async def test_first_sight_inserts_and_emits_audit(self):
        first_insert_uuid: list[uuid.UUID] = []

        session = AsyncMock()

        async def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            result = MagicMock()
            if "INSERT INTO actors" in sql:
                first_insert_uuid.append(params["actor_id"])
                # Match — first sight, RETURNING yields the same UUID.
                result.first = MagicMock(return_value=(params["actor_id"],))
            elif "INSERT INTO audit_log" in sql:
                result.first = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        tenant_id = uuid.uuid4()
        returned_id = await upsert_entitlement_actor(
            session, tenant_id, "user-abc", "User Display"
        )

        assert returned_id == first_insert_uuid[0]
        # INSERT + audit.
        assert session.execute.await_count == 2

    async def test_re_sight_returns_existing_actor_no_audit(self):
        """ON CONFLICT DO UPDATE returns the existing UUID; the
        UUID-comparison discriminator skips the audit emission."""
        existing_actor = uuid.uuid4()

        session = AsyncMock()
        execute_calls: list[str] = []

        async def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            execute_calls.append(sql)
            result = MagicMock()
            if "INSERT INTO actors" in sql:
                # Conflict: RETURNING yields the EXISTING actor_id (not
                # the one we tried to insert).
                result.first = MagicMock(return_value=(existing_actor,))
            elif "INSERT INTO audit_log" in sql:
                result.first = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        returned_id = await upsert_entitlement_actor(
            session, uuid.uuid4(), "user-abc", "Updated Name"
        )

        assert returned_id == existing_actor
        # No audit INSERT.
        assert any("INSERT INTO audit_log" in s for s in execute_calls) is False
        assert session.execute.await_count == 1

    async def test_display_name_passed_to_insert(self):
        """The supplied display_name (not oidc_subject) is what lands in
        the row when one is provided."""
        captured: dict[str, Any] = {}

        session = AsyncMock()

        async def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            result = MagicMock()
            if "INSERT INTO actors" in sql:
                captured.update(params)
                result.first = MagicMock(return_value=(params["actor_id"],))
            elif "INSERT INTO audit_log" in sql:
                result.first = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        await upsert_entitlement_actor(
            session, uuid.uuid4(), "user-abc", "Jane Doe"
        )

        assert captured["display_name"] == "Jane Doe"
        assert captured["oidc_subject"] == "user-abc"

    async def test_display_name_falls_back_to_oidc_subject_when_none(self):
        """display_name is NOT NULL in the schema, so a None argument
        must be backfilled with oidc_subject."""
        captured: dict[str, Any] = {}

        session = AsyncMock()

        async def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            result = MagicMock()
            if "INSERT INTO actors" in sql:
                captured.update(params)
                result.first = MagicMock(return_value=(params["actor_id"],))
            elif "INSERT INTO audit_log" in sql:
                result.first = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        await upsert_entitlement_actor(
            session, uuid.uuid4(), "user-abc", None
        )

        assert captured["display_name"] == "user-abc"


class TestDisabledTenantError:
    def test_carries_slug(self):
        err = DisabledTenantError("acme")
        assert err.slug == "acme"
        assert "acme" in str(err)
