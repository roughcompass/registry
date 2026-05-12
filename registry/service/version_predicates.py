"""Version predicate validation and evaluation — semver range parsing and evaluation.

Implements npm-style semver range parsing and evaluation in pure Python,
using the ``semver`` library for individual comparisons.

Supported range syntax
----------------------
- Simple comparisons: ``>=2.0.0``, ``<3.0.0``, ``==1.2.3``, ``!=1.2.3``
- Comma-separated AND ranges: ``>=2.0,<3.0``  (all clauses must be satisfied)
- Caret ranges: ``^1.4``, ``^1.4.2``, ``^0.4``, ``^0.0.3``
- Tilde ranges: ``~2.3.4``, ``~2.3``, ``~2``
- Bare version (equality): ``1.2.3``
- Empty string (no constraint): always satisfied

Intentionally unsupported (return False from validate / False from evaluate):
- Hyphen ranges: ``1.2.3 - 2.3.4``
- Wildcard/X ranges: ``1.x``, ``1.2.x``, ``*``
- OR-separated ranges (``||``)

Design principles
-----------------
- ``validate_version_predicate`` is used at write-time; returns ``False`` for
  malformed predicates (caller must raise 422).
- ``evaluate_version_predicate`` is used at query-time; returns ``False`` for
  unsatisfied predicates and for malformed predicates — never raises.
- Both functions are pure (no I/O).  The ``semver`` library is used only for
  individual atomic comparisons; all range expansion is done here.

npm-style caret semantics (^)
------------------------------
Caret ranges allow changes that do not modify the left-most non-zero
version digit:
  ^1.2.3  :=  >=1.2.3 <2.0.0
  ^0.2.3  :=  >=0.2.3 <0.3.0
  ^0.0.3  :=  >=0.0.3 <0.0.4
  ^1.2    :=  >=1.2.0 <2.0.0
  ^1      :=  >=1.0.0 <2.0.0
  ^0.2    :=  >=0.2.0 <0.3.0
  ^0      :=  >=0.0.0 <1.0.0

npm-style tilde semantics (~)
------------------------------
Tilde ranges allow patch-level changes when minor is specified, or
minor-level changes when only major is specified:
  ~1.2.3  :=  >=1.2.3 <1.3.0
  ~1.2    :=  >=1.2.0 <1.3.0
  ~1      :=  >=1.0.0 <2.0.0
"""

from __future__ import annotations

import logging
import re

import semver

_log = logging.getLogger(__name__)

# Regex for a single atomic comparison clause: optional operator + version.
# Operator group is optional; bare versions are treated as equality (==).
_SIMPLE_OP_RE = re.compile(r"^(?P<op>>=|<=|!=|==|>|<)?\s*(?P<ver>\S+)$")

# Valid simple operators accepted by semver.Version.match
_VALID_OPS = frozenset({">=", "<=", "!=", "==", ">", "<"})

# ---------------------------------------------------------------------------
# Version coercion
# ---------------------------------------------------------------------------


def _coerce_version(ver_str: str) -> str | None:
    """Normalise an incomplete version string to three-part semver.

    ``"2.0"`` → ``"2.0.0"``; ``"1"`` → ``"1.0.0"``.
    Returns ``None`` if the string cannot be coerced.
    """
    # Strip leading ``v`` (e.g. ``v1.2.3``).
    ver_str = ver_str.strip().lstrip("v")
    parts = ver_str.split(".")
    if len(parts) == 1:
        # Major only
        try:
            int(parts[0])
        except ValueError:
            return None
        return f"{parts[0]}.0.0"
    if len(parts) == 2:
        # Major.Minor only
        try:
            int(parts[0])
            int(parts[1])
        except ValueError:
            return None
        return f"{parts[0]}.{parts[1]}.0"
    # Three or more parts — try to parse as-is; semver.Version.parse will
    # accept pre-release / build suffixes if present.
    return ver_str


def _parse_version(ver_str: str) -> semver.Version | None:
    """Return a ``semver.Version`` or ``None`` on parse failure."""
    coerced = _coerce_version(ver_str)
    if coerced is None:
        return None
    try:
        return semver.Version.parse(coerced)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Range expansion helpers
# ---------------------------------------------------------------------------


def _expand_caret(ver_str: str) -> list[str] | None:
    """Expand a caret range into a list of atomic comparison strings.

    Returns ``None`` if ``ver_str`` cannot be coerced to a valid version.

    npm caret semantics:
    - Major > 0: ``^M.m.p`` → ``>=M.m.p,<(M+1).0.0``
    - Major == 0, Minor > 0: ``^0.m.p`` → ``>=0.m.p,<0.(m+1).0``
    - Major == 0, Minor == 0: ``^0.0.p`` → ``>=0.0.p,<0.0.(p+1)``
    """
    ver_str = ver_str.strip().lstrip("v")
    parts = ver_str.split(".")
    if len(parts) == 1:
        # ``^M``
        major = int(parts[0])
        lower = f">={major}.0.0"
        upper = f"<{major + 1}.0.0"
        return [lower, upper]
    if len(parts) == 2:
        # ``^M.m``
        major, minor = int(parts[0]), int(parts[1])
        if major > 0:
            lower = f">={major}.{minor}.0"
            upper = f"<{major + 1}.0.0"
        else:
            # ^0.m → >=0.m.0 <0.(m+1).0
            lower = f">=0.{minor}.0"
            upper = f"<0.{minor + 1}.0"
        return [lower, upper]
    if len(parts) >= 3:
        # ``^M.m.p``
        major, minor, patch_str = int(parts[0]), int(parts[1]), parts[2]
        # patch_str may contain pre-release info; take numeric prefix
        patch = int(re.match(r"^\d+", patch_str).group(0))  # type: ignore[union-attr]
        if major > 0:
            lower = f">={major}.{minor}.{patch}"
            upper = f"<{major + 1}.0.0"
        elif minor > 0:
            lower = f">=0.{minor}.{patch}"
            upper = f"<0.{minor + 1}.0"
        else:
            lower = f">=0.0.{patch}"
            upper = f"<0.0.{patch + 1}"
        return [lower, upper]
    return None


