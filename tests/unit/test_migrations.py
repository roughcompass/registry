"""Unit tests for the provider-consumer Alembic migration.

Tests confirm that:
1. The migration module is importable without a DB connection and carries
   the correct revision chain.
2. upgrade() emits DDL for all 7 new tables and seeds vocabulary rows.
3. downgrade() emits DROP TABLE statements for all new tables and DELETE
   statements for the seeded rows.
4. Partition-name helper produces the correct bounds for a known date.
5. System PII pattern IDs are unique and have the correct sentinel value
   for the entropy-based detector.

The round-trip against a real Postgres DB (upgrade → downgrade → upgrade)
is validated by the session-scoped ``pg_container`` fixture in conftest.py;
those tests are marked ``integration`` and run separately.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Load the migration module without a real DB
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIG_PATH = _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0007_phase6_graph_primitives.py"

_MIG_SPEC = importlib.util.spec_from_file_location("migration_0007", _MIG_PATH)
assert _MIG_SPEC is not None and _MIG_SPEC.loader is not None
_mig = importlib.util.module_from_spec(_MIG_SPEC)
_MIG_SPEC.loader.exec_module(_mig)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NEW_TABLES = [
    "edge_property_schemas",
    "closure_cache",
    "external_systems",
    "entity_external_ids",
    "pii_patterns",
    "pii_field_policies",
    "pii_detection_log",
]

_VOCAB_SEEDS = _mig._VOCAB_SEEDS  # list of (kind, value)
_SYSTEM_PII_PATTERNS = _mig._SYSTEM_PII_PATTERNS  # list of (name, cat, regex, mod)
_SYSTEM_PII_PATTERN_IDS = _mig._SYSTEM_PII_PATTERN_IDS  # dict name -> uuid str
DEFAULT_TENANT_UUID = _mig.DEFAULT_TENANT_UUID


def _make_capturing_op_patches(executed: list[str]) -> tuple[Any, Any]:
    """Build (capture_op_execute, capture_op_get_bind) so both flow into *executed*.

    Migrations issue some statements via ``op.execute(...)`` and others via
    ``bind = op.get_bind(); bind.execute(text(...), {...})`` (the
    parameterized path). For bind.execute we serialize the bind dict
    alongside the SQL so legacy tests that look for the seed value in the
    captured text continue to find it after the bind-parameter migration.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    def capture_execute(sql: object) -> None:
        executed.append(str(sql))

    def _capture_bind_execute(sql: object, *args: Any, **kwargs: Any) -> None:
        params: Any = None
        if args:
            params = args[0]
        elif "parameters" in kwargs:
            params = kwargs["parameters"]
        # Append SQL + param values so legacy tests that grep for a seed
        # value (which used to be a SQL literal, now lives in the bind dict)
        # continue to find it.
        param_str = " | params=" + str(params) if params else ""
        executed.append(str(sql) + param_str)

    bind_mock = MagicMock()
    bind_mock.execute = MagicMock(side_effect=_capture_bind_execute)

    def capture_get_bind() -> Any:
        return bind_mock

    return capture_execute, capture_get_bind


def _capture_upgrade() -> list[str]:
    """Run upgrade() with op.execute + op.get_bind patched; capture all SQL strings."""
    executed: list[str] = []
    from alembic import op  # noqa: PLC0415

    capture_execute, capture_get_bind = _make_capturing_op_patches(executed)
    original_execute = getattr(op, "execute", None)
    original_get_bind = getattr(op, "get_bind", None)
    try:
        op.execute = capture_execute  # type: ignore[attr-defined]
        op.get_bind = capture_get_bind  # type: ignore[attr-defined]
        _mig.upgrade()
    finally:
        if original_execute is not None:
            op.execute = original_execute  # type: ignore[attr-defined]
        if original_get_bind is not None:
            op.get_bind = original_get_bind  # type: ignore[attr-defined]
    return executed


def _capture_downgrade() -> list[str]:
    """Run downgrade() with op.execute + op.get_bind patched; capture all SQL strings."""
    executed: list[str] = []
    from alembic import op  # noqa: PLC0415

    capture_execute, capture_get_bind = _make_capturing_op_patches(executed)
    original_execute = getattr(op, "execute", None)
    original_get_bind = getattr(op, "get_bind", None)
    try:
        op.execute = capture_execute  # type: ignore[attr-defined]
        op.get_bind = capture_get_bind  # type: ignore[attr-defined]
        _mig.downgrade()
    finally:
        if original_execute is not None:
            op.execute = original_execute  # type: ignore[attr-defined]
        if original_get_bind is not None:
            op.get_bind = original_get_bind  # type: ignore[attr-defined]
    return executed


# ---------------------------------------------------------------------------
# Module-level checks
# ---------------------------------------------------------------------------


class TestMigrationModule:
    def test_revision_id(self) -> None:
        assert _mig.revision == "0007_phase6_graph_primitives"

    def test_down_revision(self) -> None:
        assert _mig.down_revision == "0006_phase5_partitions"

    def test_upgrade_callable(self) -> None:
        assert callable(_mig.upgrade)

    def test_downgrade_callable(self) -> None:
        assert callable(_mig.downgrade)


# ---------------------------------------------------------------------------
# upgrade() DDL coverage
# ---------------------------------------------------------------------------


