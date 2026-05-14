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
import os
import re
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
    tenants_created: int = 0
    tenants_present: int = 0
    actors_created: int = 0
    role_grants_created: int = 0
    adoptions_created: int = 0
    progression_definitions_created: int = 0
    visibility_changes: int = 0
    per_entity: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SeedBundle:
    """In-memory representation of a single seed file."""

    path: Path
    name: str
    description: str
    released_at: datetime.datetime | None
    target_tenant_slug: str | None
    tenants: list[dict[str, Any]]
    actors: list[dict[str, Any]]
    vocabulary: list[dict[str, str]]
    external_systems: list[dict[str, str]]
    entities: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    bitemporal_attributes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    adoptions: list[dict[str, Any]]
    progression_definitions: list[dict[str, Any]]


_ALLOWED_TOP_LEVEL: frozenset[str] = frozenset(
    [
        "schema_version",
        "name",
        "description",
        "released_at",
        "target_tenant_slug",
        "tenants",
        "actors",
        "vocabulary",
        "external_systems",
        "entities",
        "facts",
        "bitemporal_attributes",
        "edges",
        "adoptions",
        "progression_definitions",
    ]
)

# Roles the bootstrap script provisions per tenant. Re-declared here so
# the loader can seed roles when it creates a new tenant without
# importing the script (which would pull in the whole bootstrap CLI).
_DEFAULT_ROLES: tuple[str, ...] = ("admin", "producer", "consumer", "auditor")


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

    return SeedBundle(
        path=path,
        name=name,
        description=description,
        released_at=released_at,
        target_tenant_slug=raw.get("target_tenant_slug"),
        tenants=raw.get("tenants") or [],
        actors=raw.get("actors") or [],
        vocabulary=raw.get("vocabulary") or [],
        external_systems=raw.get("external_systems") or [],
        entities=raw.get("entities") or [],
        facts=raw.get("facts") or [],
        bitemporal_attributes=raw.get("bitemporal_attributes") or [],
        edges=raw.get("edges") or [],
        adoptions=raw.get("adoptions") or [],
        progression_definitions=raw.get("progression_definitions") or [],
    )


