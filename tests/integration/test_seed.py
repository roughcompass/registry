"""End-to-end test for `make dev-seed`.

Bootstraps the ``dev`` tenant, runs ``scripts/seed.py``, and verifies
the high-value invariants from a single sweep:

- Salt's capability + components land with the expected enriched axes
  (attributes, ≥16 composes edges, overview + release_note facts).
- 3 consumer tenants exist with their admin actors.
- 8 cross-tenant adoptions tie consumers to Salt capabilities; matching
  ``provides_to`` edges live in the provider tenant.
- A capability progression definition is installed on ``dev``.
- The five platform capabilities (identity, prefs, notifications,
  web-sdk, web-runtime) exist with `depends_on` edges between them.
- Web SDK carries a `migration_guide` fact for the v2→v3 breaking change.
- Every capability has a technical owner (`owned_by` edge to a `person`
  entity) and a product owner (`product_owned_by` edge).
- Re-running the seed is idempotent — counts are stable.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine

_REPO_ROOT = Path(__file__).parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed.py"

_TOKEN_LINE_RE = re.compile(r"Token\s*:\s*(\S+)")


def _run(database_url: str, script: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *extra],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
        cwd=str(_REPO_ROOT),
        check=False,
    )


@pytest.mark.asyncio
async def test_dev_seed_populates_dev_tenant(pg_container: str) -> None:
    """Full seed: Salt enrichment + multi-tenant org, all from one run."""
    # Bootstrap the dev tenant.
    bootstrap = _run(pg_container, _BOOTSTRAP_SCRIPT, "--tenant-slug", "dev")
    assert bootstrap.returncode == 0, bootstrap.stderr

    # Single seed run — loads every bundle under seeds/.
    seed = _run(pg_container, _SEED_SCRIPT)
    assert seed.returncode == 0, seed.stderr
    assert "salt-design-system" in seed.stdout

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    async with engine.connect() as conn:
        # --- Salt capability + components ---
        salt_row = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT e.entity_id FROM entities e "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND e.name = 'salt-design-system'"
                )
            )
        ).first()
        assert salt_row is not None, "salt-design-system not seeded in dev tenant"
        salt_id = uuid.UUID(str(salt_row[0]))

        component_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM edges e "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND e.src_entity_id = :sid "
                    "AND e.rel = 'composes' AND e.t_invalidated_at IS NULL"
                ),
                {"sid": salt_id},
            )
        ).scalar_one()
        assert component_count >= 16, f"expected ≥16 composes edges, got {component_count}"

        fact_categories = {
            row[0]
            for row in (
                await conn.execute(
                    sqlalchemy.text(
                        "SELECT category FROM facts " "WHERE entity_id = :sid AND t_invalidated_at IS NULL"
                    ),
                    {"sid": salt_id},
                )
            ).all()
        }
        assert {"overview", "release_note"}.issubset(fact_categories), fact_categories

        # --- Multi-tenant org ---
        consumer_slugs = {
            row[0]
            for row in (
                await conn.execute(
                    sqlalchemy.text(
                        "SELECT slug FROM tenants "
                        "WHERE slug IN ('acme-trading', 'beta-research', 'gamma-onboarding')"
                    )
                )
            ).all()
        }
        assert consumer_slugs == {"acme-trading", "beta-research", "gamma-onboarding"}, consumer_slugs

        adoption_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM adoption_events ae "
                    "JOIN entities e ON ae.provider_capability_id = e.entity_id "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND ae.t_invalidated_at IS NULL"
                )
            )
        ).scalar_one()
        assert adoption_count == 8, f"expected 8 adoptions from dev, got {adoption_count}"

        provides_to_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM edges e "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND e.rel = 'provides_to' "
                    "AND e.t_invalidated_at IS NULL"
                )
            )
        ).scalar_one()
        assert provides_to_count == 8, f"expected 8 provides_to edges, got {provides_to_count}"

        prog_row = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT entity_type, is_advisory FROM progression_definitions pd "
                    "JOIN tenants t ON pd.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND pd.t_invalidated_at IS NULL"
                )
            )
        ).first()
        assert prog_row is not None, "expected a progression_definitions row on dev"
        assert prog_row[0] == "capability"
        assert prog_row[1] is False, "capability progression must be enforced, not advisory"

        # --- Platform capabilities: identity, prefs, notifications, web-sdk, web-runtime ---
        platform_cap_names = {
            row[0]
            for row in (
                await conn.execute(
                    sqlalchemy.text(
                        "SELECT e.name FROM entities e "
                        "JOIN tenants t ON e.tenant_id = t.tenant_id "
                        "WHERE t.slug = 'dev' AND e.entity_type = 'capability' "
                        "AND e.name IN ('identity','prefs','notifications','web-sdk','web-runtime')"
                    )
                )
            ).all()
        }
        assert platform_cap_names == {
            "identity",
            "prefs",
            "notifications",
            "web-sdk",
            "web-runtime",
        }, f"missing platform capabilities: {platform_cap_names}"

        # --- depends_on edges: cross-capability dependency graph ---
        # Expected edges (8 total):
        # prefs -> identity, notifications -> identity, notifications -> prefs,
        # web-sdk -> identity, web-sdk -> prefs, web-sdk -> notifications,
        # web-runtime -> web-sdk, web-runtime -> identity, web-runtime -> prefs
        # That's 9 edges. Pin the count + spot-check a few specific ones.
        depends_on_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM edges e "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND e.rel = 'depends_on' "
                    "AND e.t_invalidated_at IS NULL"
                )
            )
        ).scalar_one()
        assert depends_on_count == 9, f"expected 9 depends_on edges, got {depends_on_count}"

        # web-runtime -> depends_on -> web-sdk is the headline edge.
        web_runtime_to_sdk = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT 1 FROM edges e "
                    "JOIN entities src ON e.src_entity_id = src.entity_id "
                    "JOIN entities dst ON e.dst_entity_id = dst.entity_id "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND e.rel = 'depends_on' "
                    "AND src.name = 'web-runtime' AND dst.name = 'web-sdk' "
                    "AND e.t_invalidated_at IS NULL"
                )
            )
        ).first()
        assert web_runtime_to_sdk is not None, "missing web-runtime --depends_on--> web-sdk edge"

        # --- Web SDK v2→v3 breaking change story ---
        web_sdk_id_row = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT entity_id FROM entities e "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND e.name = 'web-sdk'"
                )
            )
        ).first()
        web_sdk_id = uuid.UUID(str(web_sdk_id_row[0]))

        # migration_guide fact must exist — that's where the v2→v3 narrative lives.
        migration_guide = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT title FROM facts "
                    "WHERE entity_id = :sid AND category = 'migration_guide' "
                    "AND t_invalidated_at IS NULL"
                ),
                {"sid": web_sdk_id},
            )
        ).first()
        assert migration_guide is not None, "expected a migration_guide fact on web-sdk for the v2→v3 breaking change"

        # Bitemporal current_version: v2.4.0 valid before 2026-02-01, v3.0.0 after.
        version_rows = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT value::text, t_valid_from, t_valid_to FROM attributes "
                    "WHERE entity_id = :sid AND key = 'current_version' "
                    "AND t_invalidated_at IS NULL "
                    "ORDER BY t_valid_from"
                ),
                {"sid": web_sdk_id},
            )
        ).all()
        assert len(version_rows) == 2, f"expected 2 current_version rows, got {len(version_rows)}"
        # JSONB values come back quoted — "2.4.0" not 2.4.0.
        assert version_rows[0][0] == '"2.4.0"', version_rows[0]
        assert version_rows[1][0] == '"3.0.0"', version_rows[1]

        # --- Owners: 8 person entities, 12 ownership edges (6 caps × 2) ---
        person_count = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM entities e "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND e.entity_type = 'person'"
                )
            )
        ).scalar_one()
        assert person_count == 8, f"expected 8 person entities, got {person_count}"

        # Every capability has exactly one technical owner and one product owner.
        for cap_name in ("salt-design-system", "identity", "prefs", "notifications", "web-sdk", "web-runtime"):
            owner_rels = sorted(
                row[0]
                for row in (
                    await conn.execute(
                        sqlalchemy.text(
                            "SELECT e.rel FROM edges e "
                            "JOIN entities src ON e.src_entity_id = src.entity_id "
                            "JOIN tenants t ON e.tenant_id = t.tenant_id "
                            "WHERE t.slug = 'dev' AND src.name = :cap "
                            "AND e.rel IN ('owned_by', 'product_owned_by') "
                            "AND e.t_invalidated_at IS NULL"
                        ),
                        {"cap": cap_name},
                    )
                ).all()
            )
            assert owner_rels == [
                "owned_by",
                "product_owned_by",
            ], f"{cap_name}: expected one owned_by + one product_owned_by edge, got {owner_rels}"

        # Web SDK's technical owner is Alice Chen (led the v2→v3 breaking change).
        sdk_tech_owner = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT dst.name FROM edges e "
                    "JOIN entities src ON e.src_entity_id = src.entity_id "
                    "JOIN entities dst ON e.dst_entity_id = dst.entity_id "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND src.name = 'web-sdk' "
                    "AND e.rel = 'owned_by' AND e.t_invalidated_at IS NULL"
                )
            )
        ).scalar_one()
        assert sdk_tech_owner == "alice-chen", sdk_tech_owner

    await engine.dispose()

    # --- Idempotency: re-run leaves counts unchanged ---
    reseed = _run(pg_container, _SEED_SCRIPT)
    assert reseed.returncode == 0, reseed.stderr

    engine2 = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    async with engine2.connect() as conn:
        adoption_after = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM adoption_events ae "
                    "JOIN entities e ON ae.provider_capability_id = e.entity_id "
                    "JOIN tenants t ON e.tenant_id = t.tenant_id "
                    "WHERE t.slug = 'dev' AND ae.t_invalidated_at IS NULL"
                )
            )
        ).scalar_one()
    await engine2.dispose()
    assert adoption_after == 8, f"re-run created duplicate adoptions: was 8, now {adoption_after}"
