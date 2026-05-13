"""Seed the dev tenant's vocabulary + two demo capabilities.

Follow-up to `bootstrap_dev_tenant.py`. The vocabulary insert removes
the `unknown vocabulary value` rejection that `POST /v1/capabilities`
otherwise raises on a fresh dev tenant; the two demo capabilities give
`GET /v1/capabilities` and `GET /v1/capabilities/{entity_id}` something
to return.

Idempotent: re-running against the same database produces the same
entity_ids (UUIDv5 derived from the tenant + name) and is a no-op on
existing rows. Hand-seeded demo data — not eval fixtures.

Usage::

    make dev-token       # if you haven't already
    make dev-seed
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# Ensure the repo root is importable when invoked as a subprocess from
# arbitrary cwd. Without this, `from registry.X import Y` raises
# ModuleNotFoundError.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_DEFAULT_TENANT_SLUG = "dev"
_DEFAULT_ACTOR_NAME = "dev-admin"

# Matches the docker-compose default in registry/docker-compose.yml.
_DOCKER_COMPOSE_DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5544/catalog"


# Mirrors the vocabulary the Alembic migrations seed for the `default` tenant.
# A tenant created at runtime (like `dev`) doesn't inherit these — the script
# seeds them per tenant so `POST /v1/capabilities` accepts standard payloads.
_VOCAB_SEEDS: tuple[tuple[str, str], ...] = (
    # entity_type
    ("entity_type", "capability"),
    ("entity_type", "concept"),
    ("entity_type", "operation"),
    ("entity_type", "person"),
    ("entity_type", "system"),
    ("entity_type", "integration"),
    # fact_category
    ("fact_category", "overview"),
    ("fact_category", "concept_glossary"),
    ("fact_category", "limits"),
    ("fact_category", "security_model"),
    ("fact_category", "pricing"),
    ("fact_category", "release_note"),
    ("fact_category", "faq"),
    ("fact_category", "adr"),
    ("fact_category", "rfc"),
    ("fact_category", "dev_doc"),
    ("fact_category", "api_doc"),
    ("fact_category", "catalog_entry"),
    # edge_rel
    ("edge_rel", "concept_of"),
    ("edge_rel", "operation_of"),
    ("edge_rel", "depends_on"),
    ("edge_rel", "integrates_with"),
    ("edge_rel", "event_source"),
    ("edge_rel", "replaced_by"),
    ("edge_rel", "instance_of"),
    ("edge_rel", "requires"),
    ("edge_rel", "conflicts_with"),
    ("edge_rel", "composes"),
    ("edge_rel", "provides_to"),
    # lifecycle_state
    ("lifecycle_state", "alpha"),
    ("lifecycle_state", "beta"),
    ("lifecycle_state", "ga"),
    ("lifecycle_state", "deprecated"),
    ("lifecycle_state", "retired"),
    # visibility
    ("visibility", "private"),
    ("visibility", "tenant-shared"),
    ("visibility", "public"),
    # notification_event_kind
    ("notification_event_kind", "version_published"),
    ("notification_event_kind", "deprecation"),
    ("notification_event_kind", "breaking_change"),
    ("notification_event_kind", "conflict_added"),
    ("notification_event_kind", "integration_added"),
)


# Two demo capabilities. Real-world enterprise examples so the API
# surfaces something recognisable on first open. Free-form attributes
# match the shape the rest of the catalog reads (e.g. `lifecycle.state`).
_DEMO_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "name": "salt-design-system",
        "attributes": {
            "display_name": "Salt Design System",
            "summary": (
                "JPMorgan Chase's open-source enterprise design system — "
                "React component library and design tokens for building "
                "accessible, themeable UIs."
            ),
            "owner": "JPMorgan Chase",
            "homepage": "https://www.saltdesignsystem.com/",
            "repo": "https://github.com/jpmorganchase/salt-ds",
            "lifecycle": {"state": "ga"},
            "current_version": "1.45.0",
            "package_name": "@salt-ds/core",
            "framework": "react",
            "license": "Apache-2.0",
            "accessibility_compliance": "WCAG 2.1 AA",
        },
    },
    {
        "name": "user-preferences",
        "attributes": {
            "display_name": "Enterprise User Preferences Service",
            "summary": (
                "Stores per-user UI/UX preferences (theme, locale, density, "
                "notification opt-ins) with tenant-scoped admin overrides "
                "and audit history."
            ),
            "owner": "Platform Engineering",
            "lifecycle": {"state": "beta"},
        },
    },
)


# Salt Design System components — real components from @salt-ds/core, modeled as
# `concept` child entities with `composes` edges from the Salt parent. Each child
# carries the minimum attributes needed to make the catalog's graph endpoints
# meaningful when a developer browses /docs.
_SALT_COMPONENTS: tuple[dict[str, Any], ...] = (
    {
        "name": "salt-button",
        "display_name": "Button",
        "category": "form-controls",
        "summary": "Standard, accent, and CTA buttons with focus-ring and disabled states.",
    },
    {
        "name": "salt-icon-button",
        "display_name": "IconButton",
        "category": "form-controls",
        "summary": "Icon-only button for compact toolbars and table actions.",
    },
    {
        "name": "salt-input",
        "display_name": "Input",
        "category": "form-controls",
        "summary": "Single-line text input with validation states and adornments.",
    },
    {
        "name": "salt-checkbox",
        "display_name": "Checkbox",
        "category": "form-controls",
        "summary": "Tri-state checkbox supporting indeterminate values.",
    },
    {
        "name": "salt-radio-button",
        "display_name": "RadioButton",
        "category": "form-controls",
        "summary": "Single-select option, used inside RadioButtonGroup.",
    },
    {
        "name": "salt-switch",
        "display_name": "Switch",
        "category": "form-controls",
        "summary": "Two-state toggle for binary settings.",
    },
    {
        "name": "salt-combo-box",
        "display_name": "ComboBox",
        "category": "form-controls",
        "summary": "Searchable dropdown supporting single + multi-select.",
    },
    {
        "name": "salt-form-field",
        "display_name": "FormField",
        "category": "form-controls",
        "summary": "Wraps inputs with label, helper text, and validation status.",
    },
    {
        "name": "salt-card",
        "display_name": "Card",
        "category": "layout",
        "summary": "Surface primitive for grouped content with optional accent border.",
    },
    {
        "name": "salt-tabs",
        "display_name": "Tabs",
        "category": "layout",
        "summary": "Horizontally navigable panels with overflow handling.",
    },
    {
        "name": "salt-accordion",
        "display_name": "Accordion",
        "category": "layout",
        "summary": "Vertically stacked, expandable sections.",
    },
    {
        "name": "salt-flex-layout",
        "display_name": "FlexLayout",
        "category": "layout",
        "summary": "Token-driven flex container with gap + alignment props.",
    },
    {
        "name": "salt-dialog",
        "display_name": "Dialog",
        "category": "overlays",
        "summary": "Modal dialog with focus trap and configurable widths.",
    },
    {
        "name": "salt-drawer",
        "display_name": "Drawer",
        "category": "overlays",
        "summary": "Edge-anchored sliding panel for navigation or filters.",
    },
    {
        "name": "salt-tooltip",
        "display_name": "Tooltip",
        "category": "overlays",
        "summary": "Hover/focus-triggered popover for short hints.",
    },
    {
        "name": "salt-toast",
        "display_name": "Toast",
        "category": "overlays",
        "summary": "Transient notification surface with success / warning / error variants.",
    },
    {
        "name": "salt-data-grid",
        "display_name": "DataGrid",
        "category": "data",
        "summary": "Virtualised, columnar grid with sort, group, and column pinning.",
    },
)


# Facts attached to the Salt parent entity — narrative / docs axis.
_SALT_FACTS: tuple[dict[str, str], ...] = (
    {
        "category": "overview",
        "title": "Overview",
        "body_format": "markdown",
        "body": (
            "Salt is an open-source enterprise design system developed by "
            "JPMorgan Chase. It ships a React component library plus design "
            "tokens (colour, density, corner radius, typography) themed for "
            "both light and dark modes. Salt targets data-dense financial "
            "interfaces and ships with WCAG 2.1 AA conformance. Components "
            "are tree-shakeable from @salt-ds/core; theming is applied at the "
            "<SaltProvider> boundary."
        ),
    },
    {
        "category": "release_note",
        "title": "v1.45.0 release notes",
        "body_format": "markdown",
        "body": (
            "v1.45.0 — DataGrid gains column pinning + virtualised row "
            "rendering for ≥ 100k-row datasets; Dialog focus-trap fix for "
            "nested Drawer composition; new density token `density-touch` "
            "for tablet form-factor; ComboBox now exposes a controlled "
            "`open` prop. No breaking changes vs 1.44.x."
        ),
    },
)


# External systems to ensure exist on the dev tenant — these are the
# upstream registries our demo capability maps to. New entries here
# unlock new `lookup_by_external_id` paths.
_EXTERNAL_SYSTEMS: tuple[dict[str, str], ...] = (
    {
        "slug": "npm",
        "display_name": "npm registry",
        "url_template": "https://www.npmjs.com/package/{external_id}",
    },
    {
        "slug": "github",
        "display_name": "GitHub",
        "url_template": "https://github.com/{external_id}",
    },
)


# entity_external_ids mappings to register against the Salt entity so
# `GET /v1/entities?external_system=npm&external_id=@salt-ds/core` and the
# matching MCP `lookup_by_external_id` tool resolve out of the box.
_SALT_EXTERNAL_IDS: tuple[dict[str, str], ...] = (
    {
        "system": "npm",
        "external_id": "@salt-ds/core",
        "url": "https://www.npmjs.com/package/@salt-ds/core",
    },
    {
        "system": "github",
        "external_id": "jpmorganchase/salt-ds",
        "url": "https://github.com/jpmorganchase/salt-ds",
    },
)


# ---------------------------------------------------------------------------
# Salt version history — three releases stitched into the bitemporal model
# so `?as_of=2025-10-01` returns Salt @ v1.43, etc.
# ---------------------------------------------------------------------------


# Release dates for the three Salt versions we model. Dates are plausible
# stand-ins; the goal is to give time-travel queries something to navigate.
_SALT_V143_RELEASED = datetime.datetime(2025, 9, 15, tzinfo=datetime.UTC)
_SALT_V144_RELEASED = datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC)
_SALT_V145_RELEASED = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)


# Components added in v1.44 (the baseline 17 components all date to v1.43).
_SALT_V144_COMPONENTS: tuple[dict[str, str], ...] = (
    {
        "name": "salt-filter-bar",
        "display_name": "FilterBar",
        "category": "data",
        "summary": "Stackable filter chips with type-ahead value selection for table / list filtering.",
    },
    {
        "name": "salt-stepper-input",
        "display_name": "StepperInput",
        "category": "form-controls",
        "summary": "Numeric input with increment / decrement buttons; honours min/max/step props.",
    },
)


# Per-version release notes. Each becomes a Fact with category='release_note'
# valid_from=release_date so an `?as_of=...` query returns the right set.
_SALT_VERSION_RELEASE_NOTES: tuple[dict[str, str | datetime.datetime], ...] = (
    {
        "version": "1.43.0",
        "valid_from": _SALT_V143_RELEASED,
        "title": "v1.43.0 release notes",
        "body": (
            "v1.43.0 — Initial 1.x stable line. 15 components ship with WCAG 2.1 AA "
            "conformance: Button, IconButton, Input, Checkbox, RadioButton, Switch, "
            "ComboBox, FormField, Card, Tabs, Accordion, FlexLayout, Dialog, Drawer, "
            "Tooltip, Toast, DataGrid. Tree-shakeable from @salt-ds/core. Theming "
            "via <SaltProvider density={...} mode={'light'|'dark'}>.\n\n"
            "Breaking changes vs 0.x: SaltProvider API stabilised (props renamed: "
            "`appearance` → `mode`, `applyClassesTo` removed). Migration guide at "
            "https://www.saltdesignsystem.com/salt/v1-migration."
        ),
    },
    {
        "version": "1.44.0",
        "valid_from": _SALT_V144_RELEASED,
        "title": "v1.44.0 release notes",
        "body": (
            "v1.44.0 — Two new components: **FilterBar** (`salt-filter-bar`) for "
            "table / list filtering, and **StepperInput** (`salt-stepper-input`) "
            "for numeric step controls. Toast gains a `placement` prop "
            "(`top-right` | `bottom-right` | `top-center` | `bottom-center`); "
            "default `top-right` matches prior visual behaviour.\n\n"
            "Non-breaking: ComboBox value-selection events now also fire on "
            "Enter-when-highlighted (previously only mouse click + Tab). Existing "
            "handlers continue to work; opt out via `selectOnEnter={false}`."
        ),
    },
    {
        "version": "1.45.0",
        "valid_from": _SALT_V145_RELEASED,
        "title": "v1.45.0 release notes",
        "body": (
            "v1.45.0 — DataGrid gains column pinning + virtualised row "
            "rendering for ≥ 100k-row datasets; Dialog focus-trap fix for "
            "nested Drawer composition; new density token `density-touch` "
            "for tablet form-factor; ComboBox now exposes a controlled "
            "`open` prop. No breaking changes vs 1.44.x."
        ),
    },
)


# ADR + dev_doc + security_model facts. Each carries an explicit valid_from
# so a time-travel query lands on the right state.
_SALT_NARRATIVE_FACTS: tuple[dict[str, str | datetime.datetime], ...] = (
    {
        "category": "adr",
        "title": "Architectural decision: React, not Web Components",
        "body_format": "markdown",
        "valid_from": _SALT_V143_RELEASED,
        "body": (
            "# Status\nAccepted (2025-08-12)\n\n"
            "# Context\nEnterprise consumers run a mix of React, Angular, and "
            "internal frameworks. A framework-agnostic Web-Components layer was "
            "considered to maximise reach.\n\n"
            "# Decision\nShip React-first. The team's expertise + the cost of "
            "shadow-DOM-aware theming and the lack of native form-association "
            "support outweigh the cross-framework upside. We will not block "
            "framework-agnostic wrapper packages built by consumers.\n\n"
            "# Consequences\nNon-React apps integrate via render-into-element "
            "patterns or community wrappers. The team owns React + Tokens; the "
            "community owns the bridges."
        ),
    },
    {
        "category": "security_model",
        "title": "Security model",
        "body_format": "markdown",
        "valid_from": _SALT_V143_RELEASED,
        "body": (
            "Salt is a client-rendering library; it never makes outbound network "
            "calls of its own, never reads/writes local storage by default, and "
            "depends on zero runtime-only third-party trackers. CSP-compatible: "
            "all styling is via class names against `<style>` blocks generated "
            "at build time. No inline `<script>` or runtime `eval()`. Trusted "
            "Types compatible — no innerHTML usage. Components MUST receive "
            "untrusted text via standard React children; consumers handle "
            "HTML-sanitisation upstream if they need it."
        ),
    },
    {
        "category": "dev_doc",
        "title": "Theming guide",
        "body_format": "markdown",
        "valid_from": _SALT_V143_RELEASED,
        "body": (
            "Wrap your app in `<SaltProvider density={...} mode={'light'|'dark'}>`. "
            "All components inherit from the provider; nested providers override. "
            "Custom themes: extend the default token set via a CSS variable layer "
            "(see `@salt-ds/theme`). Density: `low` / `medium` / `high` / `touch` "
            "(touch added in v1.45)."
        ),
    },
)


def _ansi(text: str, code: str, tty: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if tty else text


async def _resolve_dev_tenant(
    session: object,
    slug: str,
) -> tuple[uuid.UUID, uuid.UUID]:
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
            f"error: actor {_DEFAULT_ACTOR_NAME!r} not found in tenant {slug!r}. " f"Run `make dev-token` first."
        )
    actor_id = uuid.UUID(str(actor_row[0]))
    return tenant_id, actor_id


async def _seed_vocab(session: object, tenant_id: uuid.UUID) -> int:
    """Insert vocabulary rows; return count of new rows inserted."""
    now = datetime.datetime.now(tz=datetime.UTC)
    inserted = 0
    for kind, value in _VOCAB_SEEDS:
        result = await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO vocabulary_values "
                "(vocab_id, tenant_id, kind, value, is_system, created_at) "
                "VALUES (gen_random_uuid(), :tid, :kind, :value, FALSE, :now) "
                "ON CONFLICT (tenant_id, kind, value) DO NOTHING"
            ),
            {"tid": tenant_id, "kind": kind, "value": value, "now": now},
        )
        inserted += result.rowcount or 0
    return inserted


def _deterministic_entity_id(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """UUIDv5 over (tenant_id, name) so re-runs yield the same entity_id."""
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{tenant_id}:{name}")


def _deterministic_edge_id(tenant_id: uuid.UUID, src: uuid.UUID, rel: str, dst: uuid.UUID) -> uuid.UUID:
    """UUIDv5 over (tenant, src, rel, dst) so re-runs yield the same edge_id."""
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{tenant_id}:{src}:{rel}:{dst}")


async def _upsert_attribute(
    session: object,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID,
    key: str,
    value: Any,
    now: datetime.datetime,
) -> bool:
    """Insert one attribute row if no current row exists for (entity_id, key).

    Returns True iff a new row was inserted. "Current" means
    t_valid_to IS NULL AND t_invalidated_at IS NULL — the bitemporal
    pattern: never invalidate an existing live row from a seed script.
    """
    import json  # noqa: PLC0415

    existing = (
        await session.execute(  # type: ignore[attr-defined]
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

    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO attributes "
            "(attr_id, tenant_id, entity_id, key, value, "
            " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
            "VALUES (gen_random_uuid(), :tid, :eid, :key, CAST(:value AS JSONB), "
            "        :now, NULL, :now, NULL, :aid)"
        ),
        {
            "tid": tenant_id,
            "eid": entity_id,
            "key": key,
            "value": json.dumps(value),
            "now": now,
            "aid": actor_id,
        },
    )
    return True


async def _seed_capabilities(
    session: object,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> list[tuple[str, uuid.UUID, bool]]:
    """Insert demo capabilities + their attributes. Idempotent.

    Each call upserts the entity (insert if missing) and each attribute
    individually (insert if no current row for that key) — so re-runs
    after the seed schema grows still fill in the new attributes on
    pre-existing entities.

    Returns one tuple per capability: (name, entity_id, created_new_entity).
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    results: list[tuple[str, uuid.UUID, bool]] = []
    for cap in _DEMO_CAPABILITIES:
        name: str = cap["name"]
        attributes: dict[str, Any] = cap["attributes"]
        entity_id = _deterministic_entity_id(tenant_id, name)

        existing = (
            await session.execute(  # type: ignore[attr-defined]
                text("SELECT 1 FROM entities WHERE entity_id = :eid"),
                {"eid": entity_id},
            )
        ).first()

        created_new = existing is None
        if created_new:
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, external_id, "
                    " is_active, created_at, created_by, visibility) "
                    "VALUES (:eid, :tid, 'capability', :name, NULL, TRUE, :now, :aid, 'private')"
                ),
                {"eid": entity_id, "tid": tenant_id, "name": name, "now": now, "aid": actor_id},
            )

        for key, value in attributes.items():
            await _upsert_attribute(session, tenant_id, entity_id, actor_id, key, value, now)

        results.append((name, entity_id, created_new))
    return results