def _expand_tilde(ver_str: str) -> list[str] | None:
    """Expand a tilde range into a list of atomic comparison strings.

    npm tilde semantics:
    - ``~M.m.p`` → ``>=M.m.p,<M.(m+1).0``
    - ``~M.m``   → ``>=M.m.0,<M.(m+1).0``
    - ``~M``     → ``>=M.0.0,<(M+1).0.0``
    """
    ver_str = ver_str.strip().lstrip("v")
    parts = ver_str.split(".")
    if len(parts) == 1:
        major = int(parts[0])
        return [f">={major}.0.0", f"<{major + 1}.0.0"]
    if len(parts) == 2:
        major, minor = int(parts[0]), int(parts[1])
        return [f">={major}.{minor}.0", f"<{major}.{minor + 1}.0"]
    if len(parts) >= 3:
        major, minor, patch_str = int(parts[0]), int(parts[1]), parts[2]
        patch = int(re.match(r"^\d+", patch_str).group(0))  # type: ignore[union-attr]
        return [f">={major}.{minor}.{patch}", f"<{major}.{minor + 1}.0"]
    return None


# ---------------------------------------------------------------------------
# Clause parsing
# ---------------------------------------------------------------------------


def _parse_clause(clause: str) -> list[str] | None:
    """Parse a single clause into a list of atomic comparison strings.

    A clause is one of:
    - A caret range: ``^1.4``
    - A tilde range: ``~2.3.4``
    - An atomic comparison: ``>=2.0.0``
    - A bare version: ``1.2.3`` (treated as ``==1.2.3``)

    Returns ``None`` if the clause is malformed.
    """
    clause = clause.strip()
    if not clause:
        return None

    # Caret range
    if clause.startswith("^"):
        body = clause[1:].strip()
        try:
            return _expand_caret(body)
        except (ValueError, AttributeError):
            return None

    # Tilde range
    if clause.startswith("~"):
        body = clause[1:].strip()
        try:
            return _expand_tilde(body)
        except (ValueError, AttributeError):
            return None

    # Atomic comparison or bare version
    m = _SIMPLE_OP_RE.match(clause)
    if not m:
        return None
    op = m.group("op") or "=="
    ver = m.group("ver")
    if op not in _VALID_OPS:
        return None
    # Coerce to full semver string
    coerced = _coerce_version(ver)
    if coerced is None:
        return None
    # Validate the coerced version is parseable
    try:
        semver.Version.parse(coerced)
    except (ValueError, TypeError):
        return None
    return [f"{op}{coerced}"]


def _predicate_to_atomics(predicate: str) -> list[str] | None:
    """Convert a full predicate string to a flat list of atomic comparisons.

    Splits on commas (AND semantics) and expands caret/tilde tokens.
    Returns ``None`` if any clause fails to parse.
    Returns ``[]`` for an empty predicate (no-constraint).
    """
    predicate = predicate.strip()
    if not predicate:
        return []  # empty = no constraint

    clauses = [c.strip() for c in predicate.split(",")]
    atomics: list[str] = []
    for clause in clauses:
        if not clause:
            return None  # trailing/double comma = malformed
        result = _parse_clause(clause)
        if result is None:
            return None
        atomics.extend(result)
    return atomics


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_version_predicate(pred: str) -> bool:
    """Validate a semver range string (npm-style).

    Accepted forms: ``>=2.0,<3.0``; ``^1.4``; ``~2.3.4``; ``1.2.3``; ``""``
    (empty → no constraint, always valid).

    Returns ``True`` when the predicate is well-formed, ``False`` otherwise.
    This function never raises.

    Used at edge write-time; an invalid predicate should cause a 422.
    """
    try:
        result = _predicate_to_atomics(pred)
        return result is not None
    except Exception:
        _log.debug("version_predicate validation error", extra={"pred": pred}, exc_info=True)
        return False


def evaluate_version_predicate(version: str, predicate: str) -> bool:
    """Evaluate whether ``version`` satisfies ``predicate``.

    Returns ``True`` when the version satisfies every clause in the predicate.
    Returns ``False`` for an unsatisfied predicate or a malformed input.
    Never raises.

    An empty predicate (``""``) is treated as no constraint and always
    returns ``True``.

    Used at query-time; unsatisfied predicates set
    ``TraversalResult.version_satisfied[edge_id] = False`` but do not prune
    the traversal path.
    """
    try:
        # Parse and coerce the target version.
        coerced_ver = _coerce_version(version.strip())
        if coerced_ver is None:
            return False
        try:
            parsed_ver = semver.Version.parse(coerced_ver)
        except (ValueError, TypeError):
            return False

        # Parse the predicate into atomic comparisons.
        atomics = _predicate_to_atomics(predicate)
        if atomics is None:
            return False  # malformed predicate → False

        # Empty predicate = no constraint = always satisfied.
        if not atomics:
            return True

        # Every atomic clause must be satisfied (AND semantics).
        for atomic in atomics:
            try:
                if not parsed_ver.match(atomic):
                    return False
            except (ValueError, TypeError):
                return False

        return True
    except Exception:
        _log.debug(
            "version_predicate evaluation error",
            extra={"version": version, "pred": predicate},
            exc_info=True,
        )
        return False


__all__ = [
    "validate_version_predicate",
    "evaluate_version_predicate",
]
