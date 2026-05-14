#!/usr/bin/env python3
"""Regenerate the Salt seed JSONs from public Salt sources.

This script is a one-shot regenerator — it is **not** part of `make
dev-seed`. CI never runs it. The committed JSONs in this directory are
what the loader reads.

Re-run when:
- Salt ships a new minor or major version and we want to bump the demo.
- The team wants to refresh the prop tables to reflect breaking changes.

Sources (authoritative → fallback):
- ``npm pack @salt-ds/core@<version>`` — TypeScript declarations under
  ``dist/`` give the component list + prop types. This is the
  authoritative shape for the ``interface`` attribute.
- ``https://www.saltdesignsystem.com/salt/components/<name>`` — for the
  category, summary, and accessibility notes that don't live in the
  .d.ts files. Plain HTML scrape with ``requests`` + ``beautifulsoup4``
  is enough if the site is SSR'd; if it switches to a fully client-side
  SPA shape, look for a JSON data endpoint via the network tab.
- ``https://www.saltdesignsystem.com/salt/patterns/`` and ``/guides/`` —
  for design-pattern and guide facts.
- ``https://github.com/jpmorganchase/salt-ds`` — fallback for component
  summaries when the docs site lacks them.

Usage::

    python seeds/salt-ds/scrape.py --version 1.45.0 --previous-version 1.44.0
    python seeds/salt-ds/scrape.py --offline  # use the cached tarballs in /tmp

Output: overwrites ``seeds/salt-ds/v<version>.json`` files. Review the
diff before committing — the loader will trust whatever's in there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).parent


def parse_dts(dts_path: Path) -> dict[str, list[dict[str, str | bool]]]:
    """Parse a TypeScript declaration file; return ``{component: props}``.

    Implementation notes:
    - The export shape Salt uses is ``export interface ButtonProps
      extends ...`` followed by a body of named properties. Regex over
      the source is sufficient — these are flat declarations.
    - For each prop, extract: name, type (everything between the colon
      and the trailing semicolon), required (no ``?`` in the name), and
      a default if a ``@default`` JSDoc tag is present in a preceding
      block.
    - Components without an exported Props interface (e.g. composition
      helpers) are skipped.

    Returns one entry per discovered component:
        {"Button": [{"name": "variant", "type": "'cta' | ...", "required": false, "default": "..."}, ...]}
    """
    raise NotImplementedError(
        "Wire the TypeScript-declaration parser here. The .d.ts files come "
        "from `npm pack @salt-ds/core@<v>` extracted to a temp dir. Salt's "
        "declarations are stable shape-wise; regex over `export interface "
        "<Name>Props {...}` blocks is enough. See the v1.43.json file for "
        "the target output shape — every salt-* component entity carries an "
        "`interface` attribute with this exact structure."
    )


def fetch_component_metadata(version: str) -> dict[str, dict[str, str]]:
    """Pull category / summary / accessibility notes from the docs site.

    Returns ``{component_slug: {"display_name": ..., "category": ...,
    "summary": ..., "a11y_notes": ...}}``.
    """
    raise NotImplementedError(
        "Hit https://www.saltdesignsystem.com/salt/components/ and parse "
        "each component page. The category pill is in a stable class; the "
        "summary is the first <p> after the page title. Rate-limit at >= 1s "
        "between requests and follow robots.txt. If the site is SPA-only, "
        "look for a JSON data endpoint — many docs sites expose one for "
        "search indexing."
    )


def fetch_patterns_and_guides() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return ``(patterns, guides)`` from the docs site.

    Each entry is ready to drop into a seed file's top-level ``facts``
    array with ``entity: "salt-design-system"``, ``category: "dev_doc"``
    (or a new ``pattern`` category if you add it to the vocabulary).
    """
    raise NotImplementedError(
        "Walk /salt/patterns/ and /salt/guides/ index pages and follow "
        "links. Each pattern/guide becomes a fact on salt-design-system. "
        "Title = page title, body = the page's main content as markdown "
        "(use html2text or similar)."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", default="1.45.0", help="Current Salt version to scrape.")
    p.add_argument("--previous-version", default="1.44.0", help="Previous version to also scrape.")
    p.add_argument(
        "--offline",
        action="store_true",
        help="Use cached tarballs in /tmp/salt-scrape/ instead of running npm pack.",
    )
    args = p.parse_args(argv)

    print(
        f"Salt scraper — target output: {_HERE}/v{args.version}.json + v{args.previous_version}.json",
        file=sys.stderr,
    )
    print(
        "Not yet implemented. See module docstring for source list and "
        "the existing v1.43.json for the target output shape.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
