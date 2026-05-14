"""Seed a dev tenant from JSON bundles in ``seeds/``.

Thin orchestrator over :mod:`scripts.seed_loader`. Resolves the target
tenant + actor, opens a DB session, picks the seed files to load (the
default set or a single named use case), and prints a summary.

Idempotent: re-running with the same files yields the same entity_ids,
same edges, same attributes. UUIDv5 over ``(tenant_id, name)`` makes
identity stable across runs.

Usage::

    make dev-token                                  # if you haven't yet
    make dev-seed                                   # default set: vocab + demo-minimal + salt-ds
    make dev-seed-usecase USECASE=salt-ds           # vocab + just salt-ds
    make dev-seed-list                              # show available use cases
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
import uuid
from pathlib import Path

# Ensure the repo root is importable when invoked as a subprocess from
# arbitrary cwd. Without this, `from registry.X import Y` raises
# ModuleNotFoundError.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from scripts.seed_loader import (  # noqa: E402
    LoadCounts,
    apply_bundles,
    bundles_for_usecase,
    default_bundles,
    discover_usecases,
    load_bundle,
)

_DEFAULT_TENANT_SLUG = "dev"
_DEFAULT_ACTOR_NAME = "dev-admin"
_SEEDS_ROOT = _REPO_ROOT / "seeds"

# Matches the docker-compose default in registry/docker-compose.yml.
_DOCKER_COMPOSE_DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5544/registry"


def _ansi(t: str, code: str, tty: bool) -> str:
    return f"\033[{code}m{t}\033[0m" if tty else t


async def _resolve_dev_tenant(session: object, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Look up the dev tenant + dev-admin actor UUIDs. Both must already exist."""
    tenant_row = (
        await session.execute(  # type: ignore[attr-defined]
            text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
            {"slug": slug},
        )
    ).first()
    if tenant_row is None:
        raise SystemExit(
            f"error: tenant with slug {slug!r} not found. "
            f"Run `make dev-token` first to bootstrap the tenant + actor."
        )
    tenant_id = uuid.UUID(str(tenant_row[0]))

    actor_row = (
        await session.execute(  # type: ignore[attr-defined]
            text(
                "SELECT actor_id FROM actors "
                "WHERE tenant_id = :tid AND display_name = :name AND actor_kind = 'human'"
            ),
            {"tid": tenant_id, "name": _DEFAULT_ACTOR_NAME},
        )
    ).first()
    if actor_row is None:
        raise SystemExit(
            f"error: actor {_DEFAULT_ACTOR_NAME!r} not found in tenant {slug!r}. "
            f"Run `make dev-token` first."
        )
    actor_id = uuid.UUID(str(actor_row[0]))
    return tenant_id, actor_id


def _emit_summary(
    tenant_slug: str,
    tenant_id: uuid.UUID,
    bundle_paths: list[Path],
    counts: LoadCounts,
) -> None:
    tty = sys.stdout.isatty()
    bold = lambda s: _ansi(s, "1", tty)  # noqa: E731
    cyan = lambda s: _ansi(s, "36", tty)  # noqa: E731
    dim = lambda s: _ansi(s, "2", tty)  # noqa: E731

    print(bold(f"Seeded dev tenant {tenant_slug!r} ({tenant_id})"))
    print(f"  {dim('Bundles loaded')}: {len(bundle_paths)}")
    for path in bundle_paths:
        print(f"    {path.relative_to(_REPO_ROOT)}")
    print(
        f"  {dim('Vocabulary')}: {counts.vocabulary} new value(s) "
        f"(existing values left alone)"
    )
    print(
        f"  {dim('External systems')}: {counts.external_systems} new system(s)"
    )
    print(
        f"  {dim('Entities')}: "
        f"{counts.entities_created} created, {counts.entities_present} already present"
    )
    for name, info in counts.per_entity.items():
        marker = "+ created" if info["created_new"] else "= already present"
        print(f"    {name:25s}  {cyan(info['entity_id'])}  {dim(marker)}")
    print(
        f"  {dim('Edges')}: {counts.edges_created} new composes-edge(s)"
    )
    print(
        f"  {dim('Attributes')}: {counts.attributes_created} new attribute row(s)"
    )
    print(
        f"  {dim('Facts')}: "
        f"{counts.facts_created} new, {counts.facts_updated} updated in place"
    )
    print(
        f"  {dim('External IDs')}: {counts.external_ids_created} new mapping(s)"
    )
    print(
        f"  {dim('Bitemporal rows')}: {counts.bitemporal_rows_replaced} row(s) inserted"
    )
    print()
    print(bold("Try it:"))
    print("  curl -H 'Authorization: Bearer <token>' http://localhost:8000/v1/capabilities")
    print("  curl -H 'Authorization: Bearer <token>' http://localhost:8000/v1/capabilities/salt-design-system")
    print(
        "  curl -H 'Authorization: Bearer <token>' "
        "'http://localhost:8000/v1/capabilities/salt-design-system?include=components,external_ids'"
    )
    print(
        "  # Time-travel — Salt as of mid-October 2025 (v1.43):\n"
        "  curl -H 'Authorization: Bearer <token>' "
        "'http://localhost:8000/v1/capabilities/salt-design-system?as_of=2025-10-15T00:00:00%2B00:00'"
    )
    print(
        "  curl -H 'Authorization: Bearer <token>' "
        "'http://localhost:8000/v1/entities?external_system=npm&external_id=@salt-ds/core'"
    )
    print(f"  {dim('# /docs → Authorize → bearerAuth → expand retrieval → Try it out')}")


