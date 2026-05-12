"""Unit tests — partition_migrate.py (mock DB).

Covers:
- month_range generation (boundary conditions)
- partition_name helper
- rename_sql composition
- idempotency check (archive table present → early exit)
- resume detection (chunk already in _new → skip copy)
- _migrate_range_table delegates correctly under various DB states
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import the module under test (no psycopg2 required at import time)
# ---------------------------------------------------------------------------

_MOD_PATH = "scripts.partition_migrate"

# Provide a stub psycopg2 so the module can be imported in environments
# where psycopg2-binary is not installed.
if "psycopg2" not in sys.modules:
    _stub = types.ModuleType("psycopg2")
    _stub.connect = MagicMock()  # type: ignore[attr-defined]
    sys.modules["psycopg2"] = _stub

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    _MOD_PATH,
    Path(__file__).parent.parent.parent / "scripts" / "partition_migrate.py",
)
assert _spec is not None and _spec.loader is not None
_pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pm)  # type: ignore[union-attr]

month_range = _pm.month_range
partition_name = _pm.partition_name
rename_sql = _pm.rename_sql
run_migration = _pm.run_migration
_migrate_range_table = _pm._migrate_range_table
_table_exists = _pm._table_exists
_chunk_row_count = _pm._chunk_row_count
_copy_chunk = _pm._copy_chunk
_transactional_rename = _pm._transactional_rename


# ---------------------------------------------------------------------------
# month_range
# ---------------------------------------------------------------------------


class TestMonthRange:
    def test_single_month(self) -> None:
        result = list(month_range(datetime.date(2026, 3, 15), datetime.date(2026, 4, 1)))
        assert result == [(datetime.date(2026, 3, 1), datetime.date(2026, 4, 1))]

    def test_truncates_start_to_first_of_month(self) -> None:
        result = list(month_range(datetime.date(2026, 3, 20), datetime.date(2026, 5, 1)))
        assert result[0] == (datetime.date(2026, 3, 1), datetime.date(2026, 4, 1))
        assert len(result) == 2

    def test_year_boundary(self) -> None:
        result = list(month_range(datetime.date(2025, 11, 1), datetime.date(2026, 2, 1)))
        assert len(result) == 3
        assert result[0] == (datetime.date(2025, 11, 1), datetime.date(2025, 12, 1))
        assert result[1] == (datetime.date(2025, 12, 1), datetime.date(2026, 1, 1))
        assert result[2] == (datetime.date(2026, 1, 1), datetime.date(2026, 2, 1))

    def test_empty_when_start_gte_end(self) -> None:
        result = list(month_range(datetime.date(2026, 5, 1), datetime.date(2026, 5, 1)))
        assert result == []

    def test_twelve_months_forward(self) -> None:
        start = datetime.date(2026, 5, 1)
        end = datetime.date(2027, 5, 1)
        result = list(month_range(start, end))
        assert len(result) == 12
        assert result[-1] == (datetime.date(2027, 4, 1), datetime.date(2027, 5, 1))


# ---------------------------------------------------------------------------
# partition_name
# ---------------------------------------------------------------------------


class TestPartitionName:
    def test_basic(self) -> None:
        assert partition_name("audit_log_new", datetime.date(2026, 5, 1)) == "audit_log_new_2026_05"

    def test_zero_pad_month(self) -> None:
        assert partition_name("episodes_new", datetime.date(2025, 1, 15)) == "episodes_new_2025_01"

    def test_december(self) -> None:
        assert partition_name("audit_log_new", datetime.date(2025, 12, 1)) == "audit_log_new_2025_12"


# ---------------------------------------------------------------------------
# rename_sql
# ---------------------------------------------------------------------------


class TestRenameSql:
    def test_audit_log(self) -> None:
        archive, promote = rename_sql("audit_log")
        assert archive == "ALTER TABLE audit_log RENAME TO audit_log_archive"
        assert promote == "ALTER TABLE audit_log_new RENAME TO audit_log"

    def test_episodes(self) -> None:
        archive, promote = rename_sql("episodes")
        assert archive == "ALTER TABLE episodes RENAME TO episodes_archive"
        assert promote == "ALTER TABLE episodes_new RENAME TO episodes"

    def test_embeddings(self) -> None:
        archive, promote = rename_sql("embeddings")
        assert archive == "ALTER TABLE embeddings RENAME TO embeddings_archive"
        assert promote == "ALTER TABLE embeddings_new RENAME TO embeddings"


# ---------------------------------------------------------------------------
# Idempotency check — archive table already present
# ---------------------------------------------------------------------------


def _make_conn(
    *,
    archive_exists: bool = False,
    new_table_exists: bool = True,
    min_ts: datetime.datetime | None = None,
    max_ts: datetime.datetime | None = None,
    chunk_count: int = 0,
    child_partitions: set[str] | None = None,
) -> MagicMock:
    """Build a mock psycopg2 connection wired to common patterns."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur

    def execute_side_effect(sql: str, params: object = None) -> None:
        pass  # captured via cur.execute calls

    cur.execute.side_effect = execute_side_effect

    # Use a list-based response queue for fetchone / fetchall.
    _responses: list[object] = []

    def _fetchone() -> object:
        return _responses.pop(0) if _responses else None

    def _fetchall() -> list[object]:
        result = _responses.pop(0) if _responses else []
        return result if isinstance(result, list) else [result]

    cur.fetchone.side_effect = _fetchone
    cur.fetchall.side_effect = _fetchall
    conn._response_queue = _responses
    return conn