async def _seed_salt_components(
    session: object,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    salt_entity_id: uuid.UUID,
) -> tuple[int, int]:
    """Insert Salt component child entities + `composes` edges. Idempotent.

    Returns (new_components, new_edges).
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    new_components = 0
    new_edges = 0

    for component in _SALT_COMPONENTS:
        comp_name: str = component["name"]
        comp_id = _deterministic_entity_id(tenant_id, comp_name)

        existing_entity = (
            await session.execute(  # type: ignore[attr-defined]
                text("SELECT 1 FROM entities WHERE entity_id = :eid"),
                {"eid": comp_id},
            )
        ).first()

        if existing_entity is None:
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, external_id, "
                    " is_active, created_at, created_by, visibility) "
                    "VALUES (:eid, :tid, 'concept', :name, NULL, TRUE, :now, :aid, 'private')"
                ),
                {"eid": comp_id, "tid": tenant_id, "name": comp_name, "now": now, "aid": actor_id},
            )
            new_components += 1

        for key in ("display_name", "category", "summary"):
            await _upsert_attribute(session, tenant_id, comp_id, actor_id, key, component[key], now)

        edge_id = _deterministic_edge_id(tenant_id, salt_entity_id, "composes", comp_id)
        existing_edge = (
            await session.execute(  # type: ignore[attr-defined]
                text("SELECT 1 FROM edges WHERE edge_id = :eid"),
                {"eid": edge_id},
            )
        ).first()

        if existing_edge is None:
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " properties, is_authoritative, sync_run_id, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                    "VALUES (:edge_id, :tid, :src, 'composes', :dst, "
                    "        NULL, TRUE, NULL, :now, NULL, :now, NULL, :aid)"
                ),
                {
                    "edge_id": edge_id,
                    "tid": tenant_id,
                    "src": salt_entity_id,
                    "dst": comp_id,
                    "now": now,
                    "aid": actor_id,
                },
            )
            new_edges += 1

    return new_components, new_edges


async def _seed_salt_facts(
    session: object,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    salt_entity_id: uuid.UUID,
) -> int:
    """Insert overview + release-note facts on the Salt entity. Idempotent.

    Idempotency: a fact is considered "the same" if (entity_id, category, body)
    already exist with t_invalidated_at IS NULL. This keeps re-runs cheap
    without a unique index that would force schema knowledge here.
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    new_facts = 0
    for fact in _SALT_FACTS:
        existing = (
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "SELECT fact_id, title, body_format FROM facts WHERE entity_id = :eid "
                    "AND category = :cat AND body = :body "
                    "AND t_invalidated_at IS NULL LIMIT 1"
                ),
                {"eid": salt_entity_id, "cat": fact["category"], "body": fact["body"]},
            )
        ).first()
        if existing is not None:
            # Fact already present — but the migration may have backfilled
            # title/body_format. Update to the explicit values we want.
            if existing[1] != fact["title"] or existing[2] != fact["body_format"]:
                await session.execute(  # type: ignore[attr-defined]
                    text("UPDATE facts SET title = :title, body_format = :body_format " "WHERE fact_id = :fid"),
                    {
                        "title": fact["title"],
                        "body_format": fact["body_format"],
                        "fid": existing[0],
                    },
                )
            continue

        await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO facts "
                "(fact_id, tenant_id, entity_id, category, title, body, body_format, "
                " is_authoritative, is_authoritative_superseded, sync_run_id, "
                " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                "VALUES (gen_random_uuid(), :tid, :eid, :cat, :title, :body, :body_format, "
                "        TRUE, FALSE, NULL, :now, NULL, :now, NULL, :aid)"
            ),
            {
                "tid": tenant_id,
                "eid": salt_entity_id,
                "cat": fact["category"],
                "title": fact["title"],
                "body": fact["body"],
                "body_format": fact["body_format"],
                "now": now,
                "aid": actor_id,
            },
        )
        new_facts += 1
    return new_facts


