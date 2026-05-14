# `seeds/salt-ds/` — Salt Design System seed bundle

A multi-version seed of the [Salt Design System](https://www.saltdesignsystem.com/)
(JPMorgan Chase's open-source React + design tokens enterprise library).
Loading this use case populates the dev tenant with all 19 components
across two minor versions, their prop interfaces, the package-level
interface, and narrative facts (ADR, security model, theming guide).

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
| `v1.43.json` | 2025-09-15 | 17 baseline components with full TypeScript prop interfaces; overview, ADR, security-model, theming-guide facts; v1.43 release note. |
| `v1.44.json` | 2025-12-01 | 2 added components (`salt-filter-bar`, `salt-stepper-input`) with interfaces; v1.44 release note. |
| `v1.45.json` | 2026-01-15 | Package-level interface on `salt-design-system` (peer deps, provider props, entry points); v1.45 release note; the bitemporal `current_version` history that powers time-travel queries. |

After loading, the registry answers queries like:

```bash
TOKEN=...
# Full Salt record with components and version
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-design-system?include=components'

# Salt's package-level interface (peer deps, install command, provider props)
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-design-system' | jq '.attributes.interface'

# A specific component's prop interface
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-button' | jq '.attributes.interface'

# Time travel — what was salt-design-system's current_version on 2025-10-15?
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/v1/capabilities/salt-design-system?as_of=2025-10-15T00:00:00%2B00:00'
# → "1.43.0"
```

## Regenerating from real Salt sources

[`scrape.py`](scrape.py) documents the methodology and source list (npm
tarballs for prop interfaces, saltdesignsystem.com for categories /
summaries / patterns / guides). The current JSONs were authored against
those sources and are committed; the scraper is a tool for refreshing
them when Salt ships a new version.

The scraper is **not** part of `make dev-seed`. CI never runs it. The
committed JSONs are the source of truth.

When you do re-run it:

```bash
python seeds/salt-ds/scrape.py --version 1.46.0 --previous-version 1.45.0
# review the diff
# commit
```

## Known gaps

- **Patterns and guides from the docs site are not yet scraped.** The
  three narrative facts on `salt-design-system` (ADR, security-model,
  theming-guide) cover the "guides" axis at a high level; richer
  pattern-by-pattern facts await a network-enabled scraper run.
- **Component summaries are hand-authored.** The npm tarball gives us
  exact prop types but no human-readable summaries. When the scraper is
  fleshed out, summaries should come from the docs site (or fall back to
  the component's GitHub README) rather than the current short blurbs.

## Schema reference

The JSON shape is documented at [`../_schema.md`](../_schema.md). The
loader lives at [`../../scripts/seed_loader.py`](../../scripts/seed_loader.py).
