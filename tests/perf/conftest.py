"""Shared fixtures for performance tests.

All perf tests require a live Postgres container, which is provided by the
session-scoped ``pg_container`` fixture in ``tests/conftest.py`` (shared
across the whole test suite).

CI gate
-------
Perf tests are marked ``@pytest.mark.perf`` and ``@pytest.mark.slow``.  To
skip them in normal unit-only CI runs, add ``-m "not perf"`` to the pytest
invocation.  To run only perf: ``pytest tests/perf/ -m perf --timeout=300``.

SLO targets
----------------------------------
- Reverse traversal (depth=5, 100-node graph): p95 < 300 ms.
- Blast-radius (depth=5, 1000-node graph, cache-hit path): p95 < 1 s.
- PII scanner (64 KB input): p95 < 50 ms (no DB required).
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register custom marks to suppress PytestUnknownMarkWarning."""
    config.addinivalue_line(
        "markers",
        "perf: marks a test as a performance / SLO verification test.",
    )
    config.addinivalue_line(
        "markers",
        "slow: marks a test as slow (excluded from fast unit-only CI runs).",
    )