def _resolve_files(usecase: str | None) -> list[Path]:
    if usecase is None:
        return default_bundles(_SEEDS_ROOT)
    # Vocabulary is a prerequisite for any use case — load it first.
    files: list[Path] = []
    vocab = _SEEDS_ROOT / "_vocabulary.json"
    if vocab.exists():
        files.append(vocab)
    files.extend(bundles_for_usecase(_SEEDS_ROOT, usecase))
    return files


async def _seed(tenant_slug: str, files: list[Path]) -> tuple[uuid.UUID, LoadCounts]:
    from registry.config import get_settings  # noqa: PLC0415

    if "DATABASE_URL" not in os.environ:  # config: intentional
        os.environ["DATABASE_URL"] = _DOCKER_COMPOSE_DATABASE_URL
        print(
            f"DATABASE_URL not set; defaulting to docker-compose Postgres "
            f"({_DOCKER_COMPOSE_DATABASE_URL}). Export DATABASE_URL to override.",
            file=sys.stderr,
        )
    database_url = get_settings().database_url

    bundles = [load_bundle(p) for p in files]

    engine = create_async_engine(
        database_url,
        connect_args={"prepared_statement_cache_size": 0},  # PgBouncer transaction mode
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            tenant_id, actor_id = await _resolve_dev_tenant(session, tenant_slug)
            counts = await apply_bundles(session, tenant_id, actor_id, bundles)
    finally:
        await engine.dispose()

    return tenant_id, counts


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Seed a dev tenant from JSON bundles in seeds/.")
    parser.add_argument(
        "--tenant-slug",
        default=_DEFAULT_TENANT_SLUG,
        help=f"Target tenant slug (default: {_DEFAULT_TENANT_SLUG!r}).",
    )
    parser.add_argument(
        "--usecase",
        default=None,
        help=(
            "Name of a single use case under seeds/ (e.g. 'salt-ds'). "
            "When omitted, loads the default set: _vocabulary + demo-minimal + salt-ds."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print available use cases and exit.",
    )
    args = parser.parse_args(argv)

    if args.list:
        usecases = discover_usecases(_SEEDS_ROOT)
        print(f"Available use cases under {_SEEDS_ROOT.relative_to(_REPO_ROOT)}/:")
        for uc in usecases:
            files = sorted((_SEEDS_ROOT / uc).glob("*.json"))
            print(f"  {uc:20s}  {len(files)} file(s): {[f.name for f in files]}")
        print()
        print("Default `make dev-seed` loads:")
        for path in default_bundles(_SEEDS_ROOT):
            print(f"  {path.relative_to(_REPO_ROOT)}")
        return 0

    files = _resolve_files(args.usecase)
    if not files:
        print("error: no seed files found", file=sys.stderr)
        return 1

    tenant_id, counts = asyncio.run(_seed(args.tenant_slug, files))
    _emit_summary(args.tenant_slug, tenant_id, files, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