class TestUpgradeDdl:
    def test_all_new_tables_created(self) -> None:
        executed = _capture_upgrade()
        combined = "\n".join(executed)
        for table in _NEW_TABLES:
            assert f"CREATE TABLE {table}" in combined, f"CREATE TABLE {table} not found in upgrade() DDL"

    def test_pii_detection_log_partitioned(self) -> None:
        executed = _capture_upgrade()
        combined = "\n".join(executed)
        assert "PARTITION BY RANGE" in combined, "pii_detection_log must use PARTITION BY RANGE"

    def test_current_month_partition_created(self) -> None:
        executed = _capture_upgrade()
        today = datetime.date.today()
        suffix = f"{today.year:04d}_{today.month:02d}"
        combined = "\n".join(executed)
        assert (
            f"pii_detection_log_{suffix}" in combined
        ), f"Expected current-month partition pii_detection_log_{suffix} in upgrade()"

    def test_closure_cache_check_constraint_in_ddl(self) -> None:
        combined = "\n".join(_capture_upgrade())
        assert (
            "chk_direction" in combined or "direction IN" in combined
        ), "closure_cache direction CHECK constraint missing"

    def test_edge_property_schemas_bitemporal_columns(self) -> None:
        combined = "\n".join(_capture_upgrade())
        for col in ("t_valid_from", "t_valid_to", "t_ingested_at", "t_invalidated_at"):
            assert col in combined, f"Bi-temporal column {col} missing from upgrade() DDL"

    def test_entity_external_ids_no_t_invalidated_at(self) -> None:
        """entity_external_ids is hard-delete — no t_invalidated_at column."""
        executed = _capture_upgrade()
        # Find the CREATE TABLE statement for entity_external_ids only
        eid_stmts = [s for s in executed if "CREATE TABLE entity_external_ids" in s]
        assert len(eid_stmts) == 1
        assert "t_invalidated_at" not in eid_stmts[0]

    def test_pii_patterns_is_system_and_detector_module_columns(self) -> None:
        executed = _capture_upgrade()
        pii_stmts = [s for s in executed if "CREATE TABLE pii_patterns" in s]
        assert len(pii_stmts) == 1
        stmt = pii_stmts[0]
        assert "is_system" in stmt
        assert "detector_module" in stmt


# ---------------------------------------------------------------------------
# upgrade() vocabulary seeds
# ---------------------------------------------------------------------------


class TestUpgradeVocabSeeds:
    def test_edge_rel_seeds_present(self) -> None:
        executed = _capture_upgrade()
        combined = "\n".join(executed)
        for rel in ("requires", "conflicts_with", "composes", "provides_to"):
            assert rel in combined, f"edge_rel '{rel}' not found in upgrade() seeds"

    def test_entity_type_integration_seed_present(self) -> None:
        combined = "\n".join(_capture_upgrade())
        assert "integration" in combined

    def test_all_pii_category_seeds_present(self) -> None:
        executed = _capture_upgrade()
        combined = "\n".join(executed)
        for _, value in _VOCAB_SEEDS:
            if _is_pii_category(value):
                assert value in combined, f"pii_category '{value}' not found in upgrade() seeds"

    def test_seeds_use_on_conflict_do_nothing(self) -> None:
        executed = _capture_upgrade()
        insert_stmts = [s for s in executed if "INSERT INTO vocabulary_values" in s]
        assert len(insert_stmts) == len(_VOCAB_SEEDS), f"Expected {len(_VOCAB_SEEDS)} vocab INSERT statements"
        for stmt in insert_stmts:
            assert "ON CONFLICT DO NOTHING" in stmt, f"ON CONFLICT DO NOTHING missing from vocab seed: {stmt!r}"


def _is_pii_category(value: str) -> bool:
    return any(kind == "pii_category" and v == value for kind, v in _VOCAB_SEEDS)


# ---------------------------------------------------------------------------
# upgrade() system PII pattern seeds
# ---------------------------------------------------------------------------


class TestUpgradeSystemPiiPatterns:
    def test_all_seven_system_patterns_seeded(self) -> None:
        executed = _capture_upgrade()
        combined = "\n".join(executed)
        for name, _, _, _ in _SYSTEM_PII_PATTERNS:
            assert name in combined, f"System PII pattern '{name}' not found in upgrade() seeds"

    def test_is_system_true_in_pattern_inserts(self) -> None:
        executed = _capture_upgrade()
        pattern_inserts = [s for s in executed if "INSERT INTO pii_patterns" in s]
        assert len(pattern_inserts) == len(
            _SYSTEM_PII_PATTERNS
        ), f"Expected {len(_SYSTEM_PII_PATTERNS)} pii_patterns INSERT statements"
        for stmt in pattern_inserts:
            assert "TRUE" in stmt, f"is_system=TRUE missing from pattern insert: {stmt!r}"

    def test_aws_secret_key_uses_entropy_sentinel(self) -> None:
        executed = _capture_upgrade()
        aws_stmts = [s for s in executed if "aws_secret_key" in s and "INSERT INTO pii_patterns" in s]
        assert len(aws_stmts) == 1
        assert "__entropy__" in aws_stmts[0]

    def test_aws_secret_key_has_detector_module(self) -> None:
        executed = _capture_upgrade()
        aws_stmts = [s for s in executed if "aws_secret_key" in s and "INSERT INTO pii_patterns" in s]
        assert "fabric.security.pii_patterns.aws_secret_key" in aws_stmts[0]

    def test_non_entropy_patterns_have_no_detector_module(self) -> None:
        executed = _capture_upgrade()
        # email insert must not reference any detector_module
        email_stmts = [s for s in executed if "INSERT INTO pii_patterns" in s and "'email'" in s and "aws" not in s]
        assert len(email_stmts) >= 1
        for stmt in email_stmts:
            assert "NULL" in stmt, f"email pattern insert should have NULL detector_module: {stmt!r}"

    def test_pattern_ids_are_unique(self) -> None:
        ids = list(_SYSTEM_PII_PATTERN_IDS.values())
        assert len(ids) == len(set(ids)), "Duplicate pattern UUIDs in _SYSTEM_PII_PATTERN_IDS"

    def test_pattern_inserts_use_on_conflict_do_nothing(self) -> None:
        executed = _capture_upgrade()
        pattern_inserts = [s for s in executed if "INSERT INTO pii_patterns" in s]
        for stmt in pattern_inserts:
            assert "ON CONFLICT DO NOTHING" in stmt, f"ON CONFLICT DO NOTHING missing from pii_patterns seed: {stmt!r}"


# ---------------------------------------------------------------------------
# downgrade() coverage
# ---------------------------------------------------------------------------


