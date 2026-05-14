"""Generic loader for seed-file JSON bundles.

Reads one or more files conforming to ``seeds/_schema.md`` (schema_version
= 1) and applies them idempotently against a dev tenant's database.

The loader is a thin SQL adapter:

- Knowledge about Salt, demo capabilities, or any specific use case lives
  in the JSON files. The loader does not import that data.
- Every operation is idempotent: re-running with the same files against
  an already-seeded database produces zero net changes (no duplicate
  entities, edges, attributes, or facts).
- Entity IDs are deterministic — UUIDv5 over ``(tenant_id, name)`` — so
  the same row appears under the same UUID across runs and across files.

The loader does not bootstrap tenants or actors; that's
``scripts/bootstrap_dev_tenant.py``. It assumes both exist and fails
loudly if they don't.
"""

from __future__ import annotations

import datetime
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text


_SCHEMA_VERSION = 1
_DEFAULT_VISIBILITY = "private"


@dataclass
class LoadCounts:
    """Aggregate row-insert counts for the summary printer."""

    vocabulary: int = 0
    external_systems: int = 0
    entities_created: int = 0
    entities_present: int = 0
    edges_created: int = 0
    attributes_created: int = 0
    facts_created: int = 0
    facts_updated: int = 0
    external_ids_created: int = 0
    bitemporal_rows_replaced: int = 0
    per_entity: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SeedBundle:
    """In-memory representation of a single seed file."""

    path: Path
    name: str
    description: str
    released_at: datetime.datetime | None
    vocabulary: list[dict[str, str]]
    external_systems: list[dict[str, str]]
    entities: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    bitemporal_attributes: list[dict[str, Any]]


_ALLOWED_TOP_LEVEL: frozenset[str] = frozenset(
    [
        "schema_version",
        "name",
        "description",
        "released_at",
        "vocabulary",
        "external_systems",
        "entities",
        "facts",
        "bitemporal_attributes",
    ]
)


def _parse_iso(value: str | None) -> datetime.datetime | None:
    if value is None:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(value)


def load_bundle(path: Path) -> SeedBundle:
    """Parse a seed JSON file. Raises ValueError on any shape violation."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be an object, got {type(raw).__name__}")

    unknown = set(raw.keys()) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ValueError(
            f"{path}: unknown top-level key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_TOP_LEVEL)}. Update _schema.md if you're "
            f"adding a new section."
        )

    schema_version = raw.get("schema_version")
    if schema_version != _SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version {schema_version!r} not supported "
            f"(loader speaks {_SCHEMA_VERSION})."
        )

    name = raw.get("name") or path.stem
    description = raw.get("description") or ""
    released_at = _parse_iso(raw.get("released_at"))

    vocabulary = raw.get("vocabulary") or []
    external_systems = raw.get("external_systems") or []
    entities = raw.get("entities") or []
    facts = raw.get("facts") or []
    bitemporal_attributes = raw.get("bitemporal_attributes") or []

    return SeedBundle(
        path=path,
        name=name,
        description=description,
        released_at=released_at,
        vocabulary=vocabulary,
        external_systems=external_systems,
        entities=entities,
        facts=facts,
        bitemporal_attributes=bitemporal_attributes,
    )


def deterministic_entity_id(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """UUIDv5 over (tenant_id, name) — stable identity across runs.

    Exposed so external scripts (re-implementing the seed contract or
    debugging by-hand) can compute the same UUID the loader will use.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{tenant_id}:{name}")


def deterministic_edge_id(
    tenant_id: uuid.UUID, src: uuid.UUID, rel: str, dst: uuid.UUID
) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{tenant_id}:{src}:{rel}:{dst}")


# ---------------------------------------------------------------------------
# Section appliers — each takes a session, the tenant/actor context, and the
# section data. Idempotency is enforced per-section.
# ---------------------------------------------------------------------------


async def _apply_vocabulary(
    session: Any,
    tenant_id: uuid.UUID,
    rows: list[dict[str, str]],
    counts: LoadCounts,
    now: datetime.datetime,
) -> None:
    for row in rows:
        kind = row["kind"]
        value = row["value"]
        result = await session.execute(
            text(
                "INSERT INTO vocabulary_values "
                "(vocab_id, tenant_id, kind, value, is_system, created_at) "
                "VALUES (gen_random_uuid(), :tid, :kind, :value, FALSE, :now) "
                "ON CONFLICT (tenant_id, kind, value) DO NOTHING"
            ),
            {"tid": tenant_id, "kind": kind, "value": value, "now": now},
        )
        counts.vocabulary += result.rowcount or 0


