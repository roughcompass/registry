"""Unit tests for scripts/mint_token.py CLI argument validation.

Focuses on the --expires-days guard: 0 or negative values would mint a
token that is already expired, leaving the operator with an exit-0 CLI
that prints a token + every subsequent API call returning 401. The
script must reject such values at parse time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.mint_token import _parse_args  # noqa: E402


def _required_args() -> list[str]:
    return [
        "--tenant-id",
        "00000000-0000-0000-0000-000000000001",
        "--actor-id",
        "00000000-0000-0000-0000-000000000002",
        "--roles",
        "consumer",
    ]


def test_mint_token_rejects_non_positive_expires_days() -> None:
    """--expires-days 0 or negative must exit non-zero with a helpful message."""
    for bad in (0, -5):
        with pytest.raises(SystemExit) as exc_info:
            _parse_args([*_required_args(), "--expires-days", str(bad)])
        # argparse parser.error() exits with code 2.
        assert exc_info.value.code != 0


def test_mint_token_accepts_positive_expires_days() -> None:
    """A valid --expires-days 1 must parse cleanly and yield expires_days=1."""
    args = _parse_args([*_required_args(), "--expires-days", "1"])
    assert args.expires_days == 1


def test_mint_token_allows_omitted_expires_days() -> None:
    """Omitting --expires-days yields None (no-expiry token) — pre-existing behavior."""
    args = _parse_args(_required_args())
    assert args.expires_days is None


def test_mint_token_error_message_mentions_minimum(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The error message must give a concrete, actionable hint about the minimum."""
    with pytest.raises(SystemExit):
        _parse_args([*_required_args(), "--expires-days", "0"])

    captured = capsys.readouterr()
    msg = (captured.err + captured.out).lower()
    assert "positive" in msg or "must be" in msg, msg
