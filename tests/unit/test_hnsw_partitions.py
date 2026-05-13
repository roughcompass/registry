"""Unit tests — per-partition HNSW index for embeddings.

Covers:
- 0006_phase5_partitions migration emits HNSW DDL for all 8 buckets
- partition_migrate._ensure_hnsw_indexes: idempotency, dry-run, index creation
- ORM Embedding model still loads (tablename unchanged, mapping intact)
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Bootstrap — stub psycopg2 so partition_migrate can be imported without it
# ---------------------------------------------------------------------------

if "psycopg2" not in sys.modules:
    _stub = types.ModuleType("psycopg2")
    _stub.connect = MagicMock()  # type: ignore[attr-defined]
    sys.modules["psycopg2"] = _stub

_REPO_ROOT = Path(__file__).parent.parent.parent

# Load partition_migrate without requiring psycopg2 at import time
_PM_SPEC = importlib.util.spec_from_file_location(
    "scripts.partition_migrate",
    _REPO_ROOT / "scripts" / "partition_migrate.py",
)
assert _PM_SPEC is not None and _PM_SPEC.loader is not None
_pm = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(_pm)  # type: ignore[union-attr]

_ensure_hnsw_indexes = _pm._ensure_hnsw_indexes
_hnsw_index_name = _pm._hnsw_index_name
_EMBEDDINGS_HASH_BUCKETS: int = _pm._EMBEDDINGS_HASH_BUCKETS
_HNSW_M: int = _pm._HNSW_M
_HNSW_EF_CONSTRUCTION: int = _pm._HNSW_EF_CONSTRUCTION

# Load 0006_phase5_partitions migration module (leading digit prevents normal import)
_MIG_SPEC = importlib.util.spec_from_file_location(
    "migration_0006",
    _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0006_phase5_partitions.py",
)
assert _MIG_SPEC is not None and _MIG_SPEC.loader is not None
_mig = importlib.util.module_from_spec(_MIG_SPEC)
_MIG_SPEC.loader.exec_module(_mig)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Migration DDL — 0006_phase5_partitions
# ---------------------------------------------------------------------------


class TestMigrationHnswDdl:
    """Verify the DDL template and that upgrade() emits HNSW statements."""

    def test_hnsw_index_template_contains_all_8_buckets(self) -> None:
        """_EMBEDDINGS_HNSW_INDEX_TEMPLATE must be formattable for n in 0..7."""
        template: str = _mig._EMBEDDINGS_HNSW_INDEX_TEMPLATE
        for n in range(8):
            ddl = template.format(n=n)
            assert f"embeddings_new_p{n}" in ddl, f"partition name missing for n={n}"
            assert "hnsw" in ddl.lower(), "USING hnsw missing"
            assert "vector_cosine_ops" in ddl, "vector_cosine_ops missing"
            assert "m = 16" in ddl, "m=16 missing"
            assert "ef_construction = 64" in ddl, "ef_construction=64 missing"

    def test_upgrade_calls_op_execute_for_hnsw_indexes(self) -> None:
        """upgrade() must issue one HNSW CREATE INDEX per partition (8 total)."""
        from alembic import op  # noqa: PLC0415

        executed: list[str] = []

        def capture(sql: str) -> None:
            executed.append(sql)

        # Patch op.execute to capture DDL strings without a real DB.
        original_execute = getattr(op, "execute", None)
        try:
            op.execute = capture  # type: ignore[attr-defined]
            _mig.upgrade()
        finally:
            if original_execute is not None:
                op.execute = original_execute  # type: ignore[attr-defined]

        hnsw_stmts = [s for s in executed if "hnsw" in s.lower()]
        assert len(hnsw_stmts) == 8, f"Expected 8 HNSW statements, got {len(hnsw_stmts)}"
        for n in range(8):
            expected_table = f"embeddings_new_p{n}"
            assert any(expected_table in s for s in hnsw_stmts), f"No HNSW statement for {expected_table}"


# ---------------------------------------------------------------------------
# partition_migrate._ensure_hnsw_indexes
# ---------------------------------------------------------------------------


def _make_conn(*, index_exists: bool = False) -> MagicMock:
    """Build a mock psycopg2 connection for HNSW index tests."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    # pg_class check: return a row if index exists, None otherwise
    cur.fetchone.return_value = (1,) if index_exists else None
    cur.rowcount = 0
    return conn


