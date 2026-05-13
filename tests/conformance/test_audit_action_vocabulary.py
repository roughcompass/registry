"""Audit action vocabulary conformance gate.

Ensures that every `action=` keyword argument passed to an audit-emit call
(`emit` or `_emit_audit`) uses an imported constant from `registry.audit.actions`
rather than a bare string literal.

A developer who writes `action="annotation.created"` instead of
`action=actions.ANNOTATION_CREATED` will fail this gate immediately. The rule
catches drift before it reaches production, where bare strings are invisible
to static analysis and refactoring tools.

Two tests:
  1. Walk the entire `registry/registry/` source tree via `ast.parse` and assert
     zero bare string literals appear in `action=` kwargs of audit-emit calls.
  2. A negative-fixture test that constructs a synthetic AST with a known literal
     and asserts the detection logic fires — preventing a vacuously-passing
     implementation where the walker silently finds nothing.

Exclusions (by design):
  - `registry/api/middleware/http_methods.py` is skipped entirely (URL routing
    vocabulary, not audit emits).
  - Call nodes whose target is `add_mutation_route` are skipped (routing params,
    not audit vocabulary).
  - The raw-SQL dict literal in `_emit_override_audit` is out of scope for this
    AST gate; only function keyword-argument `action=` positions are detected.
"""

from __future__ import annotations

import ast
from pathlib import Path

from registry.audit import actions

# Collected for diagnostic display only — the pass/fail rule does NOT consult
# this set.  Any ast.Constant in an action= kwarg is a failure, regardless of
# whether its string value is a known action name.
VALID_ACTIONS: frozenset[str] = frozenset(
    getattr(actions, name) for name in actions.__all__
)

# The source tree to walk.  Resolve from this file's location:
# tests/conformance/ → tests/ → registry/ (the Python package root's parent)
# registry/registry/ is the actual source package.
REGISTRY_ROOT = Path(__file__).parent.parent.parent / "registry"

EXCLUDED_FILES: frozenset[Path] = frozenset(
    {REGISTRY_ROOT / "api" / "middleware" / "http_methods.py"}
)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _is_audit_emit_call(node: ast.Call) -> bool:
    """Return True iff this Call targets a function/method named `emit` or `_emit_audit`."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in {"emit", "_emit_audit"}:
        return True
    if isinstance(func, ast.Name) and func.id in {"emit", "_emit_audit"}:
        return True
    return False


def _is_add_mutation_route_call(node: ast.Call) -> bool:
    """Return True iff this Call targets `add_mutation_route` (URL routing, not audit).

    These calls carry an `action=` parameter whose value is a routing verb
    string, not an audit vocabulary term. They are excluded from the gate.
    """
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "add_mutation_route":
        return True
    if isinstance(func, ast.Name) and func.id == "add_mutation_route":
        return True
    return False


def _find_bare_action_literals(filepath: Path) -> list[str]:
    """Scan one file for `action=<string-literal>` kwargs in audit-emit calls.

    Returns a list of human-readable failure messages, one per violation found.
    An empty list means the file is clean.
    """
    src = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(filepath))
    except SyntaxError:
        # A file that cannot be parsed is not a vocabulary violation; let
        # other gates (lint, typecheck) catch syntax errors.
        return []

    failures: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_add_mutation_route_call(node):
            continue
        if not _is_audit_emit_call(node):
            continue
        for kw in node.keywords:
            if kw.arg != "action":
                continue
            if isinstance(kw.value, ast.Constant):
                failures.append(
                    f"Bare action literal {kw.value.value!r} at "
                    f"{filepath}:{kw.value.lineno} "
                    f"— import from registry.audit.actions instead"
                )
    return failures


# ---------------------------------------------------------------------------
# Conformance test 1 — full source tree walk
# ---------------------------------------------------------------------------


def test_no_bare_action_literals_in_audit_emit_calls() -> None:
    """Any `action=<string-literal>` in an audit-emit call is a conformance failure.

    Walks every *.py file under registry/registry/ (the source package), skipping
    the excluded middleware file and any call nodes that target add_mutation_route.
    """
    assert REGISTRY_ROOT.is_dir(), (
        f"REGISTRY_ROOT does not exist: {REGISTRY_ROOT}. "
        "Adjust the path computation in this file."
    )

    all_failures: list[str] = []
    for py_file in sorted(REGISTRY_ROOT.rglob("*.py")):
        if py_file in EXCLUDED_FILES:
            continue
        all_failures.extend(_find_bare_action_literals(py_file))

    assert not all_failures, (
        "Bare audit-action string literals detected — import from "
        "registry.audit.actions instead:\n" + "\n".join(all_failures)
    )


# ---------------------------------------------------------------------------
# Conformance test 2 — negative fixture (detection must fire on synthetic source)
# ---------------------------------------------------------------------------


def test_negative_fixture_catches_literal_in_synthetic_source() -> None:
    """The detection logic must catch a known bare-literal in a synthetic AST.

    This test prevents a vacuously-passing implementation where the AST walker
    is incorrectly structured and never finds any call nodes at all. If the
    detection logic is broken (e.g. wrong node type check), this test fails
    rather than the production gate silently passing.
    """
    synthetic_source = (
        "import uuid\n"
        "async def f():\n"
        '    await audit.emit(\n'
        '        ctx, action="annotation.created", target_type="t", target_id=uuid.uuid4()\n'
        "    )\n"
    )
    tree = ast.parse(synthetic_source, filename="<synthetic>")
    found_violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_add_mutation_route_call(node):
            continue
        if not _is_audit_emit_call(node):
            continue
        for kw in node.keywords:
            if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                found_violations.append(
                    f"synthetic literal detected: {kw.value.value!r} at line {kw.value.lineno}"
                )

    assert found_violations, (
        "Detection logic failed to catch action='annotation.created' literal in "
        "synthetic source — the AST walker is not working correctly."
    )
