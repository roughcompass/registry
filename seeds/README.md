# `seeds/` — dev-tenant data, loaded by `make dev-seed`

A single command, [`make dev-seed`](../Makefile), reads every JSON file
under this directory and applies it via [`../scripts/seed.py`](../scripts/seed.py).
Re-runs are idempotent — entity IDs, edges, attributes, and facts stay
stable.

## Layout

```
seeds/
  README.md                         ← this file (schema reference + conventions)
  00-core/                          ← infrastructure: vocab, tenants, actors
    01-vocabulary.json
    02-tenants.json
  01-saltds/                        ← Salt Design System: the consumer-facing demo
    01-baseline.json                ← Salt parent + npm/github external systems
    02-v1.43.json                   ← 37 components, real TS prop interfaces
    03-v1.44.json                   ← +1 component
    04-v1.45.json                   ← +1 component + package-level interface
    05-visibility.json              ← flip Salt entities to `public`
    06-adoptions.json               ← 8 cross-tenant adoptions
    07-progression.json             ← capability state machine
  02-identity/                      ← foundation: auth + identity claims
    01-baseline.json
  03-prefs/                         ← preferences (depends on identity)
    01-baseline.json
  04-notifications/                 ← multi-channel (depends on identity + prefs)
    01-baseline.json
  05-web-sdk/                       ← TypeScript SDK (depends on identity, prefs, notifications)
    01-baseline.json                ← capability stub + bitemporal version history
    02-v2.0.json                    ← v2 interface (sync getUserIdentity, slim shape)
    03-v3.0.json                    ← v3 interface (BREAKING: async, rich shape) + migration guide
  06-web-runtime/                   ← experience-frame config (depends on web-sdk, identity, prefs)
    01-baseline.json                ← zones, slots, menus, audiences, entitlements
  07-owners/                        ← technical + product owners (person entities + edges)
    01-owners.json                  ← 8 people, 12 ownership edges (owned_by + product_owned_by)
```

## Conventions

- **Numbered directories** (`00-core`, `01-saltds`, `02-identity`, …) load in
  lexical order. Earlier folders contain data later folders depend on.
- **Numbered files within a directory** likewise. Salt's per-version
  bundles use this ordering (`02-v1.43` before `03-v1.44`) so the
  bitemporal history reads correctly.
- **Folder name encodes the capability slug** — files inside don't
  repeat the slug. `seeds/02-identity/01-baseline.json` is enough.

## Adding a new bundle

Drop a new numbered directory under `seeds/`, drop one or more
`NN-name.json` files in it, and `make dev-seed` picks them up. No code
change required. Pick a directory number that places the new bundle
correctly relative to its dependencies.

---

# Seed-file format (schema_version = 1)

A seed file is a JSON document describing data to load into a tenant.
The loader reads one or more files in order and applies them
idempotently — re-running yields the same `entity_id`s (UUIDv5 over
`(tenant_id, name)` ensures stability).

## Top-level shape

```json
{
  "schema_version": 1,
  "name": "saltds-v1.45",
  "description": "Free-form blurb shown in seeder output.",
  "released_at": "2026-01-15T00:00:00Z",
  "target_tenant_slug": "dev",

  "tenants":             [ /* see Tenants */ ],
  "actors":              [ /* see Actors */ ],
  "vocabulary":          [ /* see Vocabulary */ ],
  "external_systems":    [ /* see External systems */ ],
  "entities":            [ /* see Entities */ ],
  "facts":               [ /* see Cross-entity facts */ ],
  "bitemporal_attributes": [ /* see Bitemporal attributes */ ],
  "edges":               [ /* see Edges */ ],
  "adoptions":           [ /* see Adoptions */ ],
  "progression_definitions": [ /* see Progression definitions */ ]
}
```

All sections are optional. A file may contain just `vocabulary`, or just
`entities`, etc. Loading order across files matters; within a file the
loader processes sections in the order listed above.

`target_tenant_slug` selects which tenant this bundle's per-tenant
sections write to. Defaults to the tenant the orchestrator was invoked
with (`dev` for `make dev-seed`). Cross-tenant sections (`tenants`,
`actors`, `adoptions`) reference tenants by slug explicitly and ignore
this field.

`released_at` is optional and used as the default `valid_from` for
rows that don't specify one.

## Vocabulary

Closed-vocabulary values inserted with
`ON CONFLICT (tenant_id, kind, value) DO NOTHING`.

```json
"vocabulary": [
  {"kind": "entity_type", "value": "capability"},
  {"kind": "edge_rel",    "value": "composes"}
]
```

## External systems

Inserted with `ON CONFLICT (tenant_id, slug) DO NOTHING`.

```json
"external_systems": [
  {
    "slug": "npm",
    "display_name": "npm registry",
    "url_template": "https://www.npmjs.com/package/{external_id}"
  }
]
```

## Entities

A single entity + its attributes + optional facts + optional
external-system mappings + optional `composes` edge from a parent.

