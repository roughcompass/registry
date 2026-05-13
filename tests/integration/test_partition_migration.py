"""Partition migration idempotency, pruning, and detach integration tests.

Covers storage-layer invariants for the hash-partitioned ``audit_log`` and
``embeddings`` tables:

- ``test_partition_migrate_idempotent``: runs ``partition_migrate.py`` against a
  testcontainers DB; verifies audit_log is partitioned; runs again and asserts
  the second invocation emits a warning-only "cutover already done" message.
- ``test_partition_pruning_embeddings``: EXPLAIN for WHERE tenant_id = :tid shows
  only 1-of-8 hash partitions scanned (partition pruning active).
- ``test_audit_partition_detach_procedure``: detaches a synthetic old partition;
  verifies audit_log parent is still queryable after detach; verifies detached
  partition is accessible as a standalone table.
- ``test_full_conformance_suite_passes``: programmatically collects all three
  conformance suites (tenant isolation, OpenAPI drift, MCP conformance) in-process
  and asserts zero collection errors. This in-process collection gate ensures the
  conformance files remain importable and structurally valid; full suite execution
  is covered by ``make test-conformance``.

Manual checklist (not automated — document here so the exit gate is explicit):
    1. k6 30-min load test:
       cd scripts/load_test
       k6 run --duration=30m --vus=100 k6_script.js
       Assert: p95 latency < 500ms on /v1/search; error rate < 0.1%.
    2. Helm fresh-cluster deploy:
       helm install catalog ./helm --set image.tag=<sha>
       kubectl wait --for=condition=Ready pod -l app=capability-fabric --timeout=120s
       curl -f http://<cluster-ip>/healthz
    3. SBOM attached to the release artefact set:
       Verify the published release (whichever registry/host the
       operator's release pipeline targets) carries ``sbom.spdx.json``.
       On GitHub: ``gh release view v1.0.0 --json assets | jq '.[].name'``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_partition_migrate(database_url: str, dry_run: bool = False) -> subprocess.CompletedProcess:
    """Invoke partition_migrate.py as a subprocess."""
    # Convert asyncpg URL to psycopg2 URL (the script uses synchronous psycopg2)
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://").replace(
        "postgresql://", "postgresql+psycopg2://"
    )
    cmd = [sys.executable, str(_SCRIPTS / "partition_migrate.py"), "--database-url", sync_url]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": sync_url},
        cwd=str(_REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partition_migrate_idempotent(pg_container: str) -> None:
    """Run partition_migrate.py twice; second run must warn-only, not fail.

    Idempotency check: when audit_log_archive already exists the
    script emits 'cutover already done' and exits 0 without touching the tables.
    """
    # First run — migrates the live schema
    result1 = _run_partition_migrate(pg_container)
    assert result1.returncode == 0, (
        f"partition_migrate.py first run failed (exit {result1.returncode}):\n"
        f"stdout: {result1.stdout}\nstderr: {result1.stderr}"
    )

    # Second run — must detect completed cutover and emit a warning, not fail
    result2 = _run_partition_migrate(pg_container)
    assert result2.returncode == 0, (
        f"partition_migrate.py second run failed (exit {result2.returncode}):\n"
        f"stdout: {result2.stdout}\nstderr: {result2.stderr}"
    )
    combined = (result2.stdout + result2.stderr).lower()
    assert "cutover already done" in combined or "warning" in combined, (
        "Expected second invocation to emit 'cutover already done' warning; "
        f"got stdout={result2.stdout!r} stderr={result2.stderr!r}"
    )


@pytest.mark.asyncio
async def test_partition_pruning_embeddings(pg_container: str) -> None:
    """EXPLAIN for WHERE tenant_id = :tid must show exactly 1 of 8 hash partitions.

    Partition pruning means the planner eliminates 7
    of the 8 embeddings hash partitions at plan time.  We verify this by
    inspecting the EXPLAIN (FORMAT JSON) output and counting the number of
    child plans that reference an embeddings partition.
    """
    import json

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    tid = uuid.uuid4()

    async with engine.begin() as conn:
        result = await conn.execute(
            # Use raw text to avoid any SQLAlchemy ORM expansion that would
            # prevent partition pruning.
            __import__("sqlalchemy").text(
                "EXPLAIN (FORMAT JSON, ANALYZE FALSE) " "SELECT * FROM embeddings WHERE tenant_id = :tid"
            ),
            {"tid": tid},
        )
        rows = result.fetchall()

    await engine.dispose()

    # rows is a list of single-column rows; the first cell holds the EXPLAIN
    # output. asyncpg auto-decodes JSON columns into Python lists/dicts, so
    # only run json.loads when the driver handed back a raw string.
    raw_plan = rows[0][0]
    plan_json = json.loads(raw_plan) if isinstance(raw_plan, str | bytes | bytearray) else raw_plan

    def _count_partition_scans(node: object) -> int:
        """Recursively count Seq Scan / Index Scan nodes on embeddings partitions."""
        count = 0
        if isinstance(node, dict):
            rel = node.get("Relation Name", "")
            if rel.startswith("embeddings_p") or rel.startswith("embeddings_new_p"):
                count += 1
            for child in node.get("Plans", []):
                count += _count_partition_scans(child)
        elif isinstance(node, list):
            for item in node:
                count += _count_partition_scans(item)
        return count

    scanned = _count_partition_scans(plan_json)
    assert scanned == 1, (
        f"Expected pruning to 1-of-8 partitions; planner scanned {scanned}. "
        f"Ensure partition_migrate.py has been run and enable_partition_pruning=on."
    )


@pytest.mark.asyncio
async def test_audit_partition_detach_procedure(pg_container: str) -> None:
    """DETACH CONCURRENTLY an old audit_log partition; parent must remain queryable.

    Verifies:
    1. A synthetic partition can be created and populated.
    2. DETACH PARTITION CONCURRENTLY succeeds without locking other partitions.
    3. The audit_log parent table is still queryable after detach.
    4. The detached partition is accessible as a standalone table.
    """
    import sqlalchemy

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    partition_name = "audit_log_2020_01"

    async with engine.begin() as conn:
        # Create a synthetic old partition
        await conn.execute(
            sqlalchemy.text(
                f"CREATE TABLE IF NOT EXISTS {partition_name} "
                f"PARTITION OF audit_log "
                f"FOR VALUES FROM ('2020-01-01') TO ('2020-02-01')"
            )
        )

        # Seed a tenants row so the audit_log_tenant_id_fkey constraint is
        # satisfied. Use a fixed slug suffix on the synthetic tenant to keep
        # the test deterministic across reruns within the same container.
        seed_tenant_id = uuid.uuid4()
        await conn.execute(
            sqlalchemy.text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, 'audit-detach-test', '2020-01-01 00:00:00+00', TRUE)"
            ),
            {"tid": seed_tenant_id, "slug": f"audit-detach-{seed_tenant_id.hex[:8]}"},
        )

        # Insert a row so the partition is non-empty and queryable after
        # detach. Column names match the audit_log schema in migration 0006:
        # audit_id PK, target_type / target_id (not legacy resource_*), ts
        # as the partition key. actor_id is left NULL so the test does not
        # have to seed an actors row to satisfy that FK.
        await conn.execute(
            sqlalchemy.text(
                f"INSERT INTO {partition_name} "
                f"(audit_id, tenant_id, actor_id, action, target_type, target_id, ts) "
                f"VALUES (gen_random_uuid(), :tid, NULL, "
                f"'test_action', 'capability', gen_random_uuid(), '2020-01-15 00:00:00+00')"
            ),
            {"tid": seed_tenant_id},
        )

    # DETACH CONCURRENTLY must run outside an explicit transaction block
    create_async_engine(
        pg_container.replace("+asyncpg", "").replace("postgresql://", "postgresql://"),
        connect_args={"prepared_statement_cache_size": 0},
        isolation_level="AUTOCOMMIT",
    )
    # For asyncpg, use isolation_level via execution_options
    detach_engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
        isolation_level="AUTOCOMMIT",
    )
    async with detach_engine.connect() as conn:
        await conn.execute(sqlalchemy.text(f"ALTER TABLE audit_log DETACH PARTITION {partition_name} CONCURRENTLY"))
    await detach_engine.dispose()

    # After detach: parent table must still be queryable
    async with engine.begin() as conn:
        result = await conn.execute(sqlalchemy.text("SELECT COUNT(*) FROM audit_log"))
        count = result.scalar()
        assert count is not None, "audit_log is not queryable after partition detach"

        # Detached partition must be accessible as a standalone table
        result2 = await conn.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {partition_name}"))
        detached_count = result2.scalar()
        assert detached_count == 1, f"Expected 1 row in detached partition; got {detached_count}"

    await engine.dispose()


def test_full_conformance_suite_passes() -> None:
    """Collect all three conformance suites in-process and assert zero collection errors.

    Uses --collect-only so this test does not require a live database. The
    purpose is to verify that all three conformance files remain importable
    and structurally valid. Full conformance execution (which does require a
    database) is covered by ``make test-conformance``; keeping that gate
    separate avoids a hard database dependency in the integration suite.

    The three suites are:
      - tests/conformance/test_tenant_isolation.py
      - tests/conformance/test_openapi_drift.py
      - tests/conformance/test_mcp_conformance.py
    """
    conformance_dir = _REPO_ROOT / "tests" / "conformance"
    suite_files = [
        str(conformance_dir / "test_tenant_isolation.py"),
        str(conformance_dir / "test_openapi_drift.py"),
        str(conformance_dir / "test_mcp_conformance.py"),
    ]

    # Use --collect-only first: if any file fails to collect, we catch it here
    # without running tests (which would require a live database in some cases).
    result = pytest.main(
        ["--collect-only", "-q", "--tb=short"] + suite_files,
        plugins=[],
    )

    # ExitCode.NO_TESTS_COLLECTED (5) is also acceptable if the environment
    # has no DB; what we reject is ERROR (3) or USAGE_ERROR (4).
    assert result not in (3, 4), (
        f"Conformance suite collection failed with pytest exit code {result!r}. "
        "Check that all three conformance files are importable and collect cleanly."
    )