async def _apply_external_systems(
    session: Any,
    tenant_id: uuid.UUID,
    rows: list[dict[str, str]],
    counts: LoadCounts,
    now: datetime.datetime,
) -> None:
    for row in rows:
        result = await session.execute(
            text(
                "INSERT INTO external_systems "
                "(slug, tenant_id, display_name, url_template, description, created_at) "
                "VALUES (:slug, :tid, :name, :url, NULL, :now) "
                "ON CONFLICT (tenant_id, slug) DO NOTHING"
            ),
            {
                "slug": row["slug"],
                "tid": tenant_id,
                "name": row["display_name"],
                "url": row["url_template"],
                "now": now,
            },
        )
        counts.external_systems += result.rowcount or 0


async def _upsert_attribute(
    session: Any,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID,
    key: str,
    value: Any,
    valid_from: datetime.datetime,
) -> bool:
    """Insert one attribute row only if no current live row exists.

    "Current" = ``t_valid_to IS NULL AND t_invalidated_at IS NULL``.
    A seed script never invalidates an existing live row.
    """
    existing = (
        await session.execute(
            text(
                "SELECT 1 FROM attributes "
                "WHERE entity_id = :eid AND key = :key "
                "AND t_valid_to IS NULL AND t_invalidated_at IS NULL "
                "LIMIT 1"
            ),
            {"eid": entity_id, "key": key},
        )
    ).first()
    if existing is not None:
        return False

    await session.execute(
        text(
            "INSERT INTO attributes "
            "(attr_id, tenant_id, entity_id, key, value, "
            " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
            "VALUES (gen_random_uuid(), :tid, :eid, :key, CAST(:value AS JSONB), "
            "        :from, NULL, :from, NULL, :aid)"
        ),
        {
            "tid": tenant_id,
            "eid": entity_id,
            "key": key,
            "value": json.dumps(value),
            "from": valid_from,
            "aid": actor_id,
        },
    )
    return True


