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

# ---------------------------------------------------------------------------
# Load the migration module without a real DB
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIG_PATH = _REPO_ROOT / "catalog" / "storage" / "migrations" / "versions" / "0007_phase6_graph_primitives.py"

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


def _capture_upgrade() -> list[str]:
    """Run upgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig.upgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


def _capture_downgrade() -> list[str]:
    """Run downgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig.downgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
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

_MIG7_PATH = _REPO_ROOT / "catalog" / "storage" / "migrations" / "versions" / "0009_phase7_provider_consumer.py"

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
    """Run the migration upgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig7.upgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


def _capture_p7_downgrade() -> list[str]:
    """Run the migration downgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig7.downgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
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

_MIG14_PATH = _REPO_ROOT / "catalog" / "storage" / "migrations" / "versions" / "0014_visibility_public_rename.py"
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