class TestEnsureHnswIndexes:
    def test_creates_8_indexes_when_none_exist(self) -> None:
        conn = _make_conn(index_exists=False)
        _ensure_hnsw_indexes(conn, dry_run=False)

        # Count CREATE INDEX calls (non-SELECT execute calls)
        create_calls = [
            c
            for c in conn.cursor().execute.call_args_list
            if isinstance(c.args[0], str) and "CREATE INDEX" in c.args[0]
        ]
        assert len(create_calls) == 8

    def test_index_names_match_expected_pattern(self) -> None:
        conn = _make_conn(index_exists=False)
        _ensure_hnsw_indexes(conn, dry_run=False)

        create_calls = [
            c.args[0]
            for c in conn.cursor().execute.call_args_list
            if isinstance(c.args[0], str) and "CREATE INDEX" in c.args[0]
        ]
        for n in range(8):
            expected = _hnsw_index_name(n)
            assert any(expected in s for s in create_calls), f"Index {expected} not in CREATE INDEX calls"

    def test_dry_run_does_not_execute_create(self) -> None:
        conn = _make_conn(index_exists=False)
        _ensure_hnsw_indexes(conn, dry_run=True)

        create_calls = [
            c
            for c in conn.cursor().execute.call_args_list
            if isinstance(c.args[0], str) and "CREATE INDEX" in c.args[0]
        ]
        assert create_calls == []

    def test_skips_existing_indexes(self) -> None:
        """When pg_class returns a row (index exists), no CREATE INDEX is issued."""
        conn = _make_conn(index_exists=True)
        _ensure_hnsw_indexes(conn, dry_run=False)

        create_calls = [
            c
            for c in conn.cursor().execute.call_args_list
            if isinstance(c.args[0], str) and "CREATE INDEX" in c.args[0]
        ]
        assert create_calls == []

    def test_commit_called_once_per_created_index(self) -> None:
        conn = _make_conn(index_exists=False)
        _ensure_hnsw_indexes(conn, dry_run=False)
        # One commit per new index
        assert conn.commit.call_count == 8

    def test_hnsw_params_in_ddl(self) -> None:
        conn = _make_conn(index_exists=False)
        _ensure_hnsw_indexes(conn, dry_run=False)

        create_calls = [
            c.args[0]
            for c in conn.cursor().execute.call_args_list
            if isinstance(c.args[0], str) and "CREATE INDEX" in c.args[0]
        ]
        for ddl in create_calls:
            assert f"m = {_HNSW_M}" in ddl, f"m param missing in: {ddl}"
            assert f"ef_construction = {_HNSW_EF_CONSTRUCTION}" in ddl, f"ef_construction param missing in: {ddl}"
            assert "vector_cosine_ops" in ddl, f"vector_cosine_ops missing in: {ddl}"


# ---------------------------------------------------------------------------
# ORM model — Embedding still loads cleanly after hash partitioning
# ---------------------------------------------------------------------------


class TestEmbeddingModelIntegrity:
    def test_embedding_tablename_unchanged(self) -> None:
        from registry.storage.models import Embedding  # noqa: PLC0415

        assert Embedding.__tablename__ == "embeddings"

    def test_embedding_has_tenant_id_column(self) -> None:
        from sqlalchemy import inspect  # noqa: PLC0415

        from registry.storage.models import Embedding  # noqa: PLC0415

        mapper = inspect(Embedding)
        column_names = {c.key for c in mapper.columns}
        assert "tenant_id" in column_names

    def test_embedding_has_vector_column(self) -> None:
        from sqlalchemy import inspect  # noqa: PLC0415

        from registry.storage.models import Embedding  # noqa: PLC0415

        mapper = inspect(Embedding)
        column_names = {c.key for c in mapper.columns}
        assert "vector" in column_names

    def test_embedding_hnsw_note_present(self) -> None:
        from registry.storage.models import Embedding  # noqa: PLC0415

        assert "PARTITION BY HASH" in (
            Embedding.__doc__ or ""
        ), "Partition note missing from Embedding docstring — Embedding must document its HASH partitioning"