def deterministic_entity_id(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """UUIDv5 over (tenant_id, name) — stable identity across runs.

    Exposed so external scripts (re-implementing the seed contract or
    debugging by-hand) can compute the same UUID the loader will use.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{tenant_id}:{name}")


class TenantRegistry:
    """Slug → (tenant_id, default_actor_id) lookup, populated as bundles load.

    A bundle's per-tenant sections write to the tenant resolved via this
    registry. The default actor is the first actor declared per tenant
    (or the actor already present in the DB for tenants that exist from
    a prior bootstrap run).
    """

    def __init__(self) -> None:
        self._tenants: dict[str, uuid.UUID] = {}
        self._actors: dict[str, uuid.UUID] = {}  # slug → first actor_id

    def has_tenant(self, slug: str) -> bool:
        return slug in self._tenants

    def register_tenant(self, slug: str, tenant_id: uuid.UUID) -> None:
        self._tenants[slug] = tenant_id

    def register_actor(self, slug: str, actor_id: uuid.UUID) -> None:
        # Keep the first actor seen as the canonical default.
        self._actors.setdefault(slug, actor_id)

    def tenant_id(self, slug: str) -> uuid.UUID:
        try:
            return self._tenants[slug]
        except KeyError as e:
            raise ValueError(
                f"tenant {slug!r} not registered; a `tenants` section must "
                f"declare it before any section referencing it loads. "
                f"Known: {sorted(self._tenants)}"
            ) from e

    def actor_id(self, slug: str) -> uuid.UUID:
        try:
            return self._actors[slug]
        except KeyError as e:
            raise ValueError(
                f"no actor registered for tenant {slug!r}; the seed must "
                f"declare at least one actor per tenant before that tenant's "
                f"sections load. Known: {sorted(self._actors)}"
            ) from e


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
    declared_visibility = entity.get("visibility")
    valid_from = _parse_iso(entity.get("valid_from")) or default_valid_from
    entity_id = deterministic_entity_id(tenant_id, name)

    existing = (
        await session.execute(
            text("SELECT visibility FROM entities WHERE entity_id = :eid"),
            {"eid": entity_id},
        )
    ).first()

    if existing is None:
        visibility = declared_visibility or _DEFAULT_VISIBILITY
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
        # Honour a declared visibility change on an already-existing entity.
        # This is the one mutation a re-run is allowed to perform on the
        # entities table — visibility is the lever a use-case seed flips
        # to make a capability cross-tenant-visible.
        if declared_visibility and existing[0] != declared_visibility:
            await session.execute(
                text(
                    "UPDATE entities SET visibility = :vis WHERE entity_id = :eid"
                ),
                {"vis": declared_visibility, "eid": entity_id},
            )
            counts.visibility_changes += 1

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
# Cross-tenant sections — tenants, actors, adoptions, progression definitions
# ---------------------------------------------------------------------------


async def _apply_tenants(
    session: Any,
    rows: list[dict[str, Any]],
    registry: TenantRegistry,
    counts: LoadCounts,
    now: datetime.datetime,
) -> None:
    """Provision tenants idempotently. Re-runs reuse existing rows.

    Each tenant gets the four default roles + a default ``rate_limits``
    row, matching what ``bootstrap_dev_tenant.py`` would produce. This
    keeps the seed self-sufficient for the multi-tenant demo: an
    operator can run the seed against an empty DB and end up with a
    fully-provisioned set of tenants, no bootstrap-per-tenant required.
    """
    for row in rows:
        slug = row["slug"]
        display_name = row["display_name"]

        existing = (
            await session.execute(
                text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                {"slug": slug},
            )
        ).first()
        if existing is not None:
            tenant_id = uuid.UUID(str(existing[0]))
            counts.tenants_present += 1
        else:
            tenant_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO tenants "
                    "(tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :name, :now, true)"
                ),
                {"tid": tenant_id, "slug": slug, "name": display_name, "now": now},
            )
            counts.tenants_created += 1

        # Default roles per tenant — required for actor_roles inserts.
        for role_name in _DEFAULT_ROLES:
            await session.execute(
                text(
                    "INSERT INTO roles "
                    "(role_id, tenant_id, name, permissions, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :name, '{}', :now) "
                    "ON CONFLICT (tenant_id, name) DO NOTHING"
                ),
                {"tid": tenant_id, "name": role_name, "now": now},
            )

        # Default rate-limit row — mirrors bootstrap_dev_tenant for parity.
        await session.execute(
            text(
                "INSERT INTO rate_limits "
                "(limit_id, tenant_id, actor_id, reads_per_second, writes_per_second, created_at) "
                "VALUES (gen_random_uuid(), :tid, NULL, 100, 10, :now) "
                "ON CONFLICT DO NOTHING"
            ),
            {"tid": tenant_id, "now": now},
        )

        registry.register_tenant(slug, tenant_id)


async def _apply_actors(
    session: Any,
    rows: list[dict[str, Any]],
    registry: TenantRegistry,
    counts: LoadCounts,
    now: datetime.datetime,
) -> None:
    """Provision actors + role grants per tenant. Idempotent."""
    for row in rows:
        slug = row["tenant_slug"]
        display_name = row["display_name"]
        roles = row.get("roles") or list(_DEFAULT_ROLES)
        tenant_id = registry.tenant_id(slug)

        existing = (
            await session.execute(
                text(
                    "SELECT actor_id FROM actors "
                    "WHERE tenant_id = :tid AND display_name = :name AND actor_kind = 'human'"
                ),
                {"tid": tenant_id, "name": display_name},
            )
        ).first()
        if existing is not None:
            actor_id = uuid.UUID(str(existing[0]))
        else:
            actor_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO actors "
                    "(actor_id, tenant_id, display_name, created_at, actor_kind) "
                    "VALUES (:aid, :tid, :name, :now, 'human')"
                ),
                {"aid": actor_id, "tid": tenant_id, "name": display_name, "now": now},
            )
            counts.actors_created += 1

        for role_name in roles:
            result = await session.execute(
                text(
                    "INSERT INTO actor_roles "
                    "(tenant_id, actor_id, role_id, granted_at, granted_by) "
                    "SELECT :tid, :aid, role_id, :now, NULL "
                    "FROM roles WHERE tenant_id = :tid AND name = :name "
                    "ON CONFLICT DO NOTHING"
                ),
                {"tid": tenant_id, "aid": actor_id, "name": role_name, "now": now},
            )
            counts.role_grants_created += result.rowcount or 0

        registry.register_actor(slug, actor_id)


async def _apply_adoptions(
    session: Any,
    rows: list[dict[str, Any]],
    registry: TenantRegistry,
    default_slug: str,
    counts: LoadCounts,
    now: datetime.datetime,
) -> None:
    """Insert cross-tenant adoption_events rows. Idempotent.

    Mirrors the schema in 0009 — tenant_id = consumer tenant,
    provider_capability_id = entity in the provider tenant. The unique
    constraint on (tenant_id, provider_capability_id, consumer_tenant_id)
    keeps re-runs as no-ops. A ``provides_to`` edge is also created so
    the producer-side "who depends on this?" query works.
    """
    for row in rows:
        # `provider_tenant` is optional — when omitted the adoption
        # resolves against the orchestrator's default tenant. This keeps
        # use-case bundles slug-agnostic: they don't hardcode the
        # producer's slug, so the same JSON works whether `make dev-seed`
        # was invoked with --tenant-slug=dev or any other slug.
        provider_slug = row.get("provider_tenant") or default_slug
        consumer_slug = row["consumer_tenant"]
        capability_name = row["provider_capability"]

        provider_tid = registry.tenant_id(provider_slug)
        consumer_tid = registry.tenant_id(consumer_slug)
        capability_id = deterministic_entity_id(provider_tid, capability_name)

        # Consumer actor for the bi-temporal record. If the bundle named
        # one explicitly, use it; else fall back to the consumer tenant's
        # first registered actor.
        consumer_actor_name = row.get("consumer_actor")
        if consumer_actor_name:
            actor_row = (
                await session.execute(
                    text(
                        "SELECT actor_id FROM actors "
                        "WHERE tenant_id = :tid AND display_name = :name"
                    ),
                    {"tid": consumer_tid, "name": consumer_actor_name},
                )
            ).first()
            if actor_row is None:
                raise ValueError(
                    f"adoption: actor {consumer_actor_name!r} not found in "
                    f"tenant {consumer_slug!r}. Declare it in the bundle's "
                    f"`actors` section before this adoption."
                )
            actor_id = uuid.UUID(str(actor_row[0]))
        else:
            actor_id = registry.actor_id(consumer_slug)

        intent = row.get("intent")
        version_pin = row.get("version_pin")

        existing = (
            await session.execute(
                text(
                    "SELECT adoption_id FROM adoption_events "
                    "WHERE tenant_id = :ctid AND provider_capability_id = :pid "
                    "AND consumer_tenant_id = :ctid AND t_invalidated_at IS NULL"
                ),
                {"ctid": consumer_tid, "pid": capability_id},
            )
        ).first()
        if existing is not None:
            continue

        await session.execute(
            text(
                "INSERT INTO adoption_events "
                "(adoption_id, tenant_id, provider_capability_id, consumer_tenant_id, "
                " actor_id, intent, version_pin, "
                " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                "VALUES (gen_random_uuid(), :ctid, :pid, :ctid, "
                "        :aid, :intent, :pin, :now, NULL, :now, NULL)"
            ),
            {
                "ctid": consumer_tid,
                "pid": capability_id,
                "aid": actor_id,
                "intent": intent,
                "pin": version_pin,
                "now": now,
            },
        )
        counts.adoptions_created += 1

        # provides_to edge owned by the PROVIDER tenant. Matches the
        # shape AdoptionService writes: a self-loop on the capability
        # entity, with the consumer tenant encoded in the JSONB
        # properties. The producer-side "who depends on this?" query
        # reads properties.consumer_tenant_id.
        edge_id = deterministic_edge_id(
            provider_tid, capability_id, f"provides_to:{consumer_slug}", capability_id
        )
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
                    "VALUES (:edge_id, :tid, :cap, 'provides_to', :cap, "
                    "        CAST(:props AS JSONB), TRUE, NULL, :now, NULL, :now, NULL, :aid)"
                ),
                {
                    "edge_id": edge_id,
                    "tid": provider_tid,
                    "cap": capability_id,
                    "props": json.dumps(
                        {
                            "consumer_tenant_slug": consumer_slug,
                            "consumer_tenant_id": str(consumer_tid),
                        }
                    ),
                    "now": now,
                    "aid": actor_id,
                },
            )
            counts.edges_created += 1


async def _apply_edges(
    session: Any,
    rows: list[dict[str, Any]],
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    counts: LoadCounts,
    default_valid_from: datetime.datetime,
    now: datetime.datetime,
) -> None:
    """Insert general entity-to-entity edges. Idempotent.

    Used for cross-capability `depends_on` / `integrates_with` /
    `requires` edges that don't fit the `parent` composes-edge slot on
    an entity definition. Edge IDs are deterministic over
    ``(tenant, src, rel, dst)`` so re-runs are no-ops.

    ``provides_to`` is reserved for ``AdoptionService`` (and the loader's
    own ``_apply_adoptions``); writing it via the generic edges section
    is rejected to avoid stepping on that contract.
    """
    for row in rows:
        rel = row["rel"]
        if rel == "provides_to":
            raise ValueError(
                "edges section may not write `provides_to` — that rel is "
                "reserved for adoption events. Use the `adoptions` section "
                "instead, or pick a different rel."
            )

        src_name = row["src"]
        dst_name = row["dst"]
        src_id = deterministic_entity_id(tenant_id, src_name)
        dst_id = deterministic_entity_id(tenant_id, dst_name)
        properties = row.get("properties")
        valid_from = _parse_iso(row.get("valid_from")) or default_valid_from

        # Fail loudly if src or dst doesn't exist — a typo in a bundle
        # would silently create a dangling edge otherwise.
        for label, eid, name in (("src", src_id, src_name), ("dst", dst_id, dst_name)):
            exists = (
                await session.execute(
                    text("SELECT 1 FROM entities WHERE entity_id = :eid"),
                    {"eid": eid},
                )
            ).first()
            if exists is None:
                raise ValueError(
                    f"edge {src_name!r} --{rel}--> {dst_name!r}: {label} "
                    f"entity {name!r} not found in tenant. Load the bundle "
                    f"that defines it first."
                )

        edge_id = deterministic_edge_id(tenant_id, src_id, rel, dst_id)
        existing = (
            await session.execute(
                text("SELECT 1 FROM edges WHERE edge_id = :eid"),
                {"eid": edge_id},
            )
        ).first()
        if existing is not None:
            continue

        await session.execute(
            text(
                "INSERT INTO edges "
                "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                " properties, is_authoritative, sync_run_id, "
                " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                "VALUES (:edge_id, :tid, :src, :rel, :dst, "
                "        CAST(:props AS JSONB), TRUE, NULL, :from, NULL, :from, NULL, :aid)"
            ),
            {
                "edge_id": edge_id,
                "tid": tenant_id,
                "src": src_id,
                "rel": rel,
                "dst": dst_id,
                "props": json.dumps(properties) if properties is not None else None,
                "from": valid_from,
                "aid": actor_id,
            },
        )
        counts.edges_created += 1


async def _apply_progression_definitions(
    session: Any,
    rows: list[dict[str, Any]],
    registry: TenantRegistry,
    target_slug: str,
    counts: LoadCounts,
    now: datetime.datetime,
) -> None:
    """Insert progression_definitions rows. One active row per (tenant, entity_type).

    If a row already exists for (tenant, entity_type) with t_invalidated_at
    IS NULL and the same definition JSONB, the insert is skipped. If the
    definition differs, the existing row is invalidated and a fresh one
    is inserted with a new valid_from — bi-temporal supersession.
    """
    tenant_id = registry.tenant_id(target_slug)
    for row in rows:
        entity_type = row["entity_type"]
        is_advisory = bool(row.get("is_advisory", False))
        definition = row["definition"]

        existing = (
            await session.execute(
                text(
                    "SELECT progression_id, definition::text "
                    "FROM progression_definitions "
                    "WHERE tenant_id = :tid AND entity_type = :etype "
                    "AND t_invalidated_at IS NULL"
                ),
                {"tid": tenant_id, "etype": entity_type},
            )
        ).first()

        new_def_text = json.dumps(definition, sort_keys=True)
        if existing is not None:
            existing_def_text = json.dumps(
                json.loads(existing[1]), sort_keys=True
            )
            if existing_def_text == new_def_text:
                continue
            # Definition has drifted — invalidate the live row.
            await session.execute(
                text(
                    "UPDATE progression_definitions "
                    "SET t_invalidated_at = :now "
                    "WHERE progression_id = :pid"
                ),
                {"now": now, "pid": existing[0]},
            )

        await session.execute(
            text(
                "INSERT INTO progression_definitions "
                "(progression_id, tenant_id, entity_type, definition, is_advisory, "
                " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                "VALUES (gen_random_uuid(), :tid, :etype, CAST(:def AS JSONB), :adv, "
                "        :now, NULL, :now, NULL)"
            ),
            {
                "tid": tenant_id,
                "etype": entity_type,
                "def": new_def_text,
                "adv": is_advisory,
                "now": now,
            },
        )
        counts.progression_definitions_created += 1


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def apply_bundles(
    session: Any,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    bundles: list[SeedBundle],
    *,
    default_tenant_slug: str = "dev",
) -> LoadCounts:
    """Apply each bundle in order. One transaction; partial failure rolls back.

    The caller passes the (tenant_id, actor_id) for the default tenant —
    the one bundles target when they don't specify ``target_tenant_slug``.
    Bundles can also declare ``tenants`` and ``actors`` sections, in which
    case the loader provisions them and uses the registered IDs for
    subsequent sections that reference them by slug.
    """
    counts = LoadCounts()
    now = datetime.datetime.now(tz=datetime.UTC)

    registry = TenantRegistry()
    # Pre-seed the registry with the caller-supplied default so bundles
    # without a `tenants` section still work — they all target the
    # caller's tenant via the default slug.
    registry.register_tenant(default_tenant_slug, tenant_id)
    registry.register_actor(default_tenant_slug, actor_id)

    for bundle in bundles:
        target_slug = bundle.target_tenant_slug or default_tenant_slug

        # Cross-tenant sections first — they may introduce slugs that
        # later per-tenant sections (within the same bundle) reference.
        if bundle.tenants:
            await _apply_tenants(session, bundle.tenants, registry, counts, now)
        if bundle.actors:
            await _apply_actors(session, bundle.actors, registry, counts, now)

        # Per-tenant sections — resolve once per bundle.
        t_id = registry.tenant_id(target_slug)
        a_id = registry.actor_id(target_slug)
        default_valid_from = bundle.released_at or now

        await _apply_vocabulary(session, t_id, bundle.vocabulary, counts, now)
        await _apply_external_systems(session, t_id, bundle.external_systems, counts, now)
        await _apply_entities(
            session, t_id, a_id, bundle.entities, counts, default_valid_from
        )
        await _apply_cross_entity_facts(
            session, t_id, a_id, bundle.facts, counts, default_valid_from
        )
        await _apply_bitemporal_attributes(
            session, t_id, a_id, bundle.bitemporal_attributes, counts, now
        )
        # General edges (depends_on, integrates_with, …) — must run
        # after `entities` so within-bundle src/dst references resolve,
        # and works across bundles too because lexical load order
        # guarantees earlier bundles' entities exist by now.
        if bundle.edges:
            await _apply_edges(
                session, bundle.edges, t_id, a_id, counts, default_valid_from, now
            )

        # Cross-tenant adoptions + per-tenant progression definitions go
        # last, after their referenced entities have been resolved.
        if bundle.adoptions:
            await _apply_adoptions(
                session, bundle.adoptions, registry, target_slug, counts, now
            )
        if bundle.progression_definitions:
            await _apply_progression_definitions(
                session, bundle.progression_definitions, registry, target_slug, counts, now
            )

    return counts


_SEED_DIR_RE = re.compile(r"^\d{2}-")


def all_bundles(seeds_root: Path) -> list[Path]:
    """Every seed bundle, in load order — one command loads everything.

    Walks ``seeds/`` for numbered subdirectories (``00-core``,
    ``01-saltds``, ``02-identity``, …), sorted lexically. Within each,
    all ``.json`` files are loaded in lexical order. The ``NN-`` prefix
    encodes load order: ``00-core`` must run first because every later
    bundle's vocabulary and tenant references resolve against it;
    capability folders run after that, in dependency order
    (foundations like identity before consumers like web-sdk before
    composites like web-runtime).

    New seed bundles drop in by adding a numbered directory — no code
    change needed.
    """
    files: list[Path] = []
    for child in sorted(seeds_root.iterdir()):
        if not child.is_dir():
            continue
        if not _SEED_DIR_RE.match(child.name):
            continue
        files.extend(sorted(child.glob("*.json")))
    return files


# ---------------------------------------------------------------------------
# CLI entry point — `python scripts/seed.py` / `make dev-seed`
# ---------------------------------------------------------------------------

# Ensure the repo root is importable when invoked as a subprocess from
# arbitrary cwd. Without this, `from registry.X import Y` (used by the
# settings lookup in `_seed`) raises ModuleNotFoundError. Done at module
# load rather than inside main() so import-from-cwd-agnostic callers
# still resolve the registry package.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)

_DEFAULT_TENANT_SLUG = "dev"
_DEFAULT_ACTOR_NAME = "dev-admin"
_SEEDS_ROOT = _REPO_ROOT / "seeds"

# Matches the docker-compose default in registry/docker-compose.yml.
_DOCKER_COMPOSE_DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5544/registry"


def _ansi(t: str, code: str, tty: bool) -> str:
    return f"\033[{code}m{t}\033[0m" if tty else t


async def _resolve_dev_tenant(session: Any, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Look up the dev tenant + dev-admin actor UUIDs. Both must already exist."""
    tenant_row = (
        await session.execute(
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
        await session.execute(
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
    print(f"  {dim('External systems')}: {counts.external_systems} new system(s)")
    print(
        f"  {dim('Entities')}: "
        f"{counts.entities_created} created, {counts.entities_present} already present"
    )
    for name, info in counts.per_entity.items():
        marker = "+ created" if info["created_new"] else "= already present"
        print(f"    {name:25s}  {cyan(info['entity_id'])}  {dim(marker)}")
    print(f"  {dim('Edges')}: {counts.edges_created} new (composes + provides_to)")
    print(f"  {dim('Attributes')}: {counts.attributes_created} new attribute row(s)")
    print(
        f"  {dim('Facts')}: "
        f"{counts.facts_created} new, {counts.facts_updated} updated in place"
    )
    print(f"  {dim('External IDs')}: {counts.external_ids_created} new mapping(s)")
    print(f"  {dim('Bitemporal rows')}: {counts.bitemporal_rows_replaced} row(s) inserted")
    if counts.visibility_changes:
        print(f"  {dim('Visibility changes')}: {counts.visibility_changes}")
    if counts.tenants_created or counts.tenants_present:
        print(
            f"  {dim('Tenants')}: "
            f"{counts.tenants_created} created, {counts.tenants_present} already present"
        )
    if counts.actors_created or counts.role_grants_created:
        print(
            f"  {dim('Actors')}: "
            f"{counts.actors_created} created, {counts.role_grants_created} role grant(s)"
        )
    if counts.adoptions_created:
        print(f"  {dim('Adoptions')}: {counts.adoptions_created} cross-tenant adoption(s)")
    if counts.progression_definitions_created:
        print(
            f"  {dim('Progression')}: "
            f"{counts.progression_definitions_created} definition(s) installed"
        )
    print()
    print(bold("Try it:"))
    print("  curl -H 'Authorization: Bearer <token>' http://localhost:8000/v1/capabilities")
    print(
        "  curl -H 'Authorization: Bearer <token>' "
        "'http://localhost:8000/v1/capabilities/salt-design-system?include=components,external_ids'"
    )
    print(
        "  # Time-travel — Salt as of mid-October 2025 (v1.43):\n"
        "  curl -H 'Authorization: Bearer <token>' "
        "'http://localhost:8000/v1/capabilities/salt-design-system?as_of=2025-10-15T00:00:00%2B00:00'"
    )


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
    import asyncio  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Seed a dev tenant from JSON bundles in seeds/.")
    parser.add_argument(
        "--tenant-slug",
        default=_DEFAULT_TENANT_SLUG,
        help=f"Target tenant slug (default: {_DEFAULT_TENANT_SLUG!r}).",
    )
    args = parser.parse_args(argv)

    files = all_bundles(_SEEDS_ROOT)
    if not files:
        print("error: no seed files found under seeds/", file=sys.stderr)
        return 1

    tenant_id, counts = asyncio.run(_seed(args.tenant_slug, files))
    _emit_summary(args.tenant_slug, tenant_id, files, counts)
    return 0


__all__ = [
    "LoadCounts",
    "SeedBundle",
    "TenantRegistry",
    "all_bundles",
    "apply_bundles",
    "deterministic_edge_id",
    "deterministic_entity_id",
    "load_bundle",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