class TestIdempotency:
    def test_skips_when_archive_exists(self, caplog: pytest.LogCaptureFixture) -> None:
        """If audit_log_archive exists, _migrate_range_table should exit early."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        # _table_exists checks pg_tables; return a row (truthy) for archive table
        cur.fetchone.return_value = (1,)

        import logging

        with caplog.at_level(logging.WARNING):
            _migrate_range_table(conn, "audit_log", datetime.date(2026, 5, 7), dry_run=True)

        assert "cutover already done" in caplog.text
        # No RENAME should have been issued
        rename_calls = [c for c in cur.execute.call_args_list if "RENAME" in str(c)]
        assert rename_calls == []

    def test_run_migration_skips_all_when_archives_exist(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # All archive tables exist
        cur.fetchone.return_value = (1,)

        import logging

        with caplog.at_level(logging.WARNING):
            run_migration(conn, dry_run=True)

        text = caplog.text
        assert "cutover already done" in text


# ---------------------------------------------------------------------------
# Resume detection — chunk already in _new
# ---------------------------------------------------------------------------


class TestResumeDetection:
    def test_chunk_skipped_when_count_positive(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # COUNT(*) returns > 0 → already copied
        cur.fetchone.return_value = (42,)

        import logging

        with caplog.at_level(logging.INFO):
            result = _copy_chunk(
                conn,
                "audit_log",
                "audit_log_new",
                datetime.date(2026, 4, 1),
                datetime.date(2026, 5, 1),
                dry_run=False,
            )

        assert result == 0
        # No INSERT should have been issued
        insert_calls = [c for c in cur.execute.call_args_list if "INSERT" in str(c)]
        assert insert_calls == []
        assert "RESUME" in caplog.text

    def test_chunk_copied_when_count_zero(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # COUNT(*) = 0 → needs copy; rowcount after INSERT = 5
        cur.fetchone.side_effect = [(0,), None]
        cur.rowcount = 5

        result = _copy_chunk(
            conn,
            "audit_log",
            "audit_log_new",
            datetime.date(2026, 4, 1),
            datetime.date(2026, 5, 1),
            dry_run=False,
        )

        assert result == 5
        insert_calls = [c for c in cur.execute.call_args_list if "INSERT" in str(c)]
        assert len(insert_calls) == 1


# ---------------------------------------------------------------------------
# rename_sql composition in transactional_rename
# ---------------------------------------------------------------------------


class TestTransactionalRename:
    def test_dry_run_does_not_execute_rename(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        _transactional_rename(conn, "audit_log", dry_run=True)

        rename_calls = [c for c in cur.execute.call_args_list if "RENAME" in str(c)]
        assert rename_calls == []

    def test_rename_sql_in_transaction(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        _transactional_rename(conn, "audit_log", dry_run=False)

        executed = [str(c.args[0]) for c in cur.execute.call_args_list]
        assert any("BEGIN" in s for s in executed)
        assert any("audit_log RENAME TO audit_log_archive" in s for s in executed)
        assert any("audit_log_new RENAME TO audit_log" in s for s in executed)
        assert any("COMMIT" in s for s in executed)
        # ORDER: BEGIN before renames before COMMIT
        begin_idx = next(i for i, s in enumerate(executed) if "BEGIN" in s)
        commit_idx = next(i for i, s in enumerate(executed) if "COMMIT" in s)
        assert begin_idx < commit_idx
