#!/usr/bin/env python3
"""Regenerate Salt seed JSONs from public Salt sources.

Not part of ``make dev-seed`` — CI never runs it. The committed JSONs in
this directory are the source of truth that the loader reads. Re-run
when Salt ships a new version and you want to refresh the demo.

Sources:

- ``https://registry.npmjs.org/@salt-ds/core/-/core-<ver>.tgz`` — the
  authoritative shape for component prop interfaces. Walks the
  ``dist-types/`` tree and parses ``export interface XProps { ... }``
  blocks via regex; the declarations are flat enough that a full TS
  parser isn't needed.
- ``https://www.saltdesignsystem.com/salt/{patterns,guides}/`` — Next.js
  pages with ``__NEXT_DATA__`` JSON. Patterns and guides are extracted
  from that structured blob, not HTML scraped.

Each scraped Salt version (1.43, 1.44, 1.45) writes one JSON file. The
"version diff" semantics — components added at v1.44, edits at v1.45 —
come from comparing the component lists across versions.

Usage::

    python seeds/salt-ds/scrape.py
    python seeds/salt-ds/scrape.py --versions 1.43.0 1.44.0 1.45.0
    python seeds/salt-ds/scrape.py --skip-fetch    # use cached /tmp/salt-scrape
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_CACHE = Path("/tmp/salt-scrape")
_NPM_TARBALL = "https://registry.npmjs.org/@salt-ds/core/-/core-{version}.tgz"
_DATAGRID_TARBALL = "https://registry.npmjs.org/@salt-ds/data-grid/-/data-grid-{version}.tgz"
_DOCS_BASE = "https://www.saltdesignsystem.com"

# @salt-ds/data-grid is versioned independently of @salt-ds/core. These
# pairings line up by npm release date (core 1.43.0 and data-grid 1.0.16
# both shipped 2025-04-04, etc.).
_DATAGRID_VERSION_MAP: dict[str, str] = {
    "1.43.0": "1.0.16",
    "1.44.0": "1.0.17",
    "1.45.0": "1.0.19",
}

# Component-name slug → registry-friendly seed name. Salt's directory
# names are kebab-case under dist-types/; we prefix with `salt-` to
# match the seed naming convention. A handful of directories don't
# represent user-facing components (utils, types, theme, semantic-icon-
# provider, salt-provider, viewport, breakpoints, form-field-context,
# list-control, useButton-style hooks) and get skipped.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "utils",
        "types",
        "theme",
        "salt-provider",
        "semantic-icon-provider",
        "viewport",
        "breakpoints",
        "aria-announcer",
        "form-field-context",
        "list-control",
    }
)

# Category buckets — assigned by keyword heuristic. Order matters: the
# first matching keyword wins. Components that match nothing fall into
# "uncategorised" and need a hand-edit.
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "form-controls",
        (
            "button",
            "input",
            "checkbox",
            "radio",
            "switch",
            "slider",
            "stepper",
            "combo",
            "dropdown",
            "option",
            "form-field",
            "multiline",
            "toggle",
            "segmented",
        ),
    ),
    (
        "layout",
        (
            "layout",
            "stack",
            "flex",
            "grid",
            "border",
            "split",
            "flow",
            "parent-child",
            "panel",
            "card",
            "interactable-card",
            "link-card",
            "divider",
            "accordion",
            "tabs",
        ),
    ),
    (
        "overlays",
        ("dialog", "drawer", "tooltip", "toast", "overlay", "scrim", "menu", "banner"),
    ),
    (
        "navigation",
        ("navigation", "pagination", "skip-link", "breadcrumbs", "link"),
    ),
    (
        "data",
        ("data-grid", "list-box", "list", "tag", "pill", "badge", "filter-bar", "filter"),
    ),
    (
        "feedback",
        ("spinner", "progress", "status-adornment", "status-indicator"),
    ),
    (
        "media",
        ("avatar", "icon", "text"),
    ),
    (
        "input-helpers",
        ("file-drop-zone", "adornment"),
    ),
)


def _category_for(slug: str) -> str:
    for category, keywords in _CATEGORY_RULES:
        for kw in keywords:
            if kw in slug:
                return category
    return "uncategorised"


# ---------------------------------------------------------------------------
# Tarball fetch + extract
# ---------------------------------------------------------------------------


def fetch_tarball(version: str, *, force: bool = False, package: str = "core") -> Path:
    """Download and extract a Salt npm package. Returns extract dir.

    package="core" → @salt-ds/core; package="data-grid" → @salt-ds/data-grid.
    """
    _CACHE.mkdir(parents=True, exist_ok=True)
    tgz = _CACHE / f"{package}-{version}.tgz"
    extract = _CACHE / f"{package}-{version}"
    if extract.exists() and not force:
        return extract
    if not tgz.exists() or force:
        template = _NPM_TARBALL if package == "core" else _DATAGRID_TARBALL
        url = template.format(version=version)
        print(f"  fetching {url}", file=sys.stderr)
        urllib.request.urlretrieve(url, tgz)
    if extract.exists():
        # tarfile won't overwrite; clear it first.
        import shutil

        shutil.rmtree(extract)
    extract.mkdir(parents=True)
    with tarfile.open(tgz, "r:gz") as tf:
        # The tarball has a top-level "package/" directory; strip it.
        for member in tf.getmembers():
            if member.name.startswith("package/"):
                member.name = member.name[len("package/") :]
            tf.extract(member, extract)
    return extract


# ---------------------------------------------------------------------------
# .d.ts parsing
# ---------------------------------------------------------------------------


_INTERFACE_RE = re.compile(
    # Allow an optional generic-parameter clause like `<T = any>` between
    # `Props` and the optional `extends`/`{`. Without this, generics like
    # GridProps<T = any> silently fail to match.
    r"export\s+interface\s+(?P<name>\w+)Props\s*"
    r"(?:<[^{]+?>)?\s*"
    r"(?:extends\s+[^{]+)?\{(?P<body>.+?)\n\}",
    re.DOTALL,
)
_TYPE_ALIAS_RE = re.compile(
    r"export\s+(?:declare\s+)?type\s+(?P<name>\w+)Props\s*=\s*(?P<body>[^;]+);",
    re.DOTALL,
)
# Match a single prop. Captures preceding JSDoc, name, optional `?`, type
# (everything up to the trailing semicolon). Type can span lines.
_PROP_RE = re.compile(
    r"(?:/\*\*(?P<doc>.+?)\*/\s*)?(?P<name>[\w\-\"]+)(?P<optional>\??)\s*:\s*(?P<type>(?:[^;{}]|\{[^{}]*\})+);",
    re.DOTALL,
)
_DEFAULT_RE = re.compile(r"@default\s+(.+?)(?:\n|\*/)", re.DOTALL)
_SINCE_RE = re.compile(r"@since\s+([\d.]+)", re.DOTALL)
_DEPRECATED_RE = re.compile(r"@deprecated\s+(.+?)(?:\n\s*\*\s*\n|\*/)", re.DOTALL)


def _clean_jsdoc(doc: str | None) -> str:
    if not doc:
        return ""
    # Strip leading `*` per line and excess whitespace.
    lines = []
    for raw in doc.splitlines():
        cleaned = raw.strip().lstrip("*").strip()
        # Drop tag lines from description (we extract them separately).
        if cleaned.startswith("@"):
            continue
        if cleaned:
            lines.append(cleaned)
    return " ".join(lines).strip()


def _extract_props(body: str) -> list[dict[str, Any]]:
    """Pull prop entries out of an interface body."""
    props: list[dict[str, Any]] = []
    pos = 0
    while pos < len(body):
        # Greedy approach: find next JSDoc block (optional) followed by prop.
        m = _PROP_RE.search(body, pos)
        if m is None:
            break
        pos = m.end()
        name = m.group("name").strip().strip('"').strip("'")
        # Skip obviously non-prop matches (method signatures, computed types).
        if not re.match(r"^[\w\-]+$", name):
            continue
        # Skip nested interface members that aren't actually props (e.g.
        # generic constraints) — these usually have curly braces in the
        # type string but no `:`.
        prop_type = m.group("type").strip().rstrip(",").strip()
        # Collapse internal whitespace in type unions for readability.
        prop_type = re.sub(r"\s+", " ", prop_type)
        if len(prop_type) > 200:
            prop_type = prop_type[:197] + "..."

        doc = m.group("doc") or ""
        description = _clean_jsdoc(doc)

        entry: dict[str, Any] = {
            "name": name,
            "type": prop_type,
            "required": m.group("optional") != "?",
        }
        default_match = _DEFAULT_RE.search(doc) if doc else None
        if default_match:
            entry["default"] = default_match.group(1).strip().rstrip(".").strip()
        since_match = _SINCE_RE.search(doc) if doc else None
        if since_match:
            entry["since"] = since_match.group(1).strip().rstrip(".").strip()
        deprecated_match = _DEPRECATED_RE.search(doc) if doc else None
        if deprecated_match:
            entry["deprecated"] = _clean_jsdoc(deprecated_match.group(1))
        if description and "deprecated" not in entry:
            # Description gets dropped when the prop is deprecated; the
            # deprecation message is more useful in that slot.
            entry["description"] = description

        props.append(entry)
    return props


def _find_component_interface(component_dir: Path) -> tuple[str, list[dict[str, Any]]] | None:
    """Locate the canonical Props interface for a component directory.

    Strategy: walk all .d.ts files under the directory, collect every
    ``export interface XProps`` block. Pick the one whose name matches
    the directory's PascalCase form (e.g. ``button/`` → ``ButtonProps``)
    when available, else take the first one we find.

    Returns ``(import_name, props)`` or None if no interface exists
    (some directories — like overlay/ — define only hooks).
    """
    pascal = "".join(part.capitalize() for part in component_dir.name.split("-"))

    candidates: list[tuple[str, list[dict[str, Any]]]] = []
    for dts in sorted(component_dir.rglob("*.d.ts")):
        text = dts.read_text(encoding="utf-8")
        for m in _INTERFACE_RE.finditer(text):
            name = m.group("name")
            body = m.group("body")
            props = _extract_props(body)
            candidates.append((name, props))

    if not candidates:
        return None

    # Prefer the interface whose name matches the directory.
    for name, props in candidates:
        if name == pascal:
            return name, props

    # Else, prefer one matching <Pascal>* (e.g. ComboBoxProps for combo-box-base/).
    for name, props in candidates:
        if name.startswith(pascal):
            return name, props

    # Fall back to whichever has the most props (most likely the main component).
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    return candidates[0]


def parse_components(extract_dir: Path) -> dict[str, dict[str, Any]]:
    """Return ``{slug: {display_name, props, since}}`` for every component dir.

    Slug = directory name (kebab-case). Display name = PascalCase form
    of the interface name.
    """
    dist_types = extract_dir / "dist-types"
    if not dist_types.is_dir():
        raise FileNotFoundError(f"no dist-types/ in {extract_dir}")

    components: dict[str, dict[str, Any]] = {}
    for child in sorted(dist_types.iterdir()):
        if not child.is_dir():
            continue
        if child.name in _SKIP_DIRS:
            continue
        if child.name.startswith("."):
            continue

        result = _find_component_interface(child)
        if result is None:
            continue
        import_name, props = result
        if not props:
            # Empty interface — usually means the component takes no
            # additional props beyond the wrapped HTML element.
            pass
        components[child.name] = {
            "import_name": import_name,
            "props": props,
        }
    return components


def parse_datagrid(extract_dir: Path) -> dict[str, Any] | None:
    """Parse @salt-ds/data-grid's flat structure: Grid.d.ts at dist-types root.

    Returns a single component info dict (same shape as values in
    ``parse_components``) or None if the file is missing.
    """
    grid_path = extract_dir / "dist-types" / "Grid.d.ts"
    if not grid_path.is_file():
        return None
    text = grid_path.read_text(encoding="utf-8")
    m = _INTERFACE_RE.search(text)
    if m is None:
        return None
    props = _extract_props(m.group("body"))
    return {
        "import_name": "Grid",
        "props": props,
    }


# ---------------------------------------------------------------------------
# Docs site scraping
# ---------------------------------------------------------------------------


def _fetch_next_data(path: str) -> dict[str, Any]:
    url = f"{_DOCS_BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "salt-seed-scrape/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8")
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL
    )
    if m is None:
        raise RuntimeError(f"__NEXT_DATA__ not found at {url}")
    return json.loads(m.group(1))


def fetch_patterns() -> list[dict[str, str]]:
    data = _fetch_next_data("/salt/patterns")
    # Walk to find the patterns list. The Next.js page-data lives under
    # props.pageProps.* and the structure has shifted over time; defend
    # against shape changes by walking.
    def walk(node: Any) -> list[Any] | None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "pattern" and isinstance(v, list):
                    return v
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    raw = walk(data) or []
    out: list[dict[str, str]] = []
    for entry in raw:
        title = entry.get("title")
        description = entry.get("description")
        tags = entry.get("tags") or []
        if not title:
            continue
        body = description or ""
        if tags:
            body += f"\n\n**Tags:** {', '.join(tags)}"
        out.append(
            {
                "category": "dev_doc",
                "title": f"Pattern: {title}",
                "body_format": "markdown",
                "body": body.strip(),
            }
        )
    return out


def fetch_guides() -> list[dict[str, str]]:
    """Guides live under /salt/foundations/* and /salt/getting-started/*.

    The site doesn't expose a single "guides" index, so we sample the
    foundations pages — accessibility, motion, ai-design — that the
    user-facing nav surfaces under "Guides".
    """
    guide_paths = [
        ("/salt/foundations/accessibility", "Accessibility"),
        ("/salt/foundations/color", "Color"),
        ("/salt/foundations/typography", "Typography"),
        ("/salt/foundations/density", "Density"),
        ("/salt/foundations/motion", "Motion"),
        ("/salt/getting-started/start-here", "Getting started"),
    ]
    out: list[dict[str, str]] = []
    for path, label in guide_paths:
        try:
            data = _fetch_next_data(path)
        except Exception as e:
            print(f"  skip {path}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        # Best-effort: pluck the page title and a short summary.
        summary = ""
        if isinstance(data, dict):
            pp = data.get("props", {}).get("pageProps", {})
            for cand_key in ("description", "summary", "subtitle", "intro"):
                if pp.get(cand_key):
                    summary = str(pp[cand_key])
                    break
        if not summary:
            summary = f"Salt {label.lower()} guide — see {_DOCS_BASE}{path} for full content."
        out.append(
            {
                "category": "dev_doc",
                "title": f"Guide: {label}",
                "body_format": "markdown",
                "body": f"{summary}\n\nSource: {_DOCS_BASE}{path}",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Bundle generation
# ---------------------------------------------------------------------------


# Hand-authored facts that don't come from Salt sources — these encode
# narrative claims about Salt we want in the demo (ADR, security model,
# theming guide). Each carries a category and version-applicability.
_NARRATIVE_FACTS: list[dict[str, Any]] = [
    {
        "category": "overview",
        "title": "Overview",
        "body_format": "markdown",
        "applies_from": "1.43.0",
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
        "category": "adr",
        "title": "Architectural decision: React, not Web Components",
        "body_format": "markdown",
        "applies_from": "1.43.0",
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
        "applies_from": "1.43.0",
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
        "applies_from": "1.43.0",
        "body": (
            "Wrap your app in `<SaltProvider density={...} mode={'light'|'dark'}>`. "
            "All components inherit from the provider; nested providers override. "
            "Custom themes: extend the default token set via a CSS variable layer "
            "(see `@salt-ds/theme`). Density: `low` / `medium` / `high` / `touch` "
            "(touch added in v1.45)."
        ),
    },
]

# Hand-authored release notes — npm doesn't ship machine-readable
# changelogs, and Salt's GitHub release notes are markdown-only. These
# are kept short and reflect the version's user-visible changes.
_RELEASE_NOTES: dict[str, str] = {
    "1.43.0": (
        "v1.43.0 — Initial 1.x stable line documented in this demo. Components "
        "ship from @salt-ds/core with WCAG 2.1 AA conformance. Tree-shakeable "
        "exports; theming via <SaltProvider density={...} mode={'light'|'dark'}>."
    ),
    "1.44.0": (
        "v1.44.0 — Incremental polish across form controls and overlays. See the "
        "diff against v1.43 for the new components surfaced under "
        "/v1/capabilities/salt-design-system?as_of=<v1.44-release>."
    ),
    "1.45.0": (
        "v1.45.0 — DataGrid gains column pinning + virtualised row rendering for "
        "≥ 100k-row datasets; Dialog focus-trap fix for nested Drawer composition; "
        "new density token `density-touch` for tablet form-factor; ComboBox now "
        "exposes a controlled `open` prop. No breaking changes vs 1.44.x."
    ),
}

# Release dates pinned in the seed — these are what the bitemporal
# current_version history uses for `?as_of=...` time-travel demos. They
# don't have to match the real npm release dates; they just need to be
# monotonic.
_RELEASE_DATES: dict[str, str] = {
    "1.43.0": "2025-09-15T00:00:00Z",
    "1.44.0": "2025-12-01T00:00:00Z",
    "1.45.0": "2026-01-15T00:00:00Z",
}


def _build_entity(
    slug: str,
    info: dict[str, Any],
    *,
    version: str,
    valid_from: str,
    is_capability_root: bool = False,
) -> dict[str, Any]:
    if is_capability_root:
        return {
            "name": "salt-design-system",
            "entity_type": "capability",
            "valid_from": valid_from,
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
                "package_name": "@salt-ds/core",
                "framework": "react",
                "license": "Apache-2.0",
                "accessibility_compliance": "WCAG 2.1 AA",
            },
            "external_ids": [
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
            ],
        }

    return {
        "name": f"salt-{slug}",
        "entity_type": "concept",
        "parent": "salt-design-system",
        "valid_from": valid_from,
        "attributes": {
            "display_name": info["import_name"],
            "category": _category_for(slug),
            "summary": info.get("summary")
            or f"Salt {info['import_name']} component (@salt-ds/core, v{version}).",
            "interface": {
                "package": info.get("package", "@salt-ds/core"),
                "import_name": info["import_name"],
                "props": info["props"],
            },
        },
    }


def build_v143(comps_by_slug: dict[str, dict[str, Any]]) -> dict[str, Any]:
    valid_from = _RELEASE_DATES["1.43.0"]
    entities: list[dict[str, Any]] = []
    # Capability root with package metadata.
    entities.append(
        _build_entity(
            "", {}, version="1.43.0", valid_from=valid_from, is_capability_root=True
        )
    )
    for slug, info in sorted(comps_by_slug.items()):
        entities.append(_build_entity(slug, info, version="1.43.0", valid_from=valid_from))

    facts: list[dict[str, Any]] = []
    for nf in _NARRATIVE_FACTS:
        if nf["applies_from"] == "1.43.0":
            facts.append(
                {
                    "entity": "salt-design-system",
                    "category": nf["category"],
                    "title": nf["title"],
                    "body_format": nf["body_format"],
                    "body": nf["body"],
                }
            )
    facts.append(
        {
            "entity": "salt-design-system",
            "category": "release_note",
            "title": "v1.43.0 release notes",
            "body_format": "markdown",
            "body": _RELEASE_NOTES["1.43.0"],
        }
    )

    return {
        "schema_version": 1,
        "name": "salt-ds-v1.43",
        "description": (
            "Salt Design System v1.43.0 — initial baseline. "
            f"{len(comps_by_slug)} components from @salt-ds/core@1.43.0, real "
            "TypeScript prop interfaces, narrative facts (ADR, security model, "
            "theming guide), v1.43 release note."
        ),
        "released_at": valid_from,
        "entities": entities,
        "facts": facts,
    }


def build_v144(
    v143_slugs: set[str], v144_comps: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    valid_from = _RELEASE_DATES["1.44.0"]
    new_slugs = sorted(set(v144_comps) - v143_slugs)
    entities = [
        _build_entity(slug, v144_comps[slug], version="1.44.0", valid_from=valid_from)
        for slug in new_slugs
    ]
    facts = [
        {
            "entity": "salt-design-system",
            "category": "release_note",
            "title": "v1.44.0 release notes",
            "body_format": "markdown",
            "body": _RELEASE_NOTES["1.44.0"],
        }
    ]
    return {
        "schema_version": 1,
        "name": "salt-ds-v1.44",
        "description": (
            f"Salt Design System v1.44.0 — adds {len(new_slugs)} component(s) over v1.43: "
            f"{', '.join(f'salt-{s}' for s in new_slugs) if new_slugs else '(no new components in this release)'}."
        ),
        "released_at": valid_from,
        "entities": entities,
        "facts": facts,
    }


def build_v145(
    v144_slugs: set[str],
    v145_comps: dict[str, dict[str, Any]],
    patterns: list[dict[str, str]],
    guides: list[dict[str, str]],
) -> dict[str, Any]:
    valid_from = _RELEASE_DATES["1.45.0"]
    new_slugs = sorted(set(v145_comps) - v144_slugs)
    new_entities = [
        _build_entity(slug, v145_comps[slug], version="1.45.0", valid_from=valid_from)
        for slug in new_slugs
    ]

    # Capability-level package interface (entry-points list + provider props).
    entry_points = sorted(info["import_name"] for info in v145_comps.values())
    package_interface_entity = {
        "name": "salt-design-system",
        "entity_type": "capability",
        "valid_from": valid_from,
        "attributes": {
            "interface": {
                "package": "@salt-ds/core",
                "current_version": "1.45.0",
                "install": "npm install @salt-ds/core @salt-ds/theme",
                "peer_dependencies": {
                    "react": ">=18.0.0",
                    "react-dom": ">=18.0.0",
                },
                "framework": "react",
                "language": "typescript",
                "module_format": ["esm", "cjs"],
                "tree_shakeable": True,
                "side_effects": False,
                "side_packages": [
                    {
                        "name": "@salt-ds/theme",
                        "summary": "Design tokens (colour, density, corner-radius, typography) as CSS variables.",
                    },
                    {
                        "name": "@salt-ds/icons",
                        "summary": "Salt icon set — tree-shakeable React components.",
                    },
                    {
                        "name": "@salt-ds/data-grid",
                        "summary": "Virtualised data grid, versioned independently of @salt-ds/core.",
                    },
                    {
                        "name": "@salt-ds/lab",
                        "summary": "Pre-stable components incubating before promotion to @salt-ds/core.",
                    },
                ],
                "provider": {
                    "name": "SaltProvider",
                    "summary": "Wrap your app to apply tokens and density. Nested providers override.",
                    "props": [
                        {
                            "name": "density",
                            "type": "'low' | 'medium' | 'high' | 'touch'",
                            "required": False,
                            "default": "'medium'",
                        },
                        {
                            "name": "mode",
                            "type": "'light' | 'dark'",
                            "required": False,
                            "default": "'light'",
                        },
                        {
                            "name": "theme",
                            "type": "string | string[]",
                            "required": False,
                            "description": "Theme name(s) loaded from @salt-ds/theme.",
                        },
                    ],
                },
                "entry_points": entry_points,
            }
        },
    }

    entities = [package_interface_entity, *new_entities]

    facts: list[dict[str, Any]] = []
    facts.append(
        {
            "entity": "salt-design-system",
            "category": "release_note",
            "title": "v1.45.0 release notes",
            "body_format": "markdown",
            "body": _RELEASE_NOTES["1.45.0"],
        }
    )
    # Patterns + guides as cross-entity facts on salt-design-system.
    for p in patterns:
        facts.append({"entity": "salt-design-system", **p})
    for g in guides:
        facts.append({"entity": "salt-design-system", **g})

    return {
        "schema_version": 1,
        "name": "salt-ds-v1.45",
        "description": (
            f"Salt Design System v1.45.0 — adds {len(new_slugs)} component(s) over v1.44, "
            f"plus the package-level interface (entry points, provider props, install), "
            f"{len(patterns)} scraped patterns, {len(guides)} scraped guides, "
            "and the current_version bitemporal history for time-travel demos."
        ),
        "released_at": valid_from,
        "entities": entities,
        "facts": facts,
        "bitemporal_attributes": [
            {
                "entity": "salt-design-system",
                "key": "current_version",
                "replace_existing": True,
                "rows": [
                    {
                        "value": "1.43.0",
                        "valid_from": _RELEASE_DATES["1.43.0"],
                        "valid_to": _RELEASE_DATES["1.44.0"],
                    },
                    {
                        "value": "1.44.0",
                        "valid_from": _RELEASE_DATES["1.44.0"],
                        "valid_to": _RELEASE_DATES["1.45.0"],
                    },
                    {
                        "value": "1.45.0",
                        "valid_from": _RELEASE_DATES["1.45.0"],
                        "valid_to": None,
                    },
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--versions",
        nargs=3,
        default=["1.43.0", "1.44.0", "1.45.0"],
        metavar=("V143", "V144", "V145"),
        help="Three @salt-ds/core versions to scrape (oldest first).",
    )
    p.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Use cached tarballs in /tmp/salt-scrape instead of re-downloading.",
    )
    p.add_argument(
        "--skip-docs",
        action="store_true",
        help="Skip the saltdesignsystem.com patterns + guides scrape.",
    )
    args = p.parse_args(argv)

    v143_str, v144_str, v145_str = args.versions

    print(f"Fetching @salt-ds/core @ {v143_str}, {v144_str}, {v145_str}…", file=sys.stderr)
    v143_dir = fetch_tarball(v143_str, force=not args.skip_fetch)
    v144_dir = fetch_tarball(v144_str, force=not args.skip_fetch)
    v145_dir = fetch_tarball(v145_str, force=not args.skip_fetch)

    print("Parsing component .d.ts files…", file=sys.stderr)
    v143_comps = parse_components(v143_dir)
    v144_comps = parse_components(v144_dir)
    v145_comps = parse_components(v145_dir)

    # @salt-ds/data-grid is a separate package on its own version line.
    # Splice it into each core-version's component set so the seed
    # captures it under salt-data-grid (matching what consumers see when
    # they install the data-grid package alongside core).
    for core_version, comps in (
        (v143_str, v143_comps),
        (v144_str, v144_comps),
        (v145_str, v145_comps),
    ):
        dg_version = _DATAGRID_VERSION_MAP.get(core_version)
        if dg_version is None:
            print(
                f"  no data-grid mapping for core@{core_version} — skipping",
                file=sys.stderr,
            )
            continue
        dg_dir = fetch_tarball(dg_version, force=not args.skip_fetch, package="data-grid")
        dg = parse_datagrid(dg_dir)
        if dg is None:
            print(
                f"  data-grid@{dg_version}: Grid.d.ts not found — skipping",
                file=sys.stderr,
            )
            continue
        dg["package"] = "@salt-ds/data-grid"
        comps["data-grid"] = dg

    print(
        f"  v1.43: {len(v143_comps)} components | "
        f"v1.44: {len(v144_comps)} components | "
        f"v1.45: {len(v145_comps)} components",
        file=sys.stderr,
    )

    if args.skip_docs:
        patterns: list[dict[str, str]] = []
        guides: list[dict[str, str]] = []
    else:
        print("Scraping saltdesignsystem.com patterns…", file=sys.stderr)
        patterns = fetch_patterns()
        print(f"  patterns: {len(patterns)}", file=sys.stderr)
        print("Scraping saltdesignsystem.com guides…", file=sys.stderr)
        guides = fetch_guides()
        print(f"  guides: {len(guides)}", file=sys.stderr)

    print("Building seed bundles…", file=sys.stderr)
    v143_bundle = build_v143(v143_comps)
    v144_bundle = build_v144(set(v143_comps), v144_comps)
    v145_bundle = build_v145(set(v144_comps), v145_comps, patterns, guides)

    for bundle, version in [
        (v143_bundle, v143_str),
        (v144_bundle, v144_str),
        (v145_bundle, v145_str),
    ]:
        version_label = ".".join(version.split(".")[:2])
        out = _HERE / f"v{version_label}.json"
        out.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        print(f"  wrote {out.relative_to(_HERE.parent.parent)}", file=sys.stderr)

    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