async def _seed_salt_history(
    session: object,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    salt_entity_id: uuid.UUID,
) -> dict[str, int]:
    """Layer Salt's 3-version history into the bitemporal model.

    Three things happen:

    1. Backdate the 17 existing components + their `composes` edges to
       v1.43's release date (they're all 1.43-era components).
    2. Insert v1.44's two new components (FilterBar, StepperInput) + edges
       with t_valid_from = v1.44 release date.
    3. Rewrite the Salt parent's ``current_version`` attribute as three
       bitemporal rows so `?as_of=2025-10-01` returns "1.43.0", and so on.
    4. Insert release-note Facts for v1.43 + v1.44 (v1.45 already exists);
       set their valid_from to each release date.
    5. Insert ADR / security_model / dev_doc narrative facts dated to
       v1.43's release.

    Idempotent: every insert checks for existing rows first; the version-
    attribute step UPDATEs the existing row rather than appending duplicates.
    """
    import json  # noqa: PLC0415

    counts = {
        "components_backdated": 0,
        "edges_backdated": 0,
        "components_v144": 0,
        "edges_v144": 0,
        "version_attr_rows": 0,
        "release_notes": 0,
        "narrative_facts": 0,
    }
    now = datetime.datetime.now(tz=datetime.UTC)

    # 1) Backdate the 17 existing components (entities + composes edges) to v1.43.
    component_ids = [_deterministic_entity_id(tenant_id, c["name"]) for c in _SALT_COMPONENTS]
    if component_ids:
        result = await session.execute(  # type: ignore[attr-defined]
            text(
                "UPDATE entities SET created_at = :v143 "
                "WHERE entity_id = ANY(CAST(:ids AS UUID[])) AND created_at > :v143"
            ),
            {"v143": _SALT_V143_RELEASED, "ids": [str(eid) for eid in component_ids]},
        )
        counts["components_backdated"] = result.rowcount or 0

        edge_ids = [_deterministic_edge_id(tenant_id, salt_entity_id, "composes", cid) for cid in component_ids]
        result = await session.execute(  # type: ignore[attr-defined]
            text(
                "UPDATE edges SET t_valid_from = :v143, t_ingested_at = :v143 "
                "WHERE edge_id = ANY(CAST(:ids AS UUID[])) AND t_valid_from > :v143"
            ),
            {"v143": _SALT_V143_RELEASED, "ids": [str(eid) for eid in edge_ids]},
        )
        counts["edges_backdated"] = result.rowcount or 0

    # 2) Insert the two v1.44 components if missing; create their edges.
    for component in _SALT_V144_COMPONENTS:
        comp_id = _deterministic_entity_id(tenant_id, component["name"])
        existing = (
            await session.execute(  # type: ignore[attr-defined]
                text("SELECT 1 FROM entities WHERE entity_id = :eid"),
                {"eid": comp_id},
            )
        ).first()
        if existing is None:
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, external_id, "
                    " is_active, created_at, created_by, visibility) "
                    "VALUES (:eid, :tid, 'concept', :name, NULL, TRUE, :now, :aid, 'private')"
                ),
                {
                    "eid": comp_id,
                    "tid": tenant_id,
                    "name": component["name"],
                    "now": _SALT_V144_RELEASED,
                    "aid": actor_id,
                },
            )
            counts["components_v144"] += 1

        for key in ("display_name", "category", "summary"):
            await _upsert_attribute(session, tenant_id, comp_id, actor_id, key, component[key], _SALT_V144_RELEASED)

        edge_id = _deterministic_edge_id(tenant_id, salt_entity_id, "composes", comp_id)
        existing_edge = (
            await session.execute(  # type: ignore[attr-defined]
                text("SELECT 1 FROM edges WHERE edge_id = :eid"),
                {"eid": edge_id},
            )
        ).first()
        if existing_edge is None:
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " properties, is_authoritative, sync_run_id, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                    "VALUES (:edge_id, :tid, :src, 'composes', :dst, "
                    "        NULL, TRUE, NULL, :v144, NULL, :v144, NULL, :aid)"
                ),
                {
                    "edge_id": edge_id,
                    "tid": tenant_id,
                    "src": salt_entity_id,
                    "dst": comp_id,
                    "v144": _SALT_V144_RELEASED,
                    "aid": actor_id,
                },
            )
            counts["edges_v144"] += 1

    # 3) Rewrite the Salt parent's `current_version` attribute as three
    # bitemporal rows. First, blow away any existing live current_version row
    # — we're going to set the right three.
    await session.execute(  # type: ignore[attr-defined]
        text(
            "DELETE FROM attributes "
            "WHERE entity_id = :eid AND key = 'current_version' "
            "AND t_invalidated_at IS NULL"
        ),
        {"eid": salt_entity_id},
    )
    version_rows = (
        ("1.43.0", _SALT_V143_RELEASED, _SALT_V144_RELEASED),
        ("1.44.0", _SALT_V144_RELEASED, _SALT_V145_RELEASED),
        ("1.45.0", _SALT_V145_RELEASED, None),  # still current
    )
    for version_str, valid_from, valid_to in version_rows:
        await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO attributes "
                "(attr_id, tenant_id, entity_id, key, value, "
                " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                "VALUES (gen_random_uuid(), :tid, :eid, 'current_version', "
                "        CAST(:value AS JSONB), :from, :to, :now, NULL, :aid)"
            ),
            {
                "tid": tenant_id,
                "eid": salt_entity_id,
                "value": json.dumps(version_str),
                "from": valid_from,
                "to": valid_to,
                "now": now,
                "aid": actor_id,
            },
        )
        counts["version_attr_rows"] += 1

    # 4) Release-note facts. v1.45 already exists from the basic seed —
    # update its valid_from to the release date; add the v1.43 and v1.44
    # entries if missing.
    for note in _SALT_VERSION_RELEASE_NOTES:
        body = note["body"]
        title = note["title"]
        valid_from = note["valid_from"]
        existing = (
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "SELECT fact_id FROM facts WHERE entity_id = :eid "
                    "AND category = 'release_note' AND title = :title "
                    "AND t_invalidated_at IS NULL LIMIT 1"
                ),
                {"eid": salt_entity_id, "title": title},
            )
        ).first()
        if existing is None:
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO facts "
                    "(fact_id, tenant_id, entity_id, category, title, body, body_format, "
                    " is_authoritative, is_authoritative_superseded, sync_run_id, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                    "VALUES (gen_random_uuid(), :tid, :eid, 'release_note', :title, "
                    "        :body, 'markdown', TRUE, FALSE, NULL, "
                    "        :from, NULL, :from, NULL, :aid)"
                ),
                {
                    "tid": tenant_id,
                    "eid": salt_entity_id,
                    "title": title,
                    "body": body,
                    "from": valid_from,
                    "aid": actor_id,
                },
            )
            counts["release_notes"] += 1
        else:
            # Keep the existing body but make sure valid_from matches the release date.
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "UPDATE facts SET t_valid_from = :from, t_ingested_at = :from, "
                    "                 body = :body "
                    "WHERE fact_id = :fid"
                ),
                {"fid": existing[0], "from": valid_from, "body": body},
            )

    # 5) Narrative facts (ADR, security_model, dev_doc). Same idempotency
    # pattern as release notes.
    for fact in _SALT_NARRATIVE_FACTS:
        existing = (
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "SELECT 1 FROM facts WHERE entity_id = :eid "
                    "AND category = :cat AND title = :title "
                    "AND t_invalidated_at IS NULL LIMIT 1"
                ),
                {
                    "eid": salt_entity_id,
                    "cat": fact["category"],
                    "title": fact["title"],
                },
            )
        ).first()
        if existing is not None:
            continue
        await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO facts "
                "(fact_id, tenant_id, entity_id, category, title, body, body_format, "
                " is_authoritative, is_authoritative_superseded, sync_run_id, "
                " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                "VALUES (gen_random_uuid(), :tid, :eid, :cat, :title, "
                "        :body, :body_format, TRUE, FALSE, NULL, "
                "        :from, NULL, :from, NULL, :aid)"
            ),
            {
                "tid": tenant_id,
                "eid": salt_entity_id,
                "cat": fact["category"],
                "title": fact["title"],
                "body": fact["body"],
                "body_format": fact["body_format"],
                "from": fact["valid_from"],
                "aid": actor_id,
            },
        )
        counts["narrative_facts"] += 1

    return counts


