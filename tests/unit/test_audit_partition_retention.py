"""Unit tests — audit_log partition retention check.

Covers:
- audit_partitions_eligible_for_archival predicate (pure function)
- check_audit_partition_ages: gauge update and WARNING emission
- Edge cases: no partitions, all recent, mixed, year boundary
"""

from __future__ import annotations

import datetime
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.main import (
    _AUDIT_ARCHIVAL_GAUGE,
    audit_partitions_eligible_for_archival,
    check_audit_partition_ages,
)

# ---------------------------------------------------------------------------
# audit_partitions_eligible_for_archival (pure predicate)
# ---------------------------------------------------------------------------


class TestEligibleForArchival:
    def test_empty_list_returns_empty(self) -> None:
        assert audit_partitions_eligible_for_archival([], datetime.date(2026, 5, 1)) == []

    def test_partition_exactly_at_24_months_is_not_eligible(self) -> None:
        # reference = 2026-05-01; cutoff = 2024-05-01; 2024-05 is NOT < cutoff
        result = audit_partitions_eligible_for_archival(
            ["audit_log_2024_05"],
            reference_date=datetime.date(2026, 5, 1),
        )
        assert result == []

    def test_partition_one_month_before_cutoff_is_eligible(self) -> None:
        # reference = 2026-05-01; cutoff = 2024-05-01; 2024-04 < cutoff
        result = audit_partitions_eligible_for_archival(
            ["audit_log_2024_04"],
            reference_date=datetime.date(2026, 5, 1),
        )
        assert result == ["audit_log_2024_04"]

    def test_all_old_partitions_returned(self) -> None:
        names = [
            "audit_log_2022_01",
            "audit_log_2022_12",
            "audit_log_2023_06",
        ]
        result = audit_partitions_eligible_for_archival(
            names,
            reference_date=datetime.date(2026, 5, 1),
        )
        assert result == sorted(names)

    def test_mixed_old_and_recent(self) -> None:
        names = [
            "audit_log_2023_12",  # old (> 24 mo from 2026-05)
            "audit_log_2025_01",  # recent (< 24 mo)
            "audit_log_2026_04",  # current month minus 1
        ]
        result = audit_partitions_eligible_for_archival(
            names,
            reference_date=datetime.date(2026, 5, 1),
        )
        assert result == ["audit_log_2023_12"]

    def test_unrecognised_names_skipped(self) -> None:
        names = [
            "audit_log_new",
            "audit_log_archive",
            "audit_log_2022_03",
        ]
        result = audit_partitions_eligible_for_archival(
            names,
            reference_date=datetime.date(2026, 5, 1),
        )
        assert result == ["audit_log_2022_03"]

    def test_year_boundary_cutoff(self) -> None:
        # reference = 2026-02-01 → cutoff = 2024-02-01
        result = audit_partitions_eligible_for_archival(
            ["audit_log_2024_01", "audit_log_2024_02"],
            reference_date=datetime.date(2026, 2, 1),
        )
        # 2024-01 < 2024-02 (cutoff) → eligible; 2024-02 is exactly cutoff → not eligible
        assert result == ["audit_log_2024_01"]

    def test_result_is_sorted(self) -> None:
        names = ["audit_log_2022_06", "audit_log_2021_12", "audit_log_2022_01"]
        result = audit_partitions_eligible_for_archival(
            names,
            reference_date=datetime.date(2026, 5, 1),
        )
        assert result == sorted(result)

    def test_custom_retention_months(self) -> None:
        # 12-month retention; reference = 2026-05-01; cutoff = 2025-05-01
        result = audit_partitions_eligible_for_archival(
            ["audit_log_2025_04", "audit_log_2025_05"],
            reference_date=datetime.date(2026, 5, 1),
            retention_months=12,
        )
        assert result == ["audit_log_2025_04"]


# ---------------------------------------------------------------------------
# check_audit_partition_ages — gauge and logging
# ---------------------------------------------------------------------------


class TestCheckAuditPartitionAges:
    def _make_session_factory(self, partition_names: list[str]) -> MagicMock:
        """Return a mock session_factory whose async engine returns given names."""
        sf = MagicMock()
        bind = MagicMock()
        sf.kw = {"bind": bind}

        # bind.connect() returns an async context manager wrapping an async conn.
        conn = MagicMock()
        result = MagicMock()
        result.fetchall.return_value = [(name,) for name in partition_names]
        conn.execute = AsyncMock(return_value=result)

        conn_ctx = MagicMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
        bind.connect = MagicMock(return_value=conn_ctx)
        return sf

    @pytest.mark.asyncio
    async def test_gauge_zero_when_no_partitions(self) -> None:
        sf = self._make_session_factory([])
        _AUDIT_ARCHIVAL_GAUGE.set(-1)  # sentinel
        await check_audit_partition_ages(session_factory=sf)
        assert _AUDIT_ARCHIVAL_GAUGE._value.get() == 0.0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_gauge_set_to_count_of_eligible(self) -> None:
        sf = self._make_session_factory(["audit_log_2022_01", "audit_log_2022_06", "audit_log_2025_04"])
        with patch(
            "catalog.main.audit_partitions_eligible_for_archival",
            return_value=["audit_log_2022_01", "audit_log_2022_06"],
        ) as mock_pred:
            await check_audit_partition_ages(session_factory=sf)
            assert mock_pred.called

        assert _AUDIT_ARCHIVAL_GAUGE._value.get() == 2.0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_warning_emitted_when_eligible(self, caplog: pytest.LogCaptureFixture) -> None:
        sf = self._make_session_factory(["audit_log_2021_03"])
        with caplog.at_level(logging.WARNING, logger="catalog.main"):
            with patch(
                "catalog.main.audit_partitions_eligible_for_archival",
                return_value=["audit_log_2021_03"],
            ):
                await check_audit_partition_ages(session_factory=sf)

        assert "audit_log_2021_03" in caplog.text
        assert "runbook-ops.md" in caplog.text

    @pytest.mark.asyncio
    async def test_no_warning_when_none_eligible(self, caplog: pytest.LogCaptureFixture) -> None:
        sf = self._make_session_factory(["audit_log_2025_04"])
        with caplog.at_level(logging.WARNING, logger="catalog.main"):
            with patch(
                "catalog.main.audit_partitions_eligible_for_archival",
                return_value=[],
            ):
                await check_audit_partition_ages(session_factory=sf)

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records == []

    @pytest.mark.asyncio
    async def test_no_bind_sets_gauge_zero(self) -> None:
        sf = MagicMock()
        sf.kw = {}
        sf.kw_args = {}
        _AUDIT_ARCHIVAL_GAUGE.set(-1)  # sentinel
        await check_audit_partition_ages(session_factory=sf)
        assert _AUDIT_ARCHIVAL_GAUGE._value.get() == 0.0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_db_error_logs_warning_and_returns(self, caplog: pytest.LogCaptureFixture) -> None:
        sf = MagicMock()
        bind = MagicMock()
        sf.kw = {"bind": bind}
        bind.connect.side_effect = RuntimeError("connection refused")

        with caplog.at_level(logging.WARNING, logger="catalog.main"):
            await check_audit_partition_ages(session_factory=sf)

        assert "failed to query pg_inherits" in caplog.text
