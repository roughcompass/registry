"""Tests for the no-internal-doc-references linter.

The script lives at ``registry/scripts/check_no_doc_refs.py``
and enforces the rule defined in ``CLAUDE.md`` at the repo root.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "registry" / "scripts" / "check_no_doc_refs.py"


def _load_script_module():
    """Import the script as a module without polluting sys.path globally."""
    spec = importlib.util.spec_from_file_location("check_no_doc_refs", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_no_doc_refs"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


# ---------------------------------------------------------------------------
# Pattern detection — happy path
# ---------------------------------------------------------------------------


# Fixture strings are assembled at runtime so the test file's source does
# not itself trigger the gate. Each token's bracketed digits are split so
# the regex can't match the literal in this file.
def _bad(pattern_id: str) -> str:
    """Build a single example of a forbidden pattern at runtime."""
    return {
        "adr": "ADR-" + "024",
        "f": "F7" + ".12",
        "oq": "OQ-" + "P7-3",
        "cap": "CAP-P7-" + "T20",
        "cc": "CC-" + "T01",
        "drc": "DRC-" + "T03",
        "aq": "AQ" + "7",
        "prd": "PRD " + "§",
        "tdd": "TDD " + "§",
        "doc": "interfaces.md " + "§",
        "phase": "Phase " + "7",
    }[pattern_id]


def test_violations_caught_in_fixture_file(tmp_path: Path, script_module) -> None:
    """A fixture line carrying every forbidden pattern produces one hit per pattern."""
    f = tmp_path / "bad.py"
    tokens = [
        _bad("adr"),
        _bad("f"),
        _bad("oq"),
        _bad("cap"),
        _bad("cc"),
        _bad("drc"),
        _bad("aq"),
        _bad("prd"),
        _bad("tdd"),
        _bad("doc"),
        _bad("phase"),
    ]
    body = '"""Module — ' + ", ".join(tokens) + '."""\n'
    f.write_text(body)
    hits = script_module._scan_file(f)
    found = {h.pattern.name for h in hits}
    assert "ADR-NNN" in found
    assert "F<n>.<n>" in found
    assert "OQ-…" in found
    assert "CAP-PN-TNN" in found
    assert "CC-TNN" in found
    assert "DRC-TNN" in found
    assert "AQ<n>" in found
    assert "<doc>.md §" in found
    assert "Phase <n>" in found


def test_clean_file_produces_zero_hits(tmp_path: Path, script_module) -> None:
    f = tmp_path / "good.py"
    f.write_text(
        '"""Service module.\n\n'
        "Visibility is enforced at one layer so cross-tenant data cannot leak.\n"
        '"""\n'
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
    )
    assert script_module._scan_file(f) == []


# ---------------------------------------------------------------------------
# Intentional-bypass marker
# ---------------------------------------------------------------------------


def test_intentional_bypass_line_is_ignored(tmp_path: Path, script_module) -> None:
    """Lines ending with ``# doc-ref: intentional`` are excluded from the gate."""
    f = tmp_path / "with_bypass.py"
    f.write_text(f"# See {_bad('adr')} for context.  # doc-ref: intentional\n" "x = 1\n")
    assert script_module._scan_file(f) == []


def test_bypass_is_per_line_not_per_file(tmp_path: Path, script_module) -> None:
    """A bypass on one line does not exempt other lines in the file."""
    f = tmp_path / "mixed.py"
    f.write_text(
        f"# See {_bad('adr')} for context.  # doc-ref: intentional\n"
        f"# But this line still cites {_bad('f')} with no bypass.\n"
    )
    hits = script_module._scan_file(f)
    assert len(hits) == 1
    assert hits[0].pattern.name == "F<n>.<n>"
    assert hits[0].line_no == 2


# ---------------------------------------------------------------------------
# EVAL.md exception — task IDs allowed as commit-history anchors
# ---------------------------------------------------------------------------


def test_eval_md_exempts_task_id_patterns_only(tmp_path: Path, script_module) -> None:
    """``eval/EVAL.md`` may keep ``CAP-PN-TNN`` / ``CC-TNN`` / ``DRC-TNN``
    as commit-history anchors, but every other forbidden pattern still
    fires.
    """
    f = tmp_path / "EVAL.md"
    f.write_text(
        f"| Breaking-change advisor | done | {_bad('cap')} |\n"
        f"| Config consolidation | done | {_bad('cc')} |\n"
        f"| Doc-ref cleanup | done | {_bad('drc')} |\n"
        f"| But {_bad('adr')} must still fire here |\n"
    )
    hits = script_module._scan_file(f)
    found = {h.pattern.name for h in hits}
    assert "CAP-PN-TNN" not in found
    assert "CC-TNN" not in found
    assert "DRC-TNN" not in found
    assert "ADR-NNN" in found


def test_eval_exception_only_applies_to_files_named_EVAL_md(tmp_path: Path, script_module) -> None:
    """A non-EVAL.md file does NOT get the task-ID exemption."""
    f = tmp_path / "notes.md"
    f.write_text(f"Anchor: {_bad('cap')}\n")
    hits = script_module._scan_file(f)
    found = {h.pattern.name for h in hits}
    assert "CAP-PN-TNN" in found


# ---------------------------------------------------------------------------
# Pattern boundaries — avoid over-match
# ---------------------------------------------------------------------------


def test_AQ_pattern_does_not_match_unrelated_words(tmp_path: Path, script_module) -> None:
    """AQ<n> is a word-boundary match; ``AQUARIUM`` and ``AQs`` should not fire."""
    f = tmp_path / "x.py"
    f.write_text("AQUARIUM = 1\nAQs_list = []\n")
    assert script_module._scan_file(f) == []


def test_F_pattern_requires_dot_and_digits(tmp_path: Path, script_module) -> None:
    """``F<n>.<n>`` requires the digit-dot-digit shape, not bare ``F`` or ``Foo``."""
    f = tmp_path / "x.py"
    f.write_text("F = 0\nFoo = 'bar'\nFizz = 'buzz'\n")
    assert script_module._scan_file(f) == []


def test_phase_pattern_does_not_match_non_milestone_phrases(tmp_path: Path, script_module) -> None:
    """``Phase <n>`` must include a digit; ``Phase A`` or ``Phase one`` are not hits."""
    f = tmp_path / "x.py"
    f.write_text("# Phase A of the workflow\n# Phase one is setup\n")
    assert script_module._scan_file(f) == []


# ---------------------------------------------------------------------------
# CLI smoke test — --explain exits 0 and lists patterns
# ---------------------------------------------------------------------------


def test_explain_lists_every_pattern(capsys, script_module) -> None:
    exit_code = script_module._print_explain()
    assert exit_code == 0
    out = capsys.readouterr().out
    for pat in script_module._PATTERNS:
        assert pat.name in out


# ---------------------------------------------------------------------------
# Full-repo scan — invariant
# ---------------------------------------------------------------------------


def test_repo_is_currently_clean(script_module) -> None:
    """Backstop: the gate must exit 0 against the full shipped scope.

    This test is the canary that proves the cleanup held — anyone who
    re-introduces a violation triggers it locally before CI.
    """
    targets = script_module._resolve_targets(list(script_module._DEFAULT_SCOPE))
    all_hits = []
    for path in targets:
        all_hits.extend(script_module._scan_file(path))
    if all_hits:
        sample = "\n".join(f"  {h.path.name}:{h.line_no}: {h.pattern.name}: {h.matched}" for h in all_hits[:10])
        pytest.fail(f"{len(all_hits)} forbidden reference(s) found in shipped code. " f"First 10:\n{sample}")