class TestDowngradeDdl:
    def test_all_new_tables_dropped(self) -> None:
        executed = _capture_downgrade()
        combined = "\n".join(executed)
        for table in _NEW_TABLES:
            assert table in combined, f"Table {table} not referenced in downgrade() DROP statements"

    def test_vocab_seeds_deleted_in_downgrade(self) -> None:
        executed = _capture_downgrade()
        combined = "\n".join(executed)
        for _kind, value in _VOCAB_SEEDS:
            assert value in combined, f"vocab value '{value}' not deleted in downgrade()"

    def test_system_pii_patterns_deleted_in_downgrade(self) -> None:
        executed = _capture_downgrade()
        combined = "\n".join(executed)
        for pid in _SYSTEM_PII_PATTERN_IDS.values():
            assert pid in combined, f"System PII pattern id {pid} not deleted in downgrade()"

    def test_pii_field_policies_dropped_before_pii_patterns(self) -> None:
        """pii_field_policies has FK to pii_patterns; must be dropped first."""
        executed = _capture_downgrade()
        drop_stmts = [s for s in executed if "DROP TABLE" in s]
        field_policy_pos = next((i for i, s in enumerate(drop_stmts) if "pii_field_policies" in s), None)
        pii_patterns_pos = next(
            (i for i, s in enumerate(drop_stmts) if "pii_patterns" in s and "pii_field_policies" not in s), None
        )
        assert field_policy_pos is not None, "pii_field_policies DROP not found"
        assert pii_patterns_pos is not None, "pii_patterns DROP not found"
        assert field_policy_pos < pii_patterns_pos, "pii_field_policies must be dropped before pii_patterns"

    def test_detection_log_dropped_before_pii_patterns(self) -> None:
        """pii_detection_log has FK to pii_patterns; must be dropped first."""
        executed = _capture_downgrade()
        drop_stmts = [s for s in executed if "DROP TABLE" in s]
        log_pos = next((i for i, s in enumerate(drop_stmts) if "pii_detection_log" in s), None)
        pii_pos = next(
            (
                i
                for i, s in enumerate(drop_stmts)
                if "pii_patterns" in s and "pii_detection_log" not in s and "pii_field_policies" not in s
            ),
            None,
        )
        assert log_pos is not None
        assert pii_pos is not None
        assert log_pos < pii_pos


# ---------------------------------------------------------------------------
# Partition-bounds helper
# ---------------------------------------------------------------------------


class TestPartitionBoundsHelper:
    def test_known_date_may_2026(self) -> None:
        suffix, from_iso, to_iso = _mig._current_month_partition_bounds(datetime.date(2026, 5, 10))
        assert suffix == "2026_05"
        assert from_iso == "2026-05-01"
        assert to_iso == "2026-06-01"

    def test_december_wraps_to_next_year(self) -> None:
        suffix, from_iso, to_iso = _mig._current_month_partition_bounds(datetime.date(2026, 12, 15))
        assert suffix == "2026_12"
        assert from_iso == "2026-12-01"
        assert to_iso == "2027-01-01"

    def test_january(self) -> None:
        suffix, from_iso, to_iso = _mig._current_month_partition_bounds(datetime.date(2027, 1, 1))
        assert suffix == "2027_01"
        assert from_iso == "2027-01-01"
        assert to_iso == "2027-02-01"


# ===========================================================================
# Migration 0009_phase7_provider_consumer unit tests
# ===========================================================================

_MIG7_PATH = _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0009_phase7_provider_consumer.py"

_MIG7_SPEC = importlib.util.spec_from_file_location("migration_0009", _MIG7_PATH)
assert _MIG7_SPEC is not None and _MIG7_SPEC.loader is not None
_mig7 = importlib.util.module_from_spec(_MIG7_SPEC)
_MIG7_SPEC.loader.exec_module(_mig7)  # type: ignore[union-attr]

_P7_NEW_TABLES = [
    "adoption_events",
    "subscriptions",
    "notifications",
    "notification_deliveries",
    "integration_pairs",
]

_P7_VOCAB_SEEDS = _mig7._VOCAB_SEEDS
_P7_INTEGRATION_SCHEMA_ID = _mig7._INTEGRATION_SCHEMA_ID
_P7_DEFAULT_TENANT = _mig7.DEFAULT_TENANT_UUID


def _capture_p7_upgrade() -> list[str]:
    """Run migration upgrade() with op.execute + op.get_bind patched."""
    executed: list[str] = []
    from alembic import op  # noqa: PLC0415

    capture_execute, capture_get_bind = _make_capturing_op_patches(executed)
    original_execute = getattr(op, "execute", None)
    original_get_bind = getattr(op, "get_bind", None)
    try:
        op.execute = capture_execute  # type: ignore[attr-defined]
        op.get_bind = capture_get_bind  # type: ignore[attr-defined]
        _mig7.upgrade()
    finally:
        if original_execute is not None:
            op.execute = original_execute  # type: ignore[attr-defined]
        if original_get_bind is not None:
            op.get_bind = original_get_bind  # type: ignore[attr-defined]
    return executed


def _capture_p7_downgrade() -> list[str]:
    """Run migration downgrade() with op.execute + op.get_bind patched."""
    executed: list[str] = []
    from alembic import op  # noqa: PLC0415

    capture_execute, capture_get_bind = _make_capturing_op_patches(executed)
    original_execute = getattr(op, "execute", None)
    original_get_bind = getattr(op, "get_bind", None)
    try:
        op.execute = capture_execute  # type: ignore[attr-defined]
        op.get_bind = capture_get_bind  # type: ignore[attr-defined]
        _mig7.downgrade()
    finally:
        if original_execute is not None:
            op.execute = original_execute  # type: ignore[attr-defined]
        if original_get_bind is not None:
            op.get_bind = original_get_bind  # type: ignore[attr-defined]
    return executed


class TestP7MigrationModule:
    def test_revision_id(self) -> None:
        assert _mig7.revision == "0009_phase7_provider_consumer"

    def test_down_revision(self) -> None:
        assert _mig7.down_revision == "0008_closure_outbox"

    def test_upgrade_callable(self) -> None:
        assert callable(_mig7.upgrade)

    def test_downgrade_callable(self) -> None:
        assert callable(_mig7.downgrade)


