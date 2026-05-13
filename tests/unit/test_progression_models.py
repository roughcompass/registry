"""Unit tests for the ProgressionDefinition and ProgressionOverride ORM models.

Verifies model shape and column presence without touching a real database —
all assertions are purely structural (Python object instantiation and table
metadata inspection).
"""

from __future__ import annotations

import datetime
import uuid

from registry.storage.models import ProgressionDefinition, ProgressionOverride


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class TestProgressionDefinitionShape:
    """ORM model shape — all required fields must round-trip through __init__."""

    def test_definition_instantiation_with_all_fields(self) -> None:
        """ProgressionDefinition accepts every column value and stores them correctly."""
        progression_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        now = _now()

        row = ProgressionDefinition(
            progression_id=progression_id,
            tenant_id=tenant_id,
            entity_type="engineer",
            definition={"stages": ["junior", "senior"], "transitions": []},
            is_advisory=False,
            t_valid_from=now,
            t_valid_to=None,
            t_ingested_at=now,
            t_invalidated_at=None,
        )

        assert row.progression_id == progression_id
        assert row.tenant_id == tenant_id
        assert row.entity_type == "engineer"
        assert row.definition == {"stages": ["junior", "senior"], "transitions": []}
        assert row.is_advisory is False
        assert row.t_valid_from == now
        assert row.t_valid_to is None
        assert row.t_ingested_at == now
        assert row.t_invalidated_at is None

    def test_definition_is_advisory_default_is_false(self) -> None:
        """is_advisory defaults to False — enforcement mode is opt-out, not opt-in."""
        # SQLAlchemy applies column defaults only on INSERT; for a Python-side
        # default we check the mapped_column default declaration instead.
        col = ProgressionDefinition.__table__.columns["is_advisory"]
        assert col.default.arg is False


class TestProgressionDefinitionBiTemporalColumns:
    """Bi-temporal column names must match the registry standard exactly."""

    def test_bitemporal_columns_present(self) -> None:
        """Table metadata must include all four bi-temporal column names."""
        col_names = set(ProgressionDefinition.__table__.columns.keys())
        required = {"t_valid_from", "t_valid_to", "t_ingested_at", "t_invalidated_at"}
        missing = required - col_names
        assert not missing, f"Missing bi-temporal columns: {missing}"


class TestProgressionOverrideShape:
    """ORM model shape for ProgressionOverride — structural assertions only, no DB."""

    def test_override_instantiation_with_all_fields(self) -> None:
        """ProgressionOverride accepts every required field; bypass_skip_rules defaults to False."""
        override_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        entity_id = uuid.uuid4()
        authorized_by = uuid.uuid4()
        audit_event_id = uuid.uuid4()
        now = datetime.datetime.now(tz=datetime.UTC)
        later = now + datetime.timedelta(hours=24)

        row = ProgressionOverride(
            override_id=override_id,
            tenant_id=tenant_id,
            entity_id=entity_id,
            from_state="draft",
            to_state="review",
            gate_id="approval_gate",
            bypass_skip_rules=False,
            reason="Emergency release — approved by VP",
            authorized_by=authorized_by,
            t_valid_from=now,
            t_valid_to=later,
            consumed_at=None,
            audit_event_id=audit_event_id,
        )

        assert row.override_id == override_id
        assert row.tenant_id == tenant_id
        assert row.entity_id == entity_id
        assert row.from_state == "draft"
        assert row.to_state == "review"
        assert row.gate_id == "approval_gate"
        assert row.authorized_by == authorized_by
        assert row.audit_event_id == audit_event_id

        # bypass_skip_rules must default to False — explicit opt-in required.
        col = ProgressionOverride.__table__.columns["bypass_skip_rules"]
        assert col.default.arg is False

        # Confirm all FK columns are present in table metadata.
        col_names = set(ProgressionOverride.__table__.columns.keys())
        for required_col in ("tenant_id", "entity_id", "authorized_by", "audit_event_id"):
            assert required_col in col_names, f"FK column missing: {required_col}"

    def test_consumed_at_is_nullable(self) -> None:
        """consumed_at must be nullable — single-use invariant is enforced at the
        service layer (ProgressionService), not via DB constraint. A row with
        consumed_at IS NOT NULL is conceptually frozen: it has been consumed and
        must not be re-used. The service checks consumed_at IS NULL before
        consuming and writes consumed_at in the same transaction.
        """
        col = ProgressionOverride.__table__.columns["consumed_at"]
        assert col.nullable is True, "consumed_at must be nullable (service enforces single-use)"

        # Verify a None value round-trips through the ORM constructor.
        row = ProgressionOverride(
            override_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            entity_id=uuid.uuid4(),
            from_state="draft",
            to_state="published",
            gate_id="*",
            reason="Override for testing",
            authorized_by=uuid.uuid4(),
            t_valid_from=datetime.datetime.now(tz=datetime.UTC),
            t_valid_to=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=1),
            consumed_at=None,
            audit_event_id=uuid.uuid4(),
        )
        assert row.consumed_at is None