async def _seed_external_systems(session: object, tenant_id: uuid.UUID) -> int:
    """Register the external systems the dev seed uses. Idempotent."""
    now = datetime.datetime.now(tz=datetime.UTC)
    inserted = 0
    for system in _EXTERNAL_SYSTEMS:
        result = await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO external_systems "
                "(slug, tenant_id, display_name, url_template, description, created_at) "
                "VALUES (:slug, :tid, :name, :url, NULL, :now) "
                "ON CONFLICT (tenant_id, slug) DO NOTHING"
            ),
            {
                "slug": system["slug"],
                "tid": tenant_id,
                "name": system["display_name"],
                "url": system["url_template"],
                "now": now,
            },
        )
        inserted += result.rowcount or 0
    return inserted


async def _seed_salt_external_ids(
    session: object,
    tenant_id: uuid.UUID,
    salt_entity_id: uuid.UUID,
) -> int:
    """Register the Salt entity's external-system mappings. Idempotent.

    The unique constraint is (tenant_id, external_system_slug, external_id);
    re-runs ON CONFLICT DO NOTHING.
    """
    inserted = 0
    for mapping in _SALT_EXTERNAL_IDS:
        result = await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO entity_external_ids "
                "(external_id_pk, entity_id, tenant_id, external_system_slug, "
                " external_id, url, metadata_jsonb) "
                "VALUES (gen_random_uuid(), :eid, :tid, :system, :ext, :url, NULL) "
                "ON CONFLICT (tenant_id, external_system_slug, external_id) DO NOTHING"
            ),
            {
                "eid": salt_entity_id,
                "tid": tenant_id,
                "system": mapping["system"],
                "ext": mapping["external_id"],
                "url": mapping["url"],
            },
        )
        inserted += result.rowcount or 0
    return inserted


