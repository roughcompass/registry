"""Lint gate: only permitted modules may INSERT INTO the tenants table.

Writing directly to the tenants table is a privileged operation — it affects
the trust boundary between tenant namespaces. Any module that inserts a row
into tenants creates a new principal in the authorization model, so these
callers must be audited and kept to a minimum.

Permitted callers (registry/registry/ subtree only; migrations and dev scripts
are excluded from this gate because they run under operator supervision):

    registry/registry/auth/rsam/tenant_store.py  — JIT materialization of
        RSAM SEAL tenants; inserts on first-sight with ON CONFLICT DO NOTHING.

    registry/registry/storage/migrations/versions/ — Alembic migrations may
        insert seed rows as part of schema bootstrapping; excluded from the
        gate rather than enumerated individually because the migration runner
        controls when they execute.

If you add a new `INSERT INTO tenants` caller:
1. Ensure the insert is protected by ON CONFLICT DO NOTHING or an equivalent
   idempotency guard so duplicate tenant rows are impossible.
2. Emit a tenant.* audit event in the same transaction.
3. Add the module path to _ALLOWED_CALLERS below and explain why it is permitted.

Run locally:
    python registry/scripts/check_visibility_bypass.py
    python registry/scripts/check_visibility_bypass.py --paths registry/registry/auth
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Default scope — the shipped application code only. Migrations, dev scripts,
# and tests are excluded: migrations run under operator control, dev scripts
# are not deployed, and tests need to seed tenants without being in production.
_DEFAULT_SCOPE: tuple[str, ...] = ("registry/registry",)

# Subtrees that are never flagged even when inside the default scope.
# Migrations are excluded because they legitimately seed tenant rows during
# schema bootstrapping — the migration runner controls when they execute.
_EXCLUDE_SUBTREES: tuple[str, ...] = (
    "registry/registry/storage/migrations",
)

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
    }
)

# The pattern that identifies a direct INSERT into tenants.
# Matches both `INSERT INTO tenants` and `INSERT  INTO  tenants` (extra whitespace).
_INSERT_TENANTS: re.Pattern[str] = re.compile(
    r"\bINSERT\s+INTO\s+tenants\b",
    re.IGNORECASE,
)

# Modules (relative to repo root) that are allowed to INSERT INTO tenants.
# Each entry must include a brief justification comment.
#
# tenant_store.py — JIT tenant materialization for RSAM SEAL IDs. Inserts
#   on first sight with ON CONFLICT DO NOTHING; emits tenant.jit_created
#   in the same transaction so tenant creation is always audited atomically.
_ALLOWED_CALLERS: frozenset[str] = frozenset(
    {
        "registry/registry/auth/rsam/tenant_store.py",
    }
)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _resolve_targets(scope: list[str]) -> list[Path]:
    """Expand the scope list into concrete .py files to scan."""
    excluded_roots = [(_REPO_ROOT / p).resolve() for p in _EXCLUDE_SUBTREES]
    out: list[Path] = []
    for entry in scope:
        target = (_REPO_ROOT / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".py":
                out.append(target)
            continue
        for path in target.rglob("*.py"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            if any(path.is_relative_to(excl) for excl in excluded_roots):
                continue
            out.append(path)
    return out


def _check_file(path: Path) -> list[tuple[int, str]]:
    """Return (line_no, line_text) for every disallowed INSERT INTO tenants hit."""
    rel = str(path.relative_to(_REPO_ROOT))
    if rel in _ALLOWED_CALLERS:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    return [
        (i + 1, line)
        for i, line in enumerate(lines)
        if _INSERT_TENANTS.search(line)
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that only permitted modules INSERT INTO tenants.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=list(_DEFAULT_SCOPE),
        help="Repo-relative paths to scan (default: registry/registry).",
    )
    args = parser.parse_args(argv)

    targets = _resolve_targets(args.paths)
    if not targets:
        print(
            "no files in scope (paths: " + ", ".join(args.paths) + ")",
            file=sys.stderr,
        )
        return 0

    violations: list[str] = []
    for path in targets:
        for line_no, line_text in _check_file(path):
            rel = path.relative_to(_REPO_ROOT)
            violations.append(f"{rel}:{line_no}: unpermitted INSERT INTO tenants\n    {line_text.strip()}")

    if not violations:
        return 0

    for v in violations:
        print(v)
    print(
        f"\n{len(violations)} unpermitted INSERT INTO tenants call(s) found.\n"
        "Add the module to _ALLOWED_CALLERS in registry/scripts/check_visibility_bypass.py\n"
        "only after confirming it uses ON CONFLICT DO NOTHING and emits an audit event.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