```json
"entities": [
  {
    "name": "salt-design-system",
    "entity_type": "capability",
    "visibility": "private",
    "attributes": {"display_name": "Salt Design System", ...},
    "facts": [{"category": "overview", "title": "Overview", "body": "...", "body_format": "markdown"}],
    "external_ids": [{"system": "npm", "external_id": "@salt-ds/core", "url": "..."}]
  },
  {
    "name": "salt-button",
    "entity_type": "concept",
    "parent": "salt-design-system",
    "attributes": {"interface": {"props": [...]}}
  }
]
```

Rules:

- `name` is unique per tenant; `entity_id` derives from UUIDv5 over
  `(tenant_id, name)`.
- `entity_type` must exist in the vocabulary.
- `visibility` defaults to `"private"`. Declaring a different visibility
  on an already-existing entity triggers an update on that column — the
  one mutation the loader will perform on an existing row.
- `parent`, when present, creates a `composes` edge from parent → this
  entity.
- Each attribute is upserted: if a current live row exists for the key,
  it's left alone.

## Cross-entity facts

Top-level `facts` array — facts attached to entities defined in a
*different* file.

```json
"facts": [
  {
    "entity": "salt-design-system",
    "category": "release_note",
    "title": "v1.45.0 release notes",
    "body": "...",
    "body_format": "markdown",
    "valid_from": "2026-01-15T00:00:00Z"
  }
]
```

Idempotency key: `(entity_id, category, title)` with
`t_invalidated_at IS NULL`. Body / valid_from drift triggers an
in-place update.

## Bitemporal attributes

Multi-row temporal sequences. Used when one attribute key has different
values over time.

```json
"bitemporal_attributes": [
  {
    "entity": "salt-design-system",
    "key": "current_version",
    "replace_existing": true,
    "rows": [
      {"value": "1.43.0", "valid_from": "2025-09-15T00:00:00Z", "valid_to": "2025-12-01T00:00:00Z"},
      {"value": "1.45.0", "valid_from": "2026-01-15T00:00:00Z", "valid_to": null}
    ]
  }
]
```

`replace_existing: true` deletes every live row for `(entity_id, key)`
before inserting.

## Edges

General entity-to-entity edges (`depends_on`, `integrates_with`,
`requires`, `replaced_by`, etc.). The `parent` field on an entity
auto-creates a `composes` edge — use the `edges` section when the
relationship doesn't fit that mould (typically: dependencies between
capabilities, in different bundles).

```json
"edges": [
  {
    "src": "web-runtime",
    "rel": "depends_on",
    "dst": "web-sdk",
    "properties": {
      "reason": "Web apps consume Web Runtime via the SDK's `runtime` module.",
      "version_pin": "^3.0.0"
    }
  }
]
```

Rules:

- `src` and `dst` are entity names (slug-form), resolved to UUIDs via
  the same UUIDv5 rule entities use. Both must exist in the tenant
  before the edge is inserted — the loader fails loudly otherwise so
  typos surface immediately.
- `rel` must be in the `edge_rel` vocabulary (`depends_on`,
  `integrates_with`, `requires`, `replaced_by`, …).
- `properties` is optional JSON, stored as JSONB on the edge.
- `provides_to` is **rejected** — that rel is reserved for the
  `adoptions` section (which mirrors `AdoptionService.adopt`'s
  contract).
- Idempotency: edge IDs are UUIDv5 over `(tenant, src, rel, dst)`,
  so re-runs are no-ops.

## Tenants

Provisioned idempotently — re-running with the same slug is a no-op.
The loader also installs the four default roles (`admin` / `producer` /
`consumer` / `auditor`) and a default rate-limit row.

```json
"tenants": [
  {"slug": "acme-trading", "display_name": "Acme Trading", "kind": "consumer"}
]
```

## Actors

One row per actor per tenant. The loader does not mint API tokens —
tokens come from `make dev-token` / `scripts/mint_token.py`.

```json
"actors": [
  {"tenant_slug": "acme-trading", "display_name": "acme-admin", "roles": ["consumer", "admin"]}
]
```

## Adoptions

Cross-tenant adoption events. `provider_tenant` is optional — when
omitted the adoption resolves against the orchestrator's default
tenant. That makes bundles slug-agnostic.

```json
"adoptions": [
  {
    "provider_capability": "salt-button",
    "consumer_tenant": "acme-trading",
    "consumer_actor": "acme-admin",
    "intent": "Primary CTA button across the trading dashboard.",
    "version_pin": "1.45.0"
  }
]
```

A `provides_to` self-loop edge is created on the provider capability
with the consumer encoded in JSONB properties (matches
`AdoptionService.adopt`).

## Progression definitions

Stage-transition state machines, scoped to one `entity_type` within
one tenant.

```json
"progression_definitions": [
  {
    "entity_type": "capability",
    "is_advisory": false,
    "definition": {
      "states": ["draft", "review", "stable", "deprecated"],
      "transitions": [
        {"from": "draft", "to": "review", "requires_attributes": ["summary", "owner"]},
        {"from": "review", "to": "stable", "requires_attributes": ["interface", "accessibility_compliance"]}
      ]
    }
  }
]
```

Idempotency: `(tenant_id, entity_type)` with `t_invalidated_at IS NULL`.
If the `definition` blob differs, the existing row is invalidated and a
fresh one is inserted.