def _emit_summary(
    tenant_slug: str,
    tenant_id: uuid.UUID,
    vocab_inserted: int,
    capabilities: list[tuple[str, uuid.UUID, bool]],
    salt_extras: dict[str, int],
) -> None:
    tty = sys.stdout.isatty()
    bold = lambda s: _ansi(s, "1", tty)  # noqa: E731
    cyan = lambda s: _ansi(s, "36", tty)  # noqa: E731
    dim = lambda s: _ansi(s, "2", tty)  # noqa: E731

    print(bold(f"Seeded dev tenant {tenant_slug!r} ({tenant_id})"))
    print(f"  {dim('Vocabulary')}: {vocab_inserted} new value(s) inserted " f"(plus pre-existing values left alone)")
    print(f"  {dim('Capabilities')}:")
    for name, entity_id, created in capabilities:
        marker = "+ created" if created else "= already present"
        print(f"    {name:25s}  {cyan(str(entity_id))}  {dim(marker)}")
    print(
        f"  {dim('Salt extras')}: "
        f"{salt_extras['components']} new component(s), "
        f"{salt_extras['edges']} new edge(s), "
        f"{salt_extras['facts']} new fact(s), "
        f"{salt_extras.get('external_systems', 0)} new external system(s), "
        f"{salt_extras.get('external_ids', 0)} new external ID mapping(s)"
    )
    print(
        f"  {dim('Salt history')}: "
        f"{salt_extras.get('history_version_rows', 0)} version row(s), "
        f"{salt_extras.get('history_release_notes', 0)} new release-note(s), "
        f"{salt_extras.get('history_v144_components', 0)} new v1.44 component(s), "
        f"{salt_extras.get('history_narrative_facts', 0)} new narrative fact(s)"
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
        "  # All release notes:\n"
        "  curl -H 'Authorization: Bearer <token>' "
        "'http://localhost:8000/v1/capabilities/<salt-id>/artifacts?category=release_note'"
    )
    print(
        "  curl -H 'Authorization: Bearer <token>' "
        "'http://localhost:8000/v1/entities?external_system=npm&external_id=@salt-ds/core'"
    )
    print(f"  {dim('# /docs → Authorize → bearerAuth → expand retrieval → Try it out')}")


