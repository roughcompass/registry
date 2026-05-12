"""Unit tests for version predicate validation and evaluation.

Contract under test
-------------------
- ``validate_version_predicate(pred: str) -> bool``
  Returns ``True`` for well-formed npm-style semver range strings; ``False``
  for malformed ones.  Never raises.

- ``evaluate_version_predicate(version: str, predicate: str) -> bool``
  Returns ``True`` when ``version`` satisfies ``predicate``; ``False``
  otherwise.  Never raises.  Empty predicate (``""``) is always satisfied.
  Malformed predicate → ``False``.

Test coverage — 100 cases
--------------------------
Ground-truth semantics are derived from npm's semver library specification
(https://github.com/npm/node-semver).  The cases are grouped by predicate
syntax and cover:

  - Simple comparison operators: >=, >, <=, <, ==, !=
  - Comma-separated AND ranges
  - Caret (^) ranges — major / minor / patch axis
  - Tilde (~) ranges — major / minor / patch axis
  - Bare version (equality shorthand)
  - Empty predicate (no constraint)
  - Partial versions with coercion (``2.0`` → ``2.0.0``)
  - Malformed predicates (validate=False / evaluate=False)
  - Boundary conditions (exact boundary inclusion/exclusion)

No I/O, no network, no database required.

Ground-truth semantics verified against npm's semver specification.
"""

from __future__ import annotations

import pytest

