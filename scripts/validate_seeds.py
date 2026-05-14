"""Validate every capability entity in seeds/ against the capability JSON Schema.

Runs without a database. Walks all seed bundles, computes the merged
attribute state for each capability across bundles (first-write-wins for
entity-level attributes, latest-non-invalidated for bitemporal_attributes),
and validates the merged state against the schema. Exits non-zero on any
violation so it can gate CI.

The schema path defaults to seeds/_templates/capability-schema.json — the
same file the loader registers via the capability_type_schemas section.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import jsonschema

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SEEDS_ROOT = _REPO_ROOT / "seeds"
_DEFAULT_SCHEMA_PATH = _SEEDS_ROOT / "_templates" / "capability-schema.json"

# Mirrors all_bundles() in seed.py: only NN-prefixed directories, sorted lex.
_BUNDLE_DIR_RE = re.compile(r"^\d{2}-")


def _bundle_paths(seeds_root: Path) -> list[Path]:
    """Same ordering rule the loader uses."""
    return sorted(
        p
        for d in sorted(seeds_root.iterdir())
        if d.is_dir() and _BUNDLE_DIR_RE.match(d.name)
        for p in sorted(d.glob("*.json"))
    )


def _merge_attrs(state: dict[str, Any], incoming: dict[str, Any]) -> None:
    """First-write-wins per key, mirroring the loader's upsert behaviour
    documented in seeds/README.md (existing live attribute rows are left
    alone)."""
    for k, v in incoming.items():
        if k not in state:
            state[k] = v


def _collect_capability_state(bundle_paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Walk bundles in load order; return {capability_name: merged_attrs}."""
    state: dict[str, dict[str, Any]] = {}

    for path in bundle_paths:
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"FAIL  {path.relative_to(_REPO_ROOT)}: invalid JSON — {e}", file=sys.stderr)
            sys.exit(2)

        for ent in bundle.get("entities", []) or []:
            if ent.get("entity_type") != "capability":
                continue
            name = ent.get("name")
            if not name:
                continue
            attrs = ent.get("attributes") or {}
            state.setdefault(name, {})
            _merge_attrs(state[name], attrs)

        # bitemporal_attributes carries the temporal-aware values
        # (e.g. current_version over time). Each entry is an envelope
        # {entity, key, rows: [{value, valid_from, valid_to}, ...]}.
        # The live value is the row with valid_to=None (or absent).
        for entry in bundle.get("bitemporal_attributes", []) or []:
            name = entry.get("entity") or entry.get("entity_name")
            key = entry.get("key")
            if not name or key is None or name not in state:
                continue
            rows = entry.get("rows") or []
            live = next(
                (r for r in rows if r.get("valid_to") in (None, "")),
                None,
            )
            if live is None:
                continue
            # bitemporal is more authoritative than the inline attribute —
            # overwrite (unlike _merge_attrs which is first-write-wins).
            state[name][key] = live.get("value")

    return state


def _validate(state: dict[str, dict[str, Any]], schema: dict[str, Any]) -> tuple[int, int]:
    validator = jsonschema.Draft202012Validator(schema)
    ok = fail = 0
    for name in sorted(state):
        errs = sorted(validator.iter_errors(state[name]), key=lambda e: list(e.absolute_path))
        if not errs:
            print(f"OK    capability={name}")
            ok += 1
            continue
        fail += 1
        print(f"FAIL  capability={name}")
        for e in errs:
            loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
            print(f"      - {loc}: {e.message}")
    return ok, fail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate every capability entity in seeds/ against the capability "
            "JSON Schema. Operates on the merged attribute state across all "
            "bundles in load order — delta bundles do not produce false-positive "
            "missing-required-field errors."
        )
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=_DEFAULT_SCHEMA_PATH,
        help=f"Path to the JSON Schema (default: {_DEFAULT_SCHEMA_PATH.relative_to(_REPO_ROOT)}).",
    )
    parser.add_argument(
        "--seeds-root",
        type=Path,
        default=_SEEDS_ROOT,
        help=f"Path to the seeds root (default: {_SEEDS_ROOT.relative_to(_REPO_ROOT)}).",
    )
    args = parser.parse_args(argv)

    if not args.schema.is_file():
        print(f"error: schema not found at {args.schema}", file=sys.stderr)
        return 2
    schema = json.loads(args.schema.read_text(encoding="utf-8"))

    bundles = _bundle_paths(args.seeds_root)
    if not bundles:
        print(f"error: no seed bundles found under {args.seeds_root}", file=sys.stderr)
        return 2

    state = _collect_capability_state(bundles)
    if not state:
        print("error: no capability entities found in seeds", file=sys.stderr)
        return 2

    print(f"Validating {len(state)} capability/capabilities against {args.schema.relative_to(_REPO_ROOT)}")
    print(f"Bundles walked: {len(bundles)}")
    print()
    ok, fail = _validate(state, schema)
    print()
    print(f"Summary: {ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
