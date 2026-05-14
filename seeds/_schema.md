# Seed-file format (schema_version = 1)

A seed file is a JSON document describing data to load into a tenant. The
loader at [`scripts/seed_loader.py`](../scripts/seed_loader.py) reads one or
more files in order and applies them idempotently — re-running yields the
same `entity_id`s, same edges, same attributes (UUIDv5 over `(tenant_id,
name)` ensures stability).

## Top-level shape

```json
{
  "schema_version": 1,
  "name": "salt-ds-v1.45",
  "description": "Free-form blurb shown in seeder output.",
  "released_at": "2026-01-15T00:00:00Z",

  "vocabulary":          [ /* see Vocabulary */ ],
  "external_systems":    [ /* see External systems */ ],
  "entities":            [ /* see Entities */ ],
  "facts":               [ /* see Cross-entity facts */ ],
  "bitemporal_attributes": [ /* see Bitemporal attributes */ ]
}
```

All sections are optional. A file may contain just `vocabulary`, or just
`entities`, etc. Loading order across files matters; within a file the
loader processes sections in the order listed above.

`released_at` is optional and only used as the default `valid_from` for
rows inside this file that don't specify one. Files that don't represent a
versioned release (e.g. `_vocabulary.json`) omit it; the loader falls back
to `now()`.

## Vocabulary

Closed-vocabulary values (entity types, edge relationship types, lifecycle
states, etc.) inserted with `ON CONFLICT (tenant_id, kind, value) DO
NOTHING`.

```json
"vocabulary": [
  {"kind": "entity_type", "value": "capability"},
  {"kind": "edge_rel",    "value": "composes"}
]
```

## External systems

`external_systems` rows (npm registry, GitHub, etc.) inserted with `ON
CONFLICT (tenant_id, slug) DO NOTHING`.

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

A single entity row + its attributes + optional facts + optional
external-system mappings + optional `composes` edge from a parent entity.

```json
"entities": [
  {
    "name": "salt-design-system",
    "entity_type": "capability",
    "visibility": "private",
    "valid_from": "2025-09-15T00:00:00Z",
    "attributes": {
      "display_name": "Salt Design System",
      "summary": "...",
      "lifecycle": {"state": "ga"}
    },
    "facts": [
      {
        "category": "overview",
        "title": "Overview",
        "body": "Salt is...",
        "body_format": "markdown"
      }
    ],
    "external_ids": [
      {
        "system": "npm",
        "external_id": "@salt-ds/core",
        "url": "https://www.npmjs.com/package/@salt-ds/core"
      }
    ]
  },
  {
    "name": "salt-button",
    "entity_type": "concept",
    "parent": "salt-design-system",
    "valid_from": "2025-09-15T00:00:00Z",
    "attributes": {
      "display_name": "Button",
      "category": "form-controls",
      "summary": "Standard, accent, and CTA buttons.",
      "interface": {
        "package": "@salt-ds/core",
        "import_name": "Button",
        "props": [
          {"name": "variant", "type": "'cta' | 'primary' | 'secondary'", "required": false, "default": "'primary'"},
          {"name": "disabled", "type": "boolean", "required": false, "default": "false"},
          {"name": "onClick", "type": "(event: MouseEvent) => void", "required": false}
        ]
      }
    }
  }
]
```

Rules:

- `name` is unique per tenant; `entity_id` derives from UUIDv5 over
  `(tenant_id, name)` — stable across runs.
- `entity_type` must exist in the vocabulary (`entity_type` kind).
- `visibility` defaults to `"private"`.
- `valid_from` defaults to the file's `released_at` or to `now()`.
- `parent`, when present, creates a `composes` edge from the named parent
  entity to this entity. Parent must be defined somewhere in the load set
  (same file or an earlier-loaded file).
- `attributes` is a flat map; values may be primitives or JSON objects. An
  `interface` attribute (component prop catalog) is a normal attribute —
  stored as JSONB. The loader does not interpret its shape.
- Each attribute is upserted: if a current live row exists for the key,
  it's left alone; otherwise a new row is inserted.
- `facts` and `external_ids` are processed scoped to this entity.

## Cross-entity facts

Top-level `facts` array — facts attached to entities defined in a
*different* file. Use sparingly; prefer keeping facts inside their entity
in the same file.

```json
"facts": [
  {
    "entity": "salt-design-system",
    "category": "release_note",
    "title": "v1.45.0 release notes",
    "body": "DataGrid gains column pinning ...",
    "body_format": "markdown",
    "valid_from": "2026-01-15T00:00:00Z"
  }
]
```

Idempotency: `(entity_id, category, title)` with `t_invalidated_at IS
NULL` is the dedup key. If a row matches by category+title but body or
`valid_from` differs, the existing row is updated in place (no new
bitemporal version is appended — that's a seed-script choice, not a
service-layer rule).

## Bitemporal attributes

Multi-row temporal sequences. Used when one attribute key has different
values over time — e.g. `current_version` of Salt: `"1.43.0"` valid
2025-09-15 → 2025-12-01, then `"1.44.0"` valid 2025-12-01 → 2026-01-15,
then `"1.45.0"` valid 2026-01-15 → ∞.

```json
"bitemporal_attributes": [
  {
    "entity": "salt-design-system",
    "key": "current_version",
    "replace_existing": true,
    "rows": [
      {"value": "1.43.0", "valid_from": "2025-09-15T00:00:00Z", "valid_to": "2025-12-01T00:00:00Z"},
      {"value": "1.44.0", "valid_from": "2025-12-01T00:00:00Z", "valid_to": "2026-01-15T00:00:00Z"},
      {"value": "1.45.0", "valid_from": "2026-01-15T00:00:00Z", "valid_to": null}
    ]
  }
]
```

`replace_existing: true` causes the loader to first delete every live row
for `(entity_id, key)` (where live = `t_invalidated_at IS NULL`) and then
insert the listed rows fresh. This is the only place the loader will
delete existing data. Set to `false` (or omit) to skip insertion when any
live row exists for the key.

## Loading order

When multiple files load in one run, the loader applies them in the order
passed. Cross-file references (e.g. an entity in `salt-ds/v1.44.json`
naming a `parent` defined in `demo-minimal/v1.json`) require the
referenced file to load first.

## Reserved fields

Future versions may add `edges` (arbitrary src/rel/dst), `subscriptions`,
or `actors`. None of those are in v1; the current loader will fail with
an explicit error if it sees an unknown top-level key.