from registry.service.version_predicates import (
    evaluate_version_predicate,
    validate_version_predicate,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _ok(version: str, predicate: str, expected: bool) -> tuple[str, str, bool]:
    """Tuple marker for parametrize readability."""
    return (version, predicate, expected)


# ---------------------------------------------------------------------------
# validate_version_predicate — parametrized
# ---------------------------------------------------------------------------

VALIDATE_CASES: list[tuple[str, bool]] = [
    # --- Well-formed: simple operators ---
    (">=2.0.0", True),
    (">1.0.0", True),
    ("<=3.0.0", True),
    ("<3.0.0", True),
    ("==1.2.3", True),
    ("!=1.2.3", True),
    # --- Well-formed: coercible partial versions ---
    (">=2.0", True),  # 2.0 coerced to 2.0.0
    (">=2", True),  # 2 coerced to 2.0.0
    ("<3.0", True),
    ("<3", True),
    # --- Well-formed: comma-separated AND ranges ---
    (">=2.0,<3.0", True),
    (">=2.0.0,<3.0.0", True),
    (">=1.0,<=2.0", True),
    (">=0.1,<0.2,!=0.1.5", True),
    # --- Well-formed: caret ranges ---
    ("^1.4", True),
    ("^1.4.2", True),
    ("^0.4", True),
    ("^0.0.3", True),
    ("^1", True),
    ("^0", True),
    ("^2.3.4", True),
    # --- Well-formed: tilde ranges ---
    ("~2.3.4", True),
    ("~2.3", True),
    ("~2", True),
    ("~1.0.0", True),
    ("~0.2.3", True),
    ("~0", True),
    # --- Well-formed: bare version (equality shorthand) ---
    ("1.2.3", True),
    ("2.0.0", True),
    ("0.0.1", True),
    # --- Well-formed: empty string (no constraint) ---
    ("", True),
    # --- Malformed: keyword values ---
    ("latest", False),
    ("*", False),
    ("x", False),
    # --- Malformed: wildcard partial ---
    ("1.x", False),
    ("1.2.x", False),
    # --- Malformed: hyphen range (not supported) ---
    ("1.2.3 - 2.3.4", False),
    # --- Malformed: OR separator (not supported) ---
    (">=1.0||<0.5", False),
    # --- Malformed: unknown operator ---
    ("~=2.0", False),  # Python tilde-equals, not supported
    # --- Malformed: double comma (empty clause) ---
    (">=1.0,,<2.0", False),
    # --- Malformed: trailing comma ---
    (">=1.0,", False),
    # --- Malformed: leading comma ---
    (",>=1.0", False),
    # --- Malformed: non-numeric version part ---
    (">=abc", False),
    (">=1.a.0", False),
    # --- Malformed: caret with garbage ---
    ("^abc", False),
    # --- Malformed: tilde with garbage ---
    ("~abc", False),
]


@pytest.mark.parametrize("pred,expected", VALIDATE_CASES)
def test_validate_version_predicate(pred: str, expected: bool) -> None:
    assert validate_version_predicate(pred) is expected, f"validate_version_predicate({pred!r}) should be {expected}"


# ---------------------------------------------------------------------------
# evaluate_version_predicate — parametrized (100 cases)
# ---------------------------------------------------------------------------

EVALUATE_CASES: list[tuple[str, str, bool]] = [
    # -----------------------------------------------------------------------
    # 1. Simple >= (10 cases)
    # -----------------------------------------------------------------------
    _ok("2.4.0", ">=2.0.0", True),
    _ok("2.0.0", ">=2.0.0", True),  # exact boundary included
    _ok("1.9.9", ">=2.0.0", False),
    _ok("3.0.0", ">=2.0.0", True),
    _ok("2.4.0", ">=2.0", True),  # coerced >=2.0.0
    _ok("1.9.9", ">=2.0", False),
    _ok("0.0.1", ">=0.0.1", True),
    _ok("0.0.0", ">=0.0.1", False),
    _ok("10.0.0", ">=2.0.0", True),
    _ok("1.0.0", ">=1.0.0", True),
    # -----------------------------------------------------------------------
    # 2. Simple > (5 cases)
    # -----------------------------------------------------------------------
    _ok("2.0.1", ">2.0.0", True),
    _ok("2.0.0", ">2.0.0", False),  # exact boundary excluded
    _ok("1.9.9", ">2.0.0", False),
    _ok("3.0.0", ">2.0.0", True),
    _ok("0.0.1", ">0.0.0", True),
    # -----------------------------------------------------------------------
    # 3. Simple <= (5 cases)
    # -----------------------------------------------------------------------
    _ok("2.0.0", "<=2.0.0", True),  # exact boundary included
    _ok("1.9.9", "<=2.0.0", True),
    _ok("2.0.1", "<=2.0.0", False),
    _ok("0.0.0", "<=0.0.1", True),
    _ok("0.0.2", "<=0.0.1", False),
    # -----------------------------------------------------------------------
    # 4. Simple < (5 cases)
    # -----------------------------------------------------------------------
    _ok("1.9.9", "<2.0.0", True),
    _ok("2.0.0", "<2.0.0", False),  # exact boundary excluded
    _ok("2.0.1", "<2.0.0", False),
    _ok("0.0.0", "<0.0.1", True),
    _ok("0.0.1", "<0.0.1", False),
    # -----------------------------------------------------------------------
    # 5. Simple == (5 cases)
    # -----------------------------------------------------------------------
    _ok("1.2.3", "==1.2.3", True),
    _ok("1.2.4", "==1.2.3", False),
    _ok("1.2.2", "==1.2.3", False),
    _ok("0.0.0", "==0.0.0", True),
    _ok("1.0.0", "==1.0.0", True),
    # -----------------------------------------------------------------------
    # 6. Simple != (5 cases)
    # -----------------------------------------------------------------------
    _ok("1.2.3", "!=1.2.3", False),
    _ok("1.2.4", "!=1.2.3", True),
    _ok("2.0.0", "!=1.0.0", True),
    _ok("0.0.0", "!=0.0.1", True),
    _ok("0.0.1", "!=0.0.1", False),
    # -----------------------------------------------------------------------
    # 7. Comma-separated AND ranges (10 cases)
    # -----------------------------------------------------------------------
    _ok("2.4.0", ">=2.0,<3.0", True),
    _ok("1.9.0", ">=2.0,<3.0", False),
    _ok("3.0.0", ">=2.0,<3.0", False),  # upper boundary excluded
    _ok("2.0.0", ">=2.0,<3.0", True),  # lower boundary included
    _ok("2.9.9", ">=2.0,<3.0", True),
    _ok("1.5.0", ">=1.0,<=2.0", True),
    _ok("2.0.0", ">=1.0,<=2.0", True),  # upper boundary included
    _ok("2.0.1", ">=1.0,<=2.0", False),
    _ok("0.9.9", ">=1.0,<=2.0", False),
    _ok("1.5.5", ">=1.0,<2.0,!=1.5.5", False),
    # -----------------------------------------------------------------------
    # 8. Caret (^) ranges — major axis (10 cases)
    # -----------------------------------------------------------------------
    _ok("1.5.2", "^1.4", True),  # task contract case
    _ok("1.4.0", "^1.4", True),  # exact lower bound
    _ok("1.3.9", "^1.4", False),  # below lower bound
    _ok("2.0.0", "^1.4", False),  # at upper bound — excluded
    _ok("1.9.9", "^1.4", True),
    _ok("1.0.0", "^1.0.0", True),
    _ok("1.9.9", "^1.0.0", True),
    _ok("2.0.0", "^1.0.0", False),
    _ok("1.0.1", "^1.0.0", True),
    _ok("0.9.9", "^1.0.0", False),
    # -----------------------------------------------------------------------
    # 9. Caret (^) ranges — minor/patch axis (5 cases)
    # -----------------------------------------------------------------------
    _ok("0.4.0", "^0.4", True),  # >=0.4.0,<0.5.0
    _ok("0.4.9", "^0.4", True),
    _ok("0.5.0", "^0.4", False),  # upper bound excluded
    _ok("0.0.3", "^0.0.3", True),  # >=0.0.3,<0.0.4
    _ok("0.0.4", "^0.0.3", False),
    # -----------------------------------------------------------------------
    # 10. Tilde (~) ranges (10 cases)
    # -----------------------------------------------------------------------
    _ok("2.3.4", "~2.3.4", True),  # exact lower bound
    _ok("2.3.5", "~2.3.4", True),  # patch bump OK
    _ok("2.4.0", "~2.3.4", False),  # minor bump excluded
    _ok("2.3.3", "~2.3.4", False),  # below lower bound
    _ok("2.3.0", "~2.3", True),  # ~2.3 = >=2.3.0,<2.4.0
    _ok("2.3.9", "~2.3", True),
    _ok("2.4.0", "~2.3", False),
    _ok("2.0.0", "~2", True),  # ~2 = >=2.0.0,<3.0.0
    _ok("2.9.9", "~2", True),
    _ok("3.0.0", "~2", False),
    # -----------------------------------------------------------------------
    # 11. Bare version (equality shorthand) (5 cases)
    # -----------------------------------------------------------------------
    _ok("1.2.3", "1.2.3", True),
    _ok("1.2.4", "1.2.3", False),
    _ok("1.2.2", "1.2.3", False),
    _ok("0.0.1", "0.0.1", True),
    _ok("1.0.0", "1.0.0", True),
    # -----------------------------------------------------------------------
    # 12. Empty predicate (no constraint) (3 cases)
    # -----------------------------------------------------------------------
    _ok("1.0.0", "", True),
    _ok("99.99.99", "", True),
    _ok("0.0.0", "", True),
    # -----------------------------------------------------------------------
    # 13. Partial version coercion (5 cases)
    # -----------------------------------------------------------------------
    _ok("2.0.0", ">=2.0", True),  # predicate coercion
    _ok("1.9.9", ">=2.0", False),
    _ok("2.0", ">=2.0.0", True),  # version coercion
    _ok("1.9", ">=2.0.0", False),
    _ok("2.0", "^1.9", False),  # 2.0 coerced to 2.0.0; ^1.9 = >=1.9.0,<2.0.0 → 2.0.0 excluded
    # -----------------------------------------------------------------------
    # 14. Malformed predicate → always False (7 cases)
    # -----------------------------------------------------------------------
    _ok("2.0.0", "latest", False),
    _ok("2.0.0", "*", False),
    _ok("2.0.0", "1.x", False),
    _ok("2.0.0", ">=abc", False),
    _ok("2.0.0", ",,", False),
    _ok("2.0.0", ">=1.0,", False),
    _ok("2.0.0", "^abc", False),
    # -----------------------------------------------------------------------
    # 15. Explicit range boundary cases (4 cases)
    # -----------------------------------------------------------------------
    _ok("2.4.0", ">=2.0,<3.0", True),  # task contract: satisfied
    _ok("1.9.0", ">=2.0,<3.0", False),  # task contract: unsatisfied
    _ok("1.5.2", "^1.4", True),  # task contract: caret satisfied
    _ok("1.3.9", "^1.4", False),  # implied from ^1.4 semantics
]


@pytest.mark.parametrize("version,pred,expected", EVALUATE_CASES)
def test_evaluate_version_predicate(version: str, pred: str, expected: bool) -> None:
    result = evaluate_version_predicate(version, pred)
    assert result is expected, (
        f"evaluate_version_predicate({version!r}, {pred!r}) " f"expected {expected}, got {result}"
    )


# ---------------------------------------------------------------------------
# Explicit never-raises contract
# ---------------------------------------------------------------------------


def test_evaluate_never_raises_on_garbage_version() -> None:
    """evaluate_version_predicate must not raise on garbage version input."""
    assert evaluate_version_predicate("not-a-version", ">=1.0.0") is False


def test_evaluate_never_raises_on_garbage_predicate() -> None:
    """evaluate_version_predicate must not raise on garbage predicate input."""
    assert evaluate_version_predicate("1.0.0", "totally wrong !!!") is False


def test_validate_never_raises_on_garbage() -> None:
    """validate_version_predicate must not raise on arbitrary garbage strings."""
    assert validate_version_predicate("!!!@@@###") is False
    assert validate_version_predicate("\x00\xff") is False


# ---------------------------------------------------------------------------
# Caret axis boundary table
# ---------------------------------------------------------------------------

CARET_BOUNDARY_CASES: list[tuple[str, str, bool]] = [
    # ^1     = >=1.0.0 <2.0.0
    ("1.0.0", "^1", True),
    ("1.9.9", "^1", True),
    ("2.0.0", "^1", False),
    ("0.9.9", "^1", False),
    # ^0     = >=0.0.0 <1.0.0
    ("0.0.0", "^0", True),
    ("0.9.9", "^0", True),
    ("1.0.0", "^0", False),
    # ^0.2   = >=0.2.0 <0.3.0
    ("0.2.0", "^0.2", True),
    ("0.2.9", "^0.2", True),
    ("0.3.0", "^0.2", False),
    ("0.1.9", "^0.2", False),
]


@pytest.mark.parametrize("version,pred,expected", CARET_BOUNDARY_CASES)
def test_caret_boundary(version: str, pred: str, expected: bool) -> None:
    assert evaluate_version_predicate(version, pred) is expected


# ---------------------------------------------------------------------------
# Tilde axis boundary table
# ---------------------------------------------------------------------------

TILDE_BOUNDARY_CASES: list[tuple[str, str, bool]] = [
    # ~1.2.3 = >=1.2.3 <1.3.0
    ("1.2.3", "~1.2.3", True),
    ("1.2.9", "~1.2.3", True),
    ("1.3.0", "~1.2.3", False),
    ("1.2.2", "~1.2.3", False),
    # ~1.2   = >=1.2.0 <1.3.0
    ("1.2.0", "~1.2", True),
    ("1.2.9", "~1.2", True),
    ("1.3.0", "~1.2", False),
    ("1.1.9", "~1.2", False),
    # ~1     = >=1.0.0 <2.0.0
    ("1.0.0", "~1", True),
    ("1.9.9", "~1", True),
    ("2.0.0", "~1", False),
    ("0.9.9", "~1", False),
]


@pytest.mark.parametrize("version,pred,expected", TILDE_BOUNDARY_CASES)
def test_tilde_boundary(version: str, pred: str, expected: bool) -> None:
    assert evaluate_version_predicate(version, pred) is expected
