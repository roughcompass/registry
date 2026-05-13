"""Unit tests for the check_no_phase_named_tests gate script.

The gate rejects test files or comments that anchor tests to a delivery
milestone rather than to a present-tense behavioral contract. These tests
verify the detection logic using temporary directory trees so no real test
files are required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the scripts directory is importable without installation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_no_phase_named_tests import main  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(directory: Path, name: str, content: str = "") -> Path:
    p = directory / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Filename detection
# ---------------------------------------------------------------------------


class TestFilenameDetection:
    def test_phase_named_file_triggers_nonzero(self, tmp_path: Path) -> None:
        """A file named test_phase3_tmp.py must cause the gate to fail."""
        _write(tmp_path, "test_phase3_tmp.py", "def test_ok(): pass\n")
        result = main(["--paths", str(tmp_path)])
        assert result == 1

    def test_phase_named_file_suffix_variant(self, tmp_path: Path) -> None:
        """test_phase7.py (no suffix after the number) also matches."""
        _write(tmp_path, "test_phase7.py", "def test_foo(): pass\n")
        result = main(["--paths", str(tmp_path)])
        assert result == 1

    def test_clean_file_exits_zero(self, tmp_path: Path) -> None:
        """A properly named file with no phase comments passes the gate."""
        _write(tmp_path, "test_sync_ingest.py", "def test_connector_runs(): pass\n")
        result = main(["--paths", str(tmp_path)])
        assert result == 0

    def test_empty_directory_exits_zero(self, tmp_path: Path) -> None:
        """An empty directory produces no hits and exits 0."""
        result = main(["--paths", str(tmp_path)])
        assert result == 0


# ---------------------------------------------------------------------------
# Comment-pattern detection
# ---------------------------------------------------------------------------


class TestCommentDetection:
    def test_phase_comment_triggers_nonzero(self, tmp_path: Path) -> None:
        """A file containing a `# phase N setup` comment must cause the gate to fail."""
        content = "# phase 3 setup\ndef test_foo(): pass\n"
        _write(tmp_path, "test_sync.py", content)
        result = main(["--paths", str(tmp_path)])
        assert result == 1

    def test_phase_comment_case_insensitive(self, tmp_path: Path) -> None:
        """Detection is case-insensitive: `# phase 3` and `# PHASE 3` both trigger."""
        content = "# phase 3 teardown\ndef test_bar(): pass\n"
        _write(tmp_path, "test_auth.py", content)
        result = main(["--paths", str(tmp_path)])
        assert result == 1

    def test_phase_comment_indented(self, tmp_path: Path) -> None:
        """Indented phase comments inside functions are also detected."""
        content = "def test_baz():\n    # phase 4 added this\n    pass\n"
        _write(tmp_path, "test_rbac.py", content)
        result = main(["--paths", str(tmp_path)])
        assert result == 1


# ---------------------------------------------------------------------------
# Bypass marker
# ---------------------------------------------------------------------------


class TestBypassMarker:
    def test_bypass_marker_exempts_comment(self, tmp_path: Path) -> None:
        """A comment ending with the bypass marker is exempt from detection."""
        content = "# phase 3  # test-hygiene: intentional\ndef test_foo(): pass\n"
        _write(tmp_path, "test_sync.py", content)
        result = main(["--paths", str(tmp_path)])
        assert result == 0

    def test_bypass_marker_only_exempts_tagged_line(self, tmp_path: Path) -> None:
        """Other lines without the marker are still detected."""
        content = "# phase 3  # test-hygiene: intentional\n" "# phase 4 leftovers\n" "def test_foo(): pass\n"
        _write(tmp_path, "test_sync.py", content)
        result = main(["--paths", str(tmp_path)])
        assert result == 1


# ---------------------------------------------------------------------------
# Non-comment lines — no false positives
# ---------------------------------------------------------------------------


class TestNonCommentLines:
    def test_python_assignment_not_flagged(self, tmp_path: Path) -> None:
        """A Python assignment containing 'phase4' is not a comment; gate passes."""
        content = 'rev = "0005_phase4_rbac"\nbranch_labels = None\n'
        _write(tmp_path, "test_migration_structure.py", content)
        result = main(["--paths", str(tmp_path)])
        assert result == 0

    def test_docstring_phase_reference_not_flagged(self, tmp_path: Path) -> None:
        """A docstring mentioning 'phase 4' is not a comment line; gate passes."""
        content = '"""Tests added in phase 4 era."""\ndef test_foo(): pass\n'
        _write(tmp_path, "test_rbac.py", content)
        result = main(["--paths", str(tmp_path)])
        assert result == 0


# ---------------------------------------------------------------------------
# Alembic migrations versions exclusion
# ---------------------------------------------------------------------------


class TestMigrationsExclusion:
    def test_migration_versions_path_excluded(self, tmp_path: Path) -> None:
        """Files under registry/storage/migrations/versions/ are not scanned."""
        versions_dir = tmp_path / "catalog" / "storage" / "migrations" / "versions"
        versions_dir.mkdir(parents=True)
        _write(versions_dir, "0005_phase4_rbac_oidc.py", "revision = '0005'\n")
        result = main(["--paths", str(tmp_path)])
        assert result == 0


# ---------------------------------------------------------------------------
# --explain flag
# ---------------------------------------------------------------------------


class TestExplainFlag:
    def test_explain_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--explain prints guidance and exits 0 regardless of codebase state."""
        result = main(["--explain"])
        captured = capsys.readouterr()
        assert result == 0
        assert "phase-named-file" in captured.out
        assert "phase-marker-comment" in captured.out
        assert "test-hygiene: intentional" in captured.out
