# `seeds/salt-ds/` — Salt Design System seed bundle

A multi-version seed of the [Salt Design System](https://www.saltdesignsystem.com/)
(JPMorgan Chase's open-source React + design tokens enterprise library).
Loading this use case populates the dev tenant with **39 real Salt
components across two minor versions**, their TypeScript prop interfaces
extracted from npm tarballs, the package-level interface, 28 design
patterns and 6 foundation guides scraped from the official docs site.

## Loading

```bash
# Default `make dev-seed` includes salt-ds:
make dev-seed

# Or load just salt-ds (plus the prerequisite _vocabulary.json):
make dev-seed-usecase USECASE=salt-ds
```

The three files load in lexical (= chronological) order:

| File | Released | Contents |
|------|----------|----------|
| `v1.43.json` | 2025-09-15 | **38** components from `@salt-ds/core@1.43.0` + DataGrid from `@salt-ds/data-grid@1.0.16`, with full TypeScript prop interfaces (JSDoc descriptions, `@since` tags, `@default` values, `@deprecated` notes). Plus: overview, ADR, security-model, and theming-guide narrative facts; v1.43 release note. |
| `v1.44.json` | 2025-12-01 | **1 added** component (`salt-slider`, with 19 real props) + v1.44 release note. |
| `v1.45.json` | 2026-01-15 | **1 added** component (`salt-stepper`); package-level interface on `salt-design-system` (peer deps, provider props, install command, entry-points list); v1.45 release note; **28 patterns** (Analytical dashboard, Announcement dialog, App header, …) and **6 guides** (Accessibility, Color, Typography, Density, Motion, Getting started) scraped from saltdesignsystem.com as `dev_doc` facts; the bitemporal `current_version` history that powers time-travel queries. |

After loading, the registry answers queries like:

```bash
TOKEN=...
# Full Salt record with components and version
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-design-system?include=components'

# Salt's package-level interface (peer deps, install command, provider props, entry points)
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-design-system' | jq '.attributes.interface'

# A specific component's real prop interface
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-slider' | jq '.attributes.interface'

# Patterns and guides as facts on salt-design-system
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-design-system/artifacts?category=dev_doc'

# Time travel — what was salt-design-system's current_version on 2025-10-15?
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-design-system?as_of=2025-10-15T00:00:00%2B00:00'
# → "1.43.0"
```

## Regenerating from real Salt sources

[`scrape.py`](scrape.py) is the working regenerator. It:

1. Downloads `@salt-ds/core@<version>.tgz` from `registry.npmjs.org` for three
   versions in one run (defaults: 1.43.0, 1.44.0, 1.45.0).
2. Downloads `@salt-ds/data-grid@<paired-version>.tgz` for each — the
   data-grid package versions independently of core, but the npm release
   dates line up release-for-release (core 1.43.0 ↔ data-grid 1.0.16,
   core 1.44.0 ↔ data-grid 1.0.17, core 1.45.0 ↔ data-grid 1.0.19).
3. Parses `dist-types/**/*.d.ts` for every `export interface XProps {…}`
   block. Extracts prop name, type, required (from `?`), `@default`,
   `@since`, `@deprecated`, and description from JSDoc.
4. Fetches `saltdesignsystem.com/salt/patterns`,
   `saltdesignsystem.com/salt/foundations/{accessibility,color,…}`,
   parses the embedded `__NEXT_DATA__` JSON (the site is Next.js SSR'd),
   and emits each pattern/guide as a fact on `salt-design-system`.
5. Computes the version diff (new components added at v1.44 vs v1.43,
   etc.) and writes the three JSON bundles.

The scraper is **not** part of `make dev-seed`. CI never runs it. The
committed JSONs are the source of truth.

```bash
# Refresh from current npm + docs site:
python seeds/salt-ds/scrape.py

# Use cached tarballs in /tmp/salt-scrape (faster, no network):
python seeds/salt-ds/scrape.py --skip-fetch

# Skip the docs-site scrape (offline-friendly):
python seeds/salt-ds/scrape.py --skip-docs

# Target different Salt versions:
python seeds/salt-ds/scrape.py --versions 1.50.0 1.51.0 1.52.0
```

Review the diff before committing — JSON changes ship to the dev
tenant unchanged.

## How the scraper handles things you might run into

| Situation | Behavior |
|---|---|
| Salt directory contains hooks but no component (`overlay/`, `breakpoints/`) | Skipped via `_SKIP_DIRS` allow-list. |
| Component has multiple `XProps` interfaces in its tree | Prefers the one whose name matches the directory's PascalCase form; falls back to the one with the most props. |
| Generic type parameters on the interface (`GridProps<T = any>`) | Handled — the regex skips the `<…>` clause between `Props` and `{`. |
| `@salt-ds/lab` component (pre-stable) | Not scraped. Lab versions are alpha-tagged on npm; add to the scraper when those components stabilise. |
| Docs site changes its `__NEXT_DATA__` shape | The walker falls back gracefully and you get fewer patterns / guides — the scraper still completes. |

## Schema reference

The JSON shape is documented at [`../_schema.md`](../_schema.md). The
loader lives at [`../../scripts/seed_loader.py`](../../scripts/seed_loader.py).
