"""Unit tests — migration module importability and revision-chain structure.

Verifies that migration modules are importable without a live DB connection
and carry correct revision/down_revision metadata with callable upgrade/downgrade
hooks. This guards against accidental breakage of the Alembic revision chain.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock, patch


def test_rbac_oidc_migration_importable() -> None:
    """0005_phase4_rbac_oidc module must be importable without a DB connection."""
    mod = importlib.import_module("catalog.storage.migrations.versions.0005_phase4_rbac_oidc")
    assert mod.revision == "0005_phase4_rbac_oidc"
    assert mod.down_revision == "0004_phase3_sync_infra"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


# ---------------------------------------------------------------------------
# SQL-parameterization guards for the three migrations that used f-string SQL.
# Each test patches op.get_bind() to return a recording connection, runs the
# migration function directly, and asserts that every SQL string contains
# named placeholders and no Python-interpolated user-controlled values.
# ---------------------------------------------------------------------------


def _captured_sql_and_params(bind: MagicMock) -> list[tuple[str, dict[str, Any]]]:
    """Pull (sql_string, bind_params) tuples out of a MagicMock connection."""
    captured: list[tuple[str, dict[str, Any]]] = []
    for call in bind.execute.call_args_list:
        text_obj = call.args[0]
        params: dict[str, Any] = (
            call.args[1] if len(call.args) > 1 else (call.kwargs.get("parameters") or {})
        )
        captured.append((str(text_obj), params))
    return captured


def test_mig0018_upgrade_does_not_interpolate_user_controlled_data() -> None:
    """0018 vocabulary seed inserts must use :tid / :kind / :value placeholders."""
    mod = importlib.import_module(
        "registry.storage.migrations.versions.0018_annotations_plaintext"
    )

    bind = MagicMock()
    with (
        patch.object(mod.op, "get_bind", return_value=bind),
        patch.object(mod.op, "execute"),
        patch.object(mod.op, "create_table", create=True),
        patch.object(mod.op, "create_index", create=True),
    ):
        mod.upgrade()

    captured = _captured_sql_and_params(bind)
    seeds = [(s, p) for s, p in captured if "INSERT INTO vocabulary_values" in s]
    assert seeds, "expected at least one parameterized vocabulary INSERT via bind.execute"

    for sql_text, params in seeds:
        assert ":tid" in sql_text and ":kind" in sql_text and ":value" in sql_text, (
            f"missing named placeholder in SQL: {sql_text}"
        )
        assert "'annotation_category'" not in sql_text, sql_text
        assert "'annotation_status'" not in sql_text, sql_text
        assert params.get("kind") in {"annotation_category", "annotation_status"}, params
        assert "value" in params and isinstance(params["value"], str)


def test_mig0007_downgrade_sql_is_parameterized() -> None:
    """0007 downgrade DELETEs must bind ids as a list and seeds as named params."""
    mod = importlib.import_module(
        "registry.storage.migrations.versions.0007_phase6_graph_primitives"
    )

    bind = MagicMock()
    with (
        patch.object(mod.op, "get_bind", return_value=bind),
        patch.object(mod.op, "execute"),
        patch.object(mod.op, "drop_table", create=True),
        patch.object(mod.op, "drop_index", create=True),
    ):
        mod.downgrade()

    captured = _captured_sql_and_params(bind)

    pii_deletes = [(s, p) for s, p in captured if "DELETE FROM pii_patterns" in s]
    assert pii_deletes, "expected a parameterized pii_patterns DELETE"
    for sql_text, params in pii_deletes:
        assert ":ids" in sql_text, sql_text
        assert "pattern_ids_csv" not in sql_text
        ids = params.get("ids")
        assert isinstance(ids, list), f"ids must be a Python list; got {type(ids).__name__}"

    vocab_deletes = [(s, p) for s, p in captured if "DELETE FROM vocabulary_values" in s]
    assert vocab_deletes, "expected at least one vocabulary_values DELETE"
    for sql_text, _ in vocab_deletes:
        assert ":tid" in sql_text and ":kind" in sql_text and ":value" in sql_text


def test_mig0009_downgrade_sql_is_parameterized() -> None:
    """0009 downgrade DELETEs must use named placeholders for kind/value/schema_id."""
    mod = importlib.import_module(
        "registry.storage.migrations.versions.0009_phase7_provider_consumer"
    )

    bind = MagicMock()
    with (
        patch.object(mod.op, "get_bind", return_value=bind),
        patch.object(mod.op, "execute"),
        patch.object(mod.op, "drop_table", create=True),
    ):
        mod.downgrade()

    captured = _captured_sql_and_params(bind)

    schema_deletes = [(s, p) for s, p in captured if "DELETE FROM capability_type_schemas" in s]
    assert schema_deletes, "expected a parameterized capability_type_schemas DELETE"
    for sql_text, _ in schema_deletes:
        assert ":schema_id" in sql_text, sql_text

    vocab_deletes = [(s, p) for s, p in captured if "DELETE FROM vocabulary_values" in s]
    assert vocab_deletes, "expected vocabulary_values DELETEs"
    for sql_text, _ in vocab_deletes:
        assert ":tid" in sql_text and ":kind" in sql_text and ":value" in sql_text, sql_text
        for forbidden in ("'integration'",):
            assert forbidden not in sql_text, f"{forbidden} leaked into SQL: {sql_text}"