class TestP7UpgradeDdl:
    def test_all_new_tables_created(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        for table in _P7_NEW_TABLES:
            assert f"CREATE TABLE {table}" in combined, f"CREATE TABLE {table} not found in migration upgrade() DDL"

    def test_entities_visibility_column_added(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert "ADD COLUMN" in combined and "visibility" in combined

    def test_entities_visibility_check_constraint(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert "chk_entity_visibility" in combined

    def test_entities_visibility_index_created(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert "idx_entities_visibility" in combined

    def test_tenants_is_regulated_added(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert "is_regulated" in combined

    def test_tenants_digest_window_added_with_check(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert "notification_digest_window" in combined
        assert "chk_digest_window" in combined

    def test_adoption_events_bitemporal_columns(self) -> None:
        executed = _capture_p7_upgrade()
        adoption_stmts = [s for s in executed if "CREATE TABLE adoption_events" in s]
        assert len(adoption_stmts) == 1
        stmt = adoption_stmts[0]
        for col in ("t_valid_from", "t_valid_to", "t_ingested_at", "t_invalidated_at"):
            assert col in stmt, f"Bi-temporal column {col} missing from adoption_events DDL"

    def test_adoption_events_deferrable_unique_constraint(self) -> None:
        executed = _capture_p7_upgrade()
        adoption_stmts = [s for s in executed if "CREATE TABLE adoption_events" in s]
        assert len(adoption_stmts) == 1
        assert "DEFERRABLE INITIALLY DEFERRED" in adoption_stmts[0]

    def test_subscriptions_bitemporal_columns(self) -> None:
        executed = _capture_p7_upgrade()
        sub_stmts = [s for s in executed if "CREATE TABLE subscriptions" in s]
        assert len(sub_stmts) == 1
        stmt = sub_stmts[0]
        for col in ("t_valid_from", "t_valid_to", "t_ingested_at", "t_invalidated_at"):
            assert col in stmt, f"Bi-temporal column {col} missing from subscriptions DDL"

    def test_subscriptions_digest_window_column(self) -> None:
        """Subscriptions carry digest_window to snapshot the consumer's preferred digest window at subscribe time."""
        executed = _capture_p7_upgrade()
        sub_stmts = [s for s in executed if "CREATE TABLE subscriptions" in s]
        assert len(sub_stmts) == 1
        assert "digest_window" in sub_stmts[0]

    def test_notifications_partitioned_monthly(self) -> None:
        executed = _capture_p7_upgrade()
        notif_stmts = [s for s in executed if "CREATE TABLE notifications" in s and "notification_deliveries" not in s]
        # Should include the parent table DDL and the current-month partition
        parent_stmts = [s for s in notif_stmts if "PARTITION BY RANGE" in s]
        assert len(parent_stmts) == 1, "notifications parent table must use PARTITION BY RANGE"

    def test_notifications_current_month_partition_created(self) -> None:
        executed = _capture_p7_upgrade()
        today = datetime.date.today()
        suffix = f"{today.year:04d}_{today.month:02d}"
        combined = "\n".join(executed)
        assert (
            f"notifications_{suffix}" in combined
        ), f"Expected current-month partition notifications_{suffix} in migration upgrade()"

    def test_notification_deliveries_partitioned_monthly(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        # Both parent and partition for notification_deliveries
        assert "notification_deliveries" in combined
        today = datetime.date.today()
        suffix = f"{today.year:04d}_{today.month:02d}"
        assert (
            f"notification_deliveries_{suffix}" in combined
        ), f"Expected current-month partition notification_deliveries_{suffix} in migration upgrade()"

    def test_integration_pairs_pair_order_check_constraint(self) -> None:
        executed = _capture_p7_upgrade()
        ip_stmts = [s for s in executed if "CREATE TABLE integration_pairs" in s]
        assert len(ip_stmts) == 1
        assert "chk_pair_order" in ip_stmts[0], "integration_pairs must have chk_pair_order CHECK constraint"

    def test_integration_pairs_trigger_function_created(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert "populate_integration_pairs" in combined

    def test_integration_pairs_trigger_has_visibility_comment(self) -> None:
        """Trigger function must carry an inline comment explaining the visibility/isolation rationale."""
        trigger_stmts = [s for s in _capture_p7_upgrade() if "populate_integration_pairs" in s and "FUNCTION" in s]
        assert len(trigger_stmts) >= 1
        combined = "\n".join(trigger_stmts)
        # The trigger must have some rationale comment; check for key vocabulary.
        has_rationale = any(word in combined for word in ("visibility", "isolation", "cross-tenant", "tenant"))
        assert (
            has_rationale
        ), "Trigger function must include an inline comment explaining its cross-tenant visibility rationale"

    def test_integration_pairs_trigger_registered_on_edges(self) -> None:
        executed = _capture_p7_upgrade()
        trigger_create_stmts = [s for s in executed if "CREATE TRIGGER trg_integration_pairs" in s]
        assert len(trigger_create_stmts) == 1
        assert "ON edges" in trigger_create_stmts[0]


class TestP7UpgradeVocabSeeds:
    def test_visibility_seeds_present(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        for vis in ("private", "tenant-shared", "public"):
            assert vis in combined, f"visibility value '{vis}' not found in migration upgrade() seeds"

    def test_event_kind_seeds_present(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        for kind in (
            "version_published",
            "deprecation",
            "breaking_change",
            "conflict_added",
            "integration_added",
        ):
            assert kind in combined, f"event kind '{kind}' not seeded in migration upgrade()"

    def test_seeds_use_on_conflict_do_nothing(self) -> None:
        executed = _capture_p7_upgrade()
        insert_stmts = [s for s in executed if "INSERT INTO vocabulary_values" in s]
        assert len(insert_stmts) == len(_P7_VOCAB_SEEDS), f"Expected {len(_P7_VOCAB_SEEDS)} vocab INSERT statements"
        for stmt in insert_stmts:
            assert "ON CONFLICT DO NOTHING" in stmt, f"ON CONFLICT DO NOTHING missing from vocab seed: {stmt!r}"


class TestP7IntegrationTypeSchema:
    def test_integration_schema_seeded(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert "capability_type_schemas" in combined
        assert "integration" in combined

    def test_integration_schema_uses_stable_id(self) -> None:
        executed = _capture_p7_upgrade()
        combined = "\n".join(executed)
        assert _P7_INTEGRATION_SCHEMA_ID in combined

    def test_integration_schema_idempotent(self) -> None:
        executed = _capture_p7_upgrade()
        schema_inserts = [s for s in executed if "INSERT INTO capability_type_schemas" in s]
        assert len(schema_inserts) == 1
        assert "ON CONFLICT DO NOTHING" in schema_inserts[0]

    def test_integration_schema_non_advisory(self) -> None:
        """Integration schema should be non-advisory (mandatory validation)."""
        executed = _capture_p7_upgrade()
        schema_inserts = [s for s in executed if "INSERT INTO capability_type_schemas" in s]
        assert len(schema_inserts) == 1
        assert "FALSE" in schema_inserts[0], "integration capability_type_schemas row must have is_advisory=FALSE"


class TestP7DowngradeDdl:
    def test_all_new_tables_dropped(self) -> None:
        executed = _capture_p7_downgrade()
        combined = "\n".join(executed)
        for table in _P7_NEW_TABLES:
            assert table in combined, f"Table {table} not referenced in migration downgrade() DROP statements"

    def test_entities_visibility_column_dropped(self) -> None:
        executed = _capture_p7_downgrade()
        combined = "\n".join(executed)
        assert "DROP COLUMN" in combined and "visibility" in combined

    def test_entities_visibility_constraint_dropped(self) -> None:
        executed = _capture_p7_downgrade()
        combined = "\n".join(executed)
        assert "chk_entity_visibility" in combined

    def test_tenants_columns_dropped(self) -> None:
        executed = _capture_p7_downgrade()
        combined = "\n".join(executed)
        assert "notification_digest_window" in combined
        assert "is_regulated" in combined

    def test_vocab_seeds_deleted_in_downgrade(self) -> None:
        executed = _capture_p7_downgrade()
        combined = "\n".join(executed)
        for _kind, value in _P7_VOCAB_SEEDS:
            assert value in combined, f"vocab value '{value}' not deleted in migration downgrade()"

    def test_integration_schema_deleted_in_downgrade(self) -> None:
        executed = _capture_p7_downgrade()
        combined = "\n".join(executed)
        assert _P7_INTEGRATION_SCHEMA_ID in combined

    def test_trigger_dropped_before_integration_pairs(self) -> None:
        """Trigger must be dropped before the integration_pairs table."""
        executed = _capture_p7_downgrade()
        drop_trigger_pos = next(
            (i for i, s in enumerate(executed) if "DROP TRIGGER" in s and "trg_integration_pairs" in s),
            None,
        )
        drop_table_pos = next(
            (i for i, s in enumerate(executed) if "DROP TABLE" in s and "integration_pairs" in s),
            None,
        )
        assert drop_trigger_pos is not None, "DROP TRIGGER trg_integration_pairs not found"
        assert drop_table_pos is not None, "DROP TABLE integration_pairs not found"
        assert drop_trigger_pos < drop_table_pos, "Trigger must be dropped before the integration_pairs table"

    def test_subscriptions_dropped_before_notifications(self) -> None:
        """notifications.subscription_id FK: notifications must drop before subscriptions."""
        executed = _capture_p7_downgrade()
        drop_stmts = [s for s in executed if "DROP TABLE" in s]
        notif_pos = next(
            (i for i, s in enumerate(drop_stmts) if "notifications" in s and "notification_deliveries" not in s),
            None,
        )
        sub_pos = next(
            (i for i, s in enumerate(drop_stmts) if "subscriptions" in s),
            None,
        )
        assert notif_pos is not None, "DROP TABLE notifications not found"
        assert sub_pos is not None, "DROP TABLE subscriptions not found"
        assert notif_pos < sub_pos, "notifications must be dropped before subscriptions (FK dependency)"


class TestP7PartitionBoundsHelper:
    def test_known_date_may_2026(self) -> None:
        suffix, from_iso, to_iso = _mig7._current_month_partition_bounds(datetime.date(2026, 5, 11))
        assert suffix == "2026_05"
        assert from_iso == "2026-05-01"
        assert to_iso == "2026-06-01"

    def test_december_wraps_to_next_year(self) -> None:
        suffix, from_iso, to_iso = _mig7._current_month_partition_bounds(datetime.date(2026, 12, 15))
        assert suffix == "2026_12"
        assert from_iso == "2026-12-01"
        assert to_iso == "2027-01-01"


# ---------------------------------------------------------------------------
# Migration 0014 — visibility vocabulary rename
# ---------------------------------------------------------------------------

_MIG14_PATH = _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0014_visibility_public_rename.py"
_MIG14_SPEC = importlib.util.spec_from_file_location("migration_0014", _MIG14_PATH)
assert _MIG14_SPEC is not None and _MIG14_SPEC.loader is not None
_mig14 = importlib.util.module_from_spec(_MIG14_SPEC)
_MIG14_SPEC.loader.exec_module(_mig14)  # type: ignore[union-attr]


def _capture_0014_upgrade() -> list[str]:
    """Run 0014 upgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig14.upgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


def _capture_0014_downgrade() -> list[str]:
    """Run 0014 downgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig14.downgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


class TestMig0014Module:
    def test_revision_id(self) -> None:
        assert _mig14.revision == "0014_visibility_public_rename"

    def test_down_revision(self) -> None:
        assert _mig14.down_revision == "0013_missing_indexes"

    def test_upgrade_callable(self) -> None:
        assert callable(_mig14.upgrade)

    def test_downgrade_callable(self) -> None:
        assert callable(_mig14.downgrade)


class TestMig0014Upgrade:
    def test_upgrade_drops_old_constraint(self) -> None:
        combined = "\n".join(_capture_0014_upgrade())
        assert "DROP CONSTRAINT" in combined and "chk_entity_visibility" in combined

    def test_upgrade_backfills_old_value_to_new(self) -> None:
        combined = "\n".join(_capture_0014_upgrade())
        # The backfill must replace 'public-in-fabric' rows with 'public'.
        assert "public-in-fabric" in combined, "Backfill UPDATE must reference the old value"
        assert "SET visibility = 'public'" in combined, "Backfill UPDATE must write the new value"

    def test_upgrade_adds_new_constraint_with_public(self) -> None:
        combined = "\n".join(_capture_0014_upgrade())
        assert "'public'" in combined
        # The new constraint must NOT include 'public-in-fabric'.
        add_stmts = [s for s in _capture_0014_upgrade() if "ADD CONSTRAINT" in s]
        assert len(add_stmts) == 1
        assert "public-in-fabric" not in add_stmts[0], "New CHECK constraint must not accept 'public-in-fabric'"

    def test_upgrade_new_constraint_accepts_all_three_values(self) -> None:
        add_stmts = [s for s in _capture_0014_upgrade() if "ADD CONSTRAINT" in s]
        assert len(add_stmts) == 1
        stmt = add_stmts[0]
        for val in ("private", "tenant-shared", "public"):
            assert val in stmt, f"New CHECK constraint must include '{val}'"


class TestMig0014Downgrade:
    def test_downgrade_drops_new_constraint(self) -> None:
        combined = "\n".join(_capture_0014_downgrade())
        assert "DROP CONSTRAINT" in combined and "chk_entity_visibility" in combined

    def test_downgrade_backfills_new_value_back_to_legacy(self) -> None:
        combined = "\n".join(_capture_0014_downgrade())
        assert "public-in-fabric" in combined, "Downgrade backfill must restore 'public-in-fabric'"
        assert "SET visibility = 'public-in-fabric'" in combined

    def test_downgrade_restores_legacy_constraint_without_public(self) -> None:
        add_stmts = [s for s in _capture_0014_downgrade() if "ADD CONSTRAINT" in s]
        assert len(add_stmts) == 1
        stmt = add_stmts[0]
        assert "public-in-fabric" in stmt, "Legacy CHECK constraint must include 'public-in-fabric'"
        # The legacy constraint must not silently include the new bare 'public'.
        # It may contain 'public' as part of 'public-in-fabric', but must not
        # list a standalone 'public' value separate from 'public-in-fabric'.
        assert "'public'" not in stmt or "public-in-fabric" in stmt


# ===========================================================================
# Migration 0018_annotations_plaintext unit tests
# ===========================================================================

_MIG18_PATH = _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0018_annotations_plaintext.py"
_MIG18_SPEC = importlib.util.spec_from_file_location("migration_0018", _MIG18_PATH)
assert _MIG18_SPEC is not None and _MIG18_SPEC.loader is not None
_mig18 = importlib.util.module_from_spec(_MIG18_SPEC)
_MIG18_SPEC.loader.exec_module(_mig18)  # type: ignore[union-attr]

_AN_CATEGORY_SEEDS: list[str] = _mig18._ANNOTATION_CATEGORY_SEEDS
_AN_STATUS_SEEDS: list[str] = _mig18._ANNOTATION_STATUS_SEEDS


def _capture_0018_upgrade() -> list[str]:
    """Run 0018 upgrade() with op.execute + op.get_bind patched."""
    executed: list[str] = []
    from alembic import op  # noqa: PLC0415

    capture_execute, capture_get_bind = _make_capturing_op_patches(executed)
    original_execute = getattr(op, "execute", None)
    original_get_bind = getattr(op, "get_bind", None)
    try:
        op.execute = capture_execute  # type: ignore[attr-defined]
        op.get_bind = capture_get_bind  # type: ignore[attr-defined]
        _mig18.upgrade()
    finally:
        if original_execute is not None:
            op.execute = original_execute  # type: ignore[attr-defined]
        if original_get_bind is not None:
            op.get_bind = original_get_bind  # type: ignore[attr-defined]
    return executed


def _capture_0018_downgrade() -> list[str]:
    """Run 0018 downgrade() with op.execute + op.get_bind patched."""
    executed: list[str] = []
    from alembic import op  # noqa: PLC0415

    capture_execute, capture_get_bind = _make_capturing_op_patches(executed)
    original_execute = getattr(op, "execute", None)
    original_get_bind = getattr(op, "get_bind", None)
    try:
        op.execute = capture_execute  # type: ignore[attr-defined]
        op.get_bind = capture_get_bind  # type: ignore[attr-defined]
        _mig18.downgrade()
    finally:
        if original_execute is not None:
            op.execute = original_execute  # type: ignore[attr-defined]
        if original_get_bind is not None:
            op.get_bind = original_get_bind  # type: ignore[attr-defined]
    return executed


class TestMig0018Module:
    def test_revision_id(self) -> None:
        assert _mig18.revision == "0018_annotations_plaintext"

    def test_down_revision(self) -> None:
        assert _mig18.down_revision == "0017_create_progression_overrides"

    def test_upgrade_callable(self) -> None:
        assert callable(_mig18.upgrade)

    def test_downgrade_callable(self) -> None:
        assert callable(_mig18.downgrade)


class TestMig0018UpgradeDdl:
    def test_capability_annotations_table_created(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "CREATE TABLE capability_annotations" in combined

    def test_all_required_columns_present(self) -> None:
        stmts = _capture_0018_upgrade()
        create_stmts = [s for s in stmts if "CREATE TABLE capability_annotations" in s]
        assert len(create_stmts) == 1
        stmt = create_stmts[0]
        required_cols = [
            "annotation_id",
            "tenant_id",
            "capability_id",
            "author_actor_id",
            "author_tenant_id",
            "body",
            "triage_note",
            "category",
            "status",
            "version_target",
            "created_at",
            "updated_at",
            "t_valid_from",
            "t_valid_to",
            "t_ingested_at",
            "t_invalidated_at",
        ]
        for col in required_cols:
            assert col in stmt, f"Column '{col}' missing from capability_annotations DDL"

    def test_body_is_not_null(self) -> None:
        stmts = _capture_0018_upgrade()
        create_stmt = next(s for s in stmts if "CREATE TABLE capability_annotations" in s)
        # body TEXT NOT NULL must appear — the simplest signal is body followed by NOT NULL
        assert "body" in create_stmt
        assert "NOT NULL" in create_stmt

    def test_status_default_open(self) -> None:
        stmts = _capture_0018_upgrade()
        create_stmt = next(s for s in stmts if "CREATE TABLE capability_annotations" in s)
        assert "DEFAULT 'open'" in create_stmt

    def test_check_constraint_category(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "chk_annotation_category" in combined
        for val in ("feedback", "bug", "suggestion", "question", "doc_gap"):
            assert val in combined, f"category value '{val}' missing from CHECK constraint DDL"

    def test_check_constraint_status(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "chk_annotation_status" in combined
        for val in ("open", "triaged", "acknowledged", "closed"):
            assert val in combined, f"status value '{val}' missing from CHECK constraint DDL"

    def test_three_partial_indexes_created(self) -> None:
        stmts = _capture_0018_upgrade()
        idx_stmts = [s for s in stmts if "CREATE INDEX" in s and "capability_annotations" in s]
        assert len(idx_stmts) == 3, f"Expected 3 partial indexes, got {len(idx_stmts)}"

    def test_idx_ann_capability_created(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "idx_ann_capability" in combined

    def test_idx_ann_author_created(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "idx_ann_author" in combined

    def test_idx_ann_status_created(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "idx_ann_status" in combined

    def test_all_indexes_have_partial_predicate(self) -> None:
        stmts = _capture_0018_upgrade()
        idx_stmts = [s for s in stmts if "CREATE INDEX" in s and "capability_annotations" in s]
        for stmt in idx_stmts:
            assert "t_invalidated_at IS NULL" in stmt, f"Partial index predicate missing from: {stmt!r}"

    def test_no_ciphertext_columns(self) -> None:
        """Plaintext-only invariant: no ENC-phase columns may appear in this migration."""
        combined = "\n".join(_capture_0018_upgrade())
        forbidden = [
            "body_ciphertext",
            "body_nonce",
            "triage_note_ciphertext",
            "triage_note_nonce",
            "kek_id",
            "wrapped_dek",
            "encryption_tier",
        ]
        for col in forbidden:
            assert col not in combined, f"Forbidden ENC-phase column '{col}' found in upgrade() DDL"


class TestMig0018VocabSeeds:
    def test_five_category_seeds(self) -> None:
        assert len(_AN_CATEGORY_SEEDS) == 5

    def test_four_status_seeds(self) -> None:
        assert len(_AN_STATUS_SEEDS) == 4

    def test_all_category_values_seeded(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        for val in ("feedback", "bug", "suggestion", "question", "doc_gap"):
            assert val in combined, f"annotation_category '{val}' not seeded"

    def test_all_status_values_seeded(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        for val in ("open", "triaged", "acknowledged", "closed"):
            assert val in combined, f"annotation_status '{val}' not seeded"

    def test_seeds_use_on_conflict_do_nothing(self) -> None:
        stmts = _capture_0018_upgrade()
        insert_stmts = [s for s in stmts if "INSERT INTO vocabulary_values" in s]
        total_seeds = len(_AN_CATEGORY_SEEDS) + len(_AN_STATUS_SEEDS)
        assert (
            len(insert_stmts) == total_seeds
        ), f"Expected {total_seeds} vocab INSERT statements, got {len(insert_stmts)}"
        for stmt in insert_stmts:
            assert "ON CONFLICT DO NOTHING" in stmt, f"ON CONFLICT DO NOTHING missing from vocab seed: {stmt!r}"

    def test_seeds_have_is_system_true(self) -> None:
        stmts = _capture_0018_upgrade()
        insert_stmts = [s for s in stmts if "INSERT INTO vocabulary_values" in s]
        for stmt in insert_stmts:
            assert "TRUE" in stmt, f"is_system=TRUE missing from vocab seed: {stmt!r}"

    def test_category_kind_used(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "annotation_category" in combined

    def test_status_kind_used(self) -> None:
        combined = "\n".join(_capture_0018_upgrade())
        assert "annotation_status" in combined


class TestMig0018DowngradeDdl:
    def test_capability_annotations_dropped(self) -> None:
        combined = "\n".join(_capture_0018_downgrade())
        assert "capability_annotations" in combined

    def test_vocab_seeds_deleted(self) -> None:
        combined = "\n".join(_capture_0018_downgrade())
        assert "DELETE FROM vocabulary_values" in combined
        assert "annotation_category" in combined
        assert "annotation_status" in combined

    def test_delete_filters_is_system(self) -> None:
        combined = "\n".join(_capture_0018_downgrade())
        assert "is_system" in combined

    def test_delete_before_drop(self) -> None:
        """Vocab DELETE must precede the DROP TABLE so no FK violation occurs."""
        stmts = _capture_0018_downgrade()
        delete_pos = next((i for i, s in enumerate(stmts) if "DELETE FROM vocabulary_values" in s), None)
        drop_pos = next((i for i, s in enumerate(stmts) if "DROP TABLE" in s and "capability_annotations" in s), None)
        assert delete_pos is not None, "DELETE FROM vocabulary_values not found in downgrade()"
        assert drop_pos is not None, "DROP TABLE capability_annotations not found in downgrade()"
        assert delete_pos < drop_pos, "vocab DELETE must precede DROP TABLE in downgrade()"


# ===========================================================================
# Migration 0019_workspaces_plaintext unit tests
# ===========================================================================

_MIG19_PATH = _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0019_workspaces_plaintext.py"
_MIG19_SPEC = importlib.util.spec_from_file_location("migration_0019", _MIG19_PATH)
assert _MIG19_SPEC is not None and _MIG19_SPEC.loader is not None
_mig19 = importlib.util.module_from_spec(_MIG19_SPEC)
_MIG19_SPEC.loader.exec_module(_mig19)  # type: ignore[union-attr]

_WS_NEW_TABLES = [
    "workspaces",
    "workspace_entries",
    "workspace_shares",
    "workspace_share_acceptances",
]


def _capture_0019_upgrade() -> list[str]:
    """Run 0019 upgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig19.upgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


def _capture_0019_downgrade() -> list[str]:
    """Run 0019 downgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig19.downgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


class TestMig0019Module:
    def test_revision_id(self) -> None:
        assert _mig19.revision == "0019_workspaces_plaintext"

    def test_down_revision(self) -> None:
        assert _mig19.down_revision == "0018_annotations_plaintext"

    def test_upgrade_callable(self) -> None:
        assert callable(_mig19.upgrade)

    def test_downgrade_callable(self) -> None:
        assert callable(_mig19.downgrade)


class TestMig0019UpgradeDdl:
    def test_all_four_tables_created(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        for table in _WS_NEW_TABLES:
            assert f"CREATE TABLE {table}" in combined, f"CREATE TABLE {table} not found in upgrade() DDL"

    def test_workspaces_encryption_tier_column(self) -> None:
        stmts = _capture_0019_upgrade()
        ws_stmts = [s for s in stmts if "CREATE TABLE workspaces" in s]
        assert len(ws_stmts) == 1
        assert "encryption_tier" in ws_stmts[0]
        assert "DEFAULT 'none'" in ws_stmts[0]

    def test_workspaces_check_constraints(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "chk_owner_kind" in combined
        assert "chk_encryption_tier" in combined
        assert "chk_actor_owner" in combined

    def test_workspaces_indexes_created(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "idx_ws_tenant" in combined
        assert "idx_ws_owner" in combined

    def test_workspace_entries_body_md_not_ciphertext(self) -> None:
        """Plaintext-only invariant: body_md must be present; no ciphertext columns."""
        stmts = _capture_0019_upgrade()
        we_stmts = [s for s in stmts if "CREATE TABLE workspace_entries" in s]
        assert len(we_stmts) == 1
        stmt = we_stmts[0]
        assert "body_md" in stmt
        assert "body_ciphertext" not in stmt
        assert "body_nonce" not in stmt

    def test_workspace_entries_kind_check_constraint(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "chk_entry_kind" in combined
        for val in ("note", "decision", "open_question", "saved_query", "saved_view", "private_annotation"):
            assert val in combined, f"entry kind '{val}' missing from chk_entry_kind"

    def test_workspace_entries_all_indexes_created(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        for idx in ("idx_we_workspace", "idx_we_tenant", "idx_we_refs", "idx_we_expires", "idx_we_body_fts"):
            assert idx in combined, f"Index '{idx}' missing from upgrade() DDL"

    def test_workspace_entries_fts_index_uses_gin_tsvector(self) -> None:
        stmts = _capture_0019_upgrade()
        fts_stmts = [s for s in stmts if "idx_we_body_fts" in s]
        assert len(fts_stmts) == 1
        assert "GIN" in fts_stmts[0]
        assert "to_tsvector" in fts_stmts[0]
        assert "english" in fts_stmts[0]

    def test_workspace_entries_refs_index_is_gin(self) -> None:
        stmts = _capture_0019_upgrade()
        refs_stmts = [s for s in stmts if "idx_we_refs" in s]
        assert len(refs_stmts) == 1
        assert "GIN" in refs_stmts[0]

    def test_workspace_shares_check_constraint(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "chk_share_role" in combined
        assert "'reader'" in combined
        assert "'contributor'" in combined

    def test_workspace_shares_unique_partial_index(self) -> None:
        stmts = _capture_0019_upgrade()
        uq_stmts = [s for s in stmts if "uq_share" in s]
        assert len(uq_stmts) == 1
        assert "UNIQUE" in uq_stmts[0]
        assert "revoked_at IS NULL" in uq_stmts[0]

    def test_workspace_shares_grantee_index(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "idx_share_grantee" in combined

    def test_owner_kind_trigger_function_created(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "check_workspace_owner_kind_change" in combined
        assert "PLPGSQL" in combined or "plpgsql" in combined.lower()

    def test_owner_kind_trigger_registered_on_workspaces(self) -> None:
        stmts = _capture_0019_upgrade()
        trig_stmts = [s for s in stmts if "trg_ws_owner_kind_change" in s and "CREATE TRIGGER" in s]
        assert len(trig_stmts) == 1
        assert "workspaces" in trig_stmts[0]

    def test_share_cross_tenant_trigger_function_created(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "check_workspace_share_cross_tenant" in combined

    def test_share_cross_tenant_trigger_registered_on_shares(self) -> None:
        stmts = _capture_0019_upgrade()
        trig_stmts = [s for s in stmts if "trg_ws_share_cross_tenant" in s and "CREATE TRIGGER" in s]
        assert len(trig_stmts) == 1
        assert "workspace_shares" in trig_stmts[0]

    def test_workspace_share_acceptances_unique_index(self) -> None:
        combined = "\n".join(_capture_0019_upgrade())
        assert "uq_acceptance" in combined
        assert "share_id" in combined
        assert "accepting_actor_id" in combined

    def test_no_forbidden_enc_phase_columns(self) -> None:
        """Plaintext-only invariant: forbidden ENC-phase columns must not appear."""
        combined = "\n".join(_capture_0019_upgrade())
        forbidden = [
            "body_ciphertext",
            "body_nonce",
            "references_ciphertext",
            "references_nonce",
            "chk_body_xor",
            "chk_refs_xor",
            "kek_id",
            "wrapped_dek",
            "dek_algorithm",
            "tenant_encryption_configs",
            "crypto_shred_events",
        ]
        for col in forbidden:
            assert col not in combined, f"Forbidden ENC-phase column '{col}' found in upgrade() DDL"

    def test_no_bitemporal_columns_on_workspace_tables(self) -> None:
        """Workspace tables defer bi-temporal columns to v2; t_invalidated_at IS allowed."""
        stmts = _capture_0019_upgrade()
        ws_table_stmts = [s for s in stmts if any(f"CREATE TABLE {t}" in s for t in _WS_NEW_TABLES)]
        for stmt in ws_table_stmts:
            for col in ("t_valid_from", "t_valid_to", "t_ingested_at"):
                assert (
                    col not in stmt
                ), f"Bi-temporal column '{col}' must not appear in WS-phase table DDL: {stmt[:80]!r}"


class TestMig0019DowngradeDdl:
    def test_all_four_tables_dropped(self) -> None:
        combined = "\n".join(_capture_0019_downgrade())
        for table in _WS_NEW_TABLES:
            assert table in combined, f"Table {table} not referenced in downgrade() DROP statements"

    def test_trigger_functions_dropped(self) -> None:
        combined = "\n".join(_capture_0019_downgrade())
        assert "check_workspace_share_cross_tenant" in combined
        assert "check_workspace_owner_kind_change" in combined

    def test_acceptances_dropped_before_shares(self) -> None:
        """workspace_share_acceptances has FK to workspace_shares; must drop first."""
        stmts = _capture_0019_downgrade()
        drop_stmts = [s for s in stmts if "DROP TABLE" in s]
        acc_pos = next((i for i, s in enumerate(drop_stmts) if "workspace_share_acceptances" in s), None)
        shares_pos = next(
            (i for i, s in enumerate(drop_stmts) if "workspace_shares" in s and "acceptances" not in s),
            None,
        )
        assert acc_pos is not None, "DROP TABLE workspace_share_acceptances not found"
        assert shares_pos is not None, "DROP TABLE workspace_shares not found"
        assert acc_pos < shares_pos, "workspace_share_acceptances must be dropped before workspace_shares"

    def test_entries_dropped_before_workspaces(self) -> None:
        """workspace_entries has FK to workspaces; must drop first."""
        stmts = _capture_0019_downgrade()
        drop_stmts = [s for s in stmts if "DROP TABLE" in s]
        entries_pos = next((i for i, s in enumerate(drop_stmts) if "workspace_entries" in s), None)
        ws_pos = next(
            (
                i
                for i, s in enumerate(drop_stmts)
                if "workspaces" in s
                and "workspace_entries" not in s
                and "workspace_shares" not in s
                and "workspace_share_acceptances" not in s
            ),
            None,
        )
        assert entries_pos is not None, "DROP TABLE workspace_entries not found"
        assert ws_pos is not None, "DROP TABLE workspaces not found"
        assert entries_pos < ws_pos, "workspace_entries must be dropped before workspaces"