async def _insert_fact(
    session: Any,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID,
    fact: dict[str, Any],
    default_valid_from: datetime.datetime,
    counts: LoadCounts,
) -> None:
    """Insert a fact if absent; update title/body/valid_from if (category,
    title) already exists but content drifted.

    Idempotency key: (entity_id, category, title) with t_invalidated_at IS NULL.
    """
    category = fact["category"]
    title = fact["title"]
    body = fact["body"]
    body_format = fact.get("body_format", "markdown")
    valid_from = _parse_iso(fact.get("valid_from")) or default_valid_from

    existing = (
        await session.execute(
            text(
                "SELECT fact_id, title, body, body_format, t_valid_from FROM facts "
                "WHERE entity_id = :eid AND category = :cat AND title = :title "
                "AND t_invalidated_at IS NULL LIMIT 1"
            ),
            {"eid": entity_id, "cat": category, "title": title},
        )
    ).first()

    if existing is None:
        await session.execute(
            text(
                "INSERT INTO facts "
                "(fact_id, tenant_id, entity_id, category, title, body, body_format, "
                " is_authoritative, is_authoritative_superseded, sync_run_id, "
                " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                "VALUES (gen_random_uuid(), :tid, :eid, :cat, :title, :body, :body_format, "
                "        TRUE, FALSE, NULL, :from, NULL, :from, NULL, :aid)"
            ),
            {
                "tid": tenant_id,
                "eid": entity_id,
                "cat": category,
                "title": title,
                "body": body,
                "body_format": body_format,
                "from": valid_from,
                "aid": actor_id,
            },
        )
        counts.facts_created += 1
        return

    # An existing row matches by (category, title). Update body / body_format /
    # valid_from in place if any drifted — this is intentional. Bitemporal
    # purity says we should invalidate-and-replace, but a seed script
    # operating against a tenant we own is allowed to nudge facts forward
    # without exploding the row count on every minor copy-edit.
    fid, existing_title, existing_body, existing_format, existing_from = existing
    needs_update = (
        existing_body != body
        or existing_format != body_format
        or existing_from != valid_from
    )
    if needs_update:
        await session.execute(
            text(
                "UPDATE facts SET body = :body, body_format = :body_format, "
                "                 t_valid_from = :from, t_ingested_at = :from "
                "WHERE fact_id = :fid"
            ),
            {
                "body": body,
                "body_format": body_format,
                "from": valid_from,
                "fid": fid,
            },
        )
        counts.facts_updated += 1


async def _insert_external_id(
    session: Any,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    mapping: dict[str, str],
    counts: LoadCounts,
) -> None:
    result = await session.execute(
        text(
            "INSERT INTO entity_external_ids "
            "(external_id_pk, entity_id, tenant_id, external_system_slug, "
            " external_id, url, metadata_jsonb) "
            "VALUES (gen_random_uuid(), :eid, :tid, :system, :ext, :url, NULL) "
            "ON CONFLICT (tenant_id, external_system_slug, external_id) DO NOTHING"
        ),
        {
            "eid": entity_id,
            "tid": tenant_id,
            "system": mapping["system"],
            "ext": mapping["external_id"],
            "url": mapping.get("url"),
        },
    )
    counts.external_ids_created += result.rowcount or 0


async def _resolve_or_insert_entity(
    session: Any,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity: dict[str, Any],
    default_valid_from: datetime.datetime,
    counts: LoadCounts,
) -> uuid.UUID:
    """Find or insert an entity row. Returns the entity_id."""
    name = entity["name"]
    entity_type = entity["entity_type"]
    visibility = entity.get("visibility", _DEFAULT_VISIBILITY)
    valid_from = _parse_iso(entity.get("valid_from")) or default_valid_from
    entity_id = deterministic_entity_id(tenant_id, name)

    existing = (
        await session.execute(
            text("SELECT 1 FROM entities WHERE entity_id = :eid"),
            {"eid": entity_id},
        )
    ).first()

    if existing is None:
        await session.execute(
            text(
                "INSERT INTO entities "
                "(entity_id, tenant_id, entity_type, name, external_id, "
                " is_active, created_at, created_by, visibility) "
                "VALUES (:eid, :tid, :etype, :name, NULL, TRUE, :now, :aid, :vis)"
            ),
            {
                "eid": entity_id,
                "tid": tenant_id,
                "etype": entity_type,
                "name": name,
                "now": valid_from,
                "aid": actor_id,
                "vis": visibility,
            },
        )
        counts.entities_created += 1
    else:
        counts.entities_present += 1

    counts.per_entity[name] = {"entity_id": str(entity_id), "created_new": existing is None}
    return entity_id


async def _apply_entities(
    session: Any,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    rows: list[dict[str, Any]],
    counts: LoadCounts,
    default_valid_from: datetime.datetime,
) -> dict[str, uuid.UUID]:
    """Insert entities + attributes + parent edges + facts + external_ids."""
    name_to_id: dict[str, uuid.UUID] = {}

    for entity in rows:
        entity_id = await _resolve_or_insert_entity(
            session, tenant_id, actor_id, entity, default_valid_from, counts
        )
        name_to_id[entity["name"]] = entity_id

        # Attributes — simple upsert (insert only if no current row).
        valid_from = _parse_iso(entity.get("valid_from")) or default_valid_from
        for key, value in (entity.get("attributes") or {}).items():
            created = await _upsert_attribute(
                session, tenant_id, entity_id, actor_id, key, value, valid_from
            )
            if created:
                counts.attributes_created += 1

        # Parent edge: composes from parent to this entity.
        parent_name = entity.get("parent")
        if parent_name:
            parent_id = name_to_id.get(parent_name) or deterministic_entity_id(
                tenant_id, parent_name
            )
            edge_id = deterministic_edge_id(tenant_id, parent_id, "composes", entity_id)
            existing_edge = (
                await session.execute(
                    text("SELECT 1 FROM edges WHERE edge_id = :eid"),
                    {"eid": edge_id},
                )
            ).first()
            if existing_edge is None:
                await session.execute(
                    text(
                        "INSERT INTO edges "
                        "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                        " properties, is_authoritative, sync_run_id, "
                        " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                        "VALUES (:edge_id, :tid, :src, 'composes', :dst, "
                        "        NULL, TRUE, NULL, :from, NULL, :from, NULL, :aid)"
                    ),
                    {
                        "edge_id": edge_id,
                        "tid": tenant_id,
                        "src": parent_id,
                        "dst": entity_id,
                        "from": valid_from,
                        "aid": actor_id,
                    },
                )
                counts.edges_created += 1

        # Inline facts on this entity.
        for fact in entity.get("facts") or []:
            await _insert_fact(
                session, tenant_id, entity_id, actor_id, fact, valid_from, counts
            )

        # External-system mappings.
        for mapping in entity.get("external_ids") or []:
            await _insert_external_id(session, tenant_id, entity_id, mapping, counts)

    return name_to_id


async def _apply_cross_entity_facts(
    session: Any,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    rows: list[dict[str, Any]],
    counts: LoadCounts,
    default_valid_from: datetime.datetime,
) -> None:
    for fact in rows:
        entity_name = fact["entity"]
        entity_id = deterministic_entity_id(tenant_id, entity_name)
        # Verify the entity exists; cross-file references can otherwise
        # silently insert facts on phantom entities.
        existing = (
            await session.execute(
                text("SELECT 1 FROM entities WHERE entity_id = :eid"),
                {"eid": entity_id},
            )
        ).first()
        if existing is None:
            raise ValueError(
                f"cross-entity fact references entity {entity_name!r} which does "
                f"not exist; load the file that defines it first."
            )
        await _insert_fact(
            session, tenant_id, entity_id, actor_id, fact, default_valid_from, counts
        )


async def _apply_bitemporal_attributes(
    session: Any,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    rows: list[dict[str, Any]],
    counts: LoadCounts,
    now: datetime.datetime,
) -> None:
    for spec in rows:
        entity_name = spec["entity"]
        key = spec["key"]
        replace = bool(spec.get("replace_existing", False))
        entries = spec["rows"]
        entity_id = deterministic_entity_id(tenant_id, entity_name)

        if replace:
            await session.execute(
                text(
                    "DELETE FROM attributes "
                    "WHERE entity_id = :eid AND key = :key "
                    "AND t_invalidated_at IS NULL"
                ),
                {"eid": entity_id, "key": key},
            )

        for row in entries:
            valid_from = _parse_iso(row["valid_from"])
            valid_to = _parse_iso(row.get("valid_to"))
            value = row["value"]
            await session.execute(
                text(
                    "INSERT INTO attributes "
                    "(attr_id, tenant_id, entity_id, key, value, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                    "VALUES (gen_random_uuid(), :tid, :eid, :key, CAST(:value AS JSONB), "
                    "        :from, :to, :now, NULL, :aid)"
                ),
                {
                    "tid": tenant_id,
                    "eid": entity_id,
                    "key": key,
                    "value": json.dumps(value),
                    "from": valid_from,
                    "to": valid_to,
                    "now": now,
                    "aid": actor_id,
                },
            )
            counts.bitemporal_rows_replaced += 1


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def apply_bundles(
    session: Any,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    bundles: list[SeedBundle],
) -> LoadCounts:
    """Apply each bundle in order. One transaction; partial failure rolls back.

    Caller is responsible for wrapping in ``async with session.begin()`` —
    the loader doesn't manage transactions because the calling script may
    want to compose this with other work in the same unit of work.
    """
    counts = LoadCounts()
    now = datetime.datetime.now(tz=datetime.UTC)

    for bundle in bundles:
        default_valid_from = bundle.released_at or now
        await _apply_vocabulary(session, tenant_id, bundle.vocabulary, counts, now)
        await _apply_external_systems(session, tenant_id, bundle.external_systems, counts, now)
        await _apply_entities(
            session, tenant_id, actor_id, bundle.entities, counts, default_valid_from
        )
        await _apply_cross_entity_facts(
            session, tenant_id, actor_id, bundle.facts, counts, default_valid_from
        )
        await _apply_bitemporal_attributes(
            session, tenant_id, actor_id, bundle.bitemporal_attributes, counts, now
        )

    return counts


def discover_usecases(seeds_root: Path) -> list[str]:
    """List directory names under seeds/ that contain at least one .json file."""
    usecases: list[str] = []
    for child in sorted(seeds_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        if any(child.glob("*.json")):
            usecases.append(child.name)
    return usecases


def bundles_for_usecase(seeds_root: Path, usecase: str) -> list[Path]:
    """Resolve the seed files for a use case, in load order (sorted by name).

    Convention: files are named like ``v1.43.json``, ``v1.44.json``,
    ``v1.45.json`` so lexical sort matches chronological load order.
    """
    usecase_dir = seeds_root / usecase
    if not usecase_dir.is_dir():
        raise ValueError(
            f"use case {usecase!r} not found at {usecase_dir}; "
            f"available: {discover_usecases(seeds_root)}"
        )
    files = sorted(usecase_dir.glob("*.json"))
    if not files:
        raise ValueError(f"use case {usecase!r} has no .json files")
    return files


def default_bundles(seeds_root: Path) -> list[Path]:
    """The set of files ``make dev-seed`` loads when no use case is named."""
    files: list[Path] = []
    vocab = seeds_root / "_vocabulary.json"
    if vocab.exists():
        files.append(vocab)
    demo_minimal = seeds_root / "demo-minimal"
    if demo_minimal.is_dir():
        files.extend(sorted(demo_minimal.glob("*.json")))
    salt = seeds_root / "salt-ds"
    if salt.is_dir():
        files.extend(sorted(salt.glob("*.json")))
    return files


__all__ = [
    "LoadCounts",
    "SeedBundle",
    "apply_bundles",
    "bundles_for_usecase",
    "default_bundles",
    "deterministic_edge_id",
    "deterministic_entity_id",
    "discover_usecases",
    "load_bundle",
]


if __name__ == "__main__":
    # Importing as a module is the normal usage; running it directly just
    # prints the discovered use cases for quick inspection.
    seeds_root = Path(__file__).parent.parent / "seeds"
    print(f"seeds root: {seeds_root}")
    print(f"default load set: {[p.name for p in default_bundles(seeds_root)]}")
    print(f"named use cases: {discover_usecases(seeds_root)}")
    sys.exit(0)