async def _seed(
    tenant_slug: str,
) -> tuple[uuid.UUID, int, list[tuple[str, uuid.UUID, bool]], dict[str, int]]:
    from registry.config import get_settings  # noqa: PLC0415

    if "DATABASE_URL" not in os.environ:  # config: intentional
        os.environ["DATABASE_URL"] = _DOCKER_COMPOSE_DATABASE_URL
        print(
            f"DATABASE_URL not set; defaulting to docker-compose Postgres "
            f"({_DOCKER_COMPOSE_DATABASE_URL}). Export DATABASE_URL to override.",
            file=sys.stderr,
        )
    database_url = get_settings().database_url

    engine = create_async_engine(
        database_url,
        connect_args={"prepared_statement_cache_size": 0},  # PgBouncer transaction mode
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            tenant_id, actor_id = await _resolve_dev_tenant(session, tenant_slug)
            vocab_inserted = await _seed_vocab(session, tenant_id)
            capabilities = await _seed_capabilities(session, tenant_id, actor_id)

            salt_entity_id = next(
                (eid for name, eid, _ in capabilities if name == "salt-design-system"),
                None,
            )
            salt_extras: dict[str, int] = {
                "components": 0,
                "edges": 0,
                "facts": 0,
                "external_systems": 0,
                "external_ids": 0,
                "history_version_rows": 0,
                "history_release_notes": 0,
                "history_v144_components": 0,
                "history_narrative_facts": 0,
            }
            external_systems = await _seed_external_systems(session, tenant_id)
            salt_extras["external_systems"] = external_systems
            if salt_entity_id is not None:
                comps, edges = await _seed_salt_components(session, tenant_id, actor_id, salt_entity_id)
                facts = await _seed_salt_facts(session, tenant_id, actor_id, salt_entity_id)
                ext_ids = await _seed_salt_external_ids(session, tenant_id, salt_entity_id)
                history = await _seed_salt_history(session, tenant_id, actor_id, salt_entity_id)
                salt_extras["components"] = comps
                salt_extras["edges"] = edges
                salt_extras["facts"] = facts
                salt_extras["external_ids"] = ext_ids
                salt_extras["history_version_rows"] = history["version_attr_rows"]
                salt_extras["history_release_notes"] = history["release_notes"]
                salt_extras["history_v144_components"] = history["components_v144"]
                salt_extras["history_narrative_facts"] = history["narrative_facts"]
    finally:
        await engine.dispose()

    return tenant_id, vocab_inserted, capabilities, salt_extras


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Seed dev-tenant vocab + demo capabilities.")
    parser.add_argument("--tenant-slug", default=_DEFAULT_TENANT_SLUG)
    args = parser.parse_args(argv)

    tenant_id, vocab_inserted, capabilities, salt_extras = asyncio.run(_seed(args.tenant_slug))
    _emit_summary(args.tenant_slug, tenant_id, vocab_inserted, capabilities, salt_extras)
    return 0


if __name__ == "__main__":
    sys.exit(main())
