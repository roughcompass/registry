"""PII scanner — pattern dispatch, layered policy resolution, always-on logging.

Policy resolution order (per-match, per-field)
-----------------------------------------------
1. Per-field override  — ``pii_field_policies`` where ``field_type`` matches
   AND either ``pattern_id`` is NULL (applies to all patterns for this field)
   OR ``pattern_id`` matches the pattern's DB row UUID.
   When both a pattern-specific and a NULL-pattern-id row exist, the
   pattern-specific row wins.
2. Per-pattern override — ``pii_patterns.policy_override`` (may be NULL).
3. Tenant default       — ``tenants.pii_policy`` (defaults to ``'advisory'``).

Action semantics
----------------
- ``advisory``: write proceeds; ``matched_patterns`` returned; no interruption.
- ``warn``    : write proceeds; ``matched_patterns`` + ``pii_warning`` envelope.
- ``block``   : caller raises 422; ``PiiScanResponse.action_taken == 'block'``.

Always-on logging
-----------------
``PiiScanner.scan()`` accepts an optional async ``log_sink`` callable.  The
scanner calls it with one ``pii_detection_log`` row dict per match.  If the
sink is not provided (unit tests, offline contexts) logging is silently skipped.
The sink MUST NOT be awaited inline — callers fire-and-forget via ``asyncio``
tasks or pass a synchronous recorder in test context.

Performance
-----------
Patterns compile their regex once at module load (never per call).  For inputs
larger than 64 KB the scanner processes 8 KB chunks with a 100-byte overlap
window to catch matches that span chunk boundaries.  Offsets reported in
``PiiMatchResult`` are always relative to the full input text.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from registry.types import PiiMatchResult, PiiScanResponse

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

_POLICY_SEVERITY: dict[str, int] = {
    "advisory": 0,
    "warn": 1,
    "block": 2,
}

_POLICY_VALUES = frozenset(_POLICY_SEVERITY)

# Chunk size for large-input streaming (bytes, treated as UTF-8 chars for simplicity).
_CHUNK_SIZE: int = 8 * 1024  # 8 KB
_CHUNK_OVERLAP: int = 100  # chars of overlap to catch cross-chunk matches


# ---------------------------------------------------------------------------
# Scanner Protocol (PiiPattern)
# ---------------------------------------------------------------------------


@runtime_checkable
class PiiPattern(Protocol):
    """Duck-type interface every pattern module implements.

    Both built-in singletons and ``RegexPattern`` tenant patterns satisfy this
    protocol automatically — no explicit base class registration required.
    """

    name: str
    category: str

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all non-overlapping matches.  Must never raise; returns ``[]`` on error."""
        ...


# ---------------------------------------------------------------------------
# Tenant custom pattern (regex-only, compiled on instantiation)
# ---------------------------------------------------------------------------


class RegexPattern:
    """Lightweight pattern wrapping a tenant-supplied regex string.

    Compiled once on construction.  Raises ``ValueError`` if the regex is
    syntactically invalid.  Callers should validate at insert time.
    """

    def __init__(
        self,
        name: str,
        category: str,
        regex: str,
        pattern_id: uuid.UUID | None = None,
    ) -> None:
        self.name = name
        self.category = category
        self.pattern_id = pattern_id
        self._re = re.compile(regex)

    def scan(self, text: str) -> list[PiiMatchResult]:
        try:
            return [
                PiiMatchResult(
                    name=self.name,
                    offset=m.start(),
                    length=m.end() - m.start(),
                    category=self.category,
                )
                for m in self._re.finditer(text)
            ]
        except Exception:  # noqa: BLE001
            return []


# ---------------------------------------------------------------------------
# Policy resolution helpers
# ---------------------------------------------------------------------------


def _resolve_policy(
    pattern_name: str,
    pattern_id: uuid.UUID | None,
    field_type: str,
    tenant_policy: str,
    pattern_overrides: dict[str, str],
    field_policies: dict[str, str],
) -> str:
    """Resolve the effective policy for one (pattern, field_type) pair.

    Parameters
    ----------
    pattern_name:
        Canonical pattern name (e.g. ``'email'``).
    pattern_id:
        DB UUID of the pattern row, or ``None`` for patterns not yet looked up.
    field_type:
        The field being scanned (e.g. ``'annotation.body'``).
    tenant_policy:
        Tenant-level default (level 3 — lowest precedence).
    pattern_overrides:
        Dict mapping ``pattern_name → policy``; sourced from
        ``pii_patterns.policy_override`` rows (level 2).
    field_policies:
        Dict mapping keys to policy strings (level 1 — highest precedence).

        Keys use one of two formats:
        - ``'<field_type>:<pattern_name>'`` — pattern-specific field override.
        - ``'<field_type>:*'``             — applies to ALL patterns for the field.

        When both forms match, the pattern-specific form wins.

    Returns
    -------
    str
        One of ``'advisory'``, ``'warn'``, ``'block'``.
    """
    # Level 1 — per-field, per-pattern (most specific wins).
    specific_key = f"{field_type}:{pattern_name}"
    wildcard_key = f"{field_type}:*"
    if specific_key in field_policies:
        policy = field_policies[specific_key]
        if policy in _POLICY_VALUES:
            return policy
    if wildcard_key in field_policies:
        policy = field_policies[wildcard_key]
        if policy in _POLICY_VALUES:
            return policy

    # Level 2 — per-pattern override.
    if pattern_name in pattern_overrides:
        policy = pattern_overrides[pattern_name]
        if policy in _POLICY_VALUES:
            return policy

    # Level 3 — tenant default.
    if tenant_policy in _POLICY_VALUES:
        return tenant_policy

    return "advisory"


def _max_policy(*policies: str) -> str:
    """Return the highest-severity policy from the given iterable."""
    if not policies:
        return "advisory"
    return max(policies, key=lambda p: _POLICY_SEVERITY.get(p, 0))


# ---------------------------------------------------------------------------
# PiiScanner
# ---------------------------------------------------------------------------


class PiiScanner:
    """Orchestrates pattern dispatch, policy resolution, and detection logging.

    Parameters
    ----------
    patterns:
        All active ``PiiPattern`` instances for this scanner (built-in + tenant).
    tenant_policy:
        Tenant-level default policy (``'advisory'`` if not configured).

    Usage
    -----
    ::

        scanner = PiiScanner(patterns=BUILT_IN_PATTERNS, tenant_policy="advisory")
        response = scanner.scan(
            text,
            field_type="annotation.body",
            tenant_policy="advisory",
            pattern_overrides={"aws_secret_key": "block"},
            field_policies={"annotation.body:email": "warn"},
        )
        if response.action_taken == "block":
            raise Http422(...)
    """

    def __init__(
        self,
        patterns: list[Any],
        tenant_policy: str = "advisory",
    ) -> None:
        self._patterns: list[Any] = list(patterns)
        self._default_tenant_policy: str = tenant_policy if tenant_policy in _POLICY_VALUES else "advisory"

    def scan(
        self,
        text: str,
        *,
        field_type: str,
        tenant_policy: str | None = None,
        pattern_overrides: dict[str, str] | None = None,
        field_policies: dict[str, str] | None = None,
        log_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> PiiScanResponse:
        """Run all enabled patterns and return the aggregated scan response.

        Parameters
        ----------
        text:
            Plaintext to scan.  For inputs > 64 KB this method chunks the text
            to avoid worst-case regex backtracking on a single huge string.
        field_type:
            Logical field being scanned (e.g. ``'workspace_entry.body'``).
        tenant_policy:
            Override the scanner's default tenant policy for this call.
        pattern_overrides:
            Per-pattern policy overrides (sourced from ``pii_patterns.policy_override``).
            Keys are pattern names.
        field_policies:
            Per-field policy overrides (sourced from ``pii_field_policies``).
            Keys have format ``'<field_type>:<pattern_name>'`` or ``'<field_type>:*'``.
        log_sink:
            Optional synchronous callable that receives one ``dict`` per match for
            persistence to ``pii_detection_log``.  Called inline; callers that need
            async persistence should wrap in a fire-and-forget task before passing.

        Returns
        -------
        PiiScanResponse
        """
        effective_tenant_policy: str = tenant_policy if tenant_policy in _POLICY_VALUES else self._default_tenant_policy
        effective_overrides: dict[str, str] = pattern_overrides or {}
        effective_field_policies: dict[str, str] = field_policies or {}

        all_matches: list[PiiMatchResult] = []
        match_policies: list[str] = []

        for pat in self._patterns:
            matches = self._scan_pattern(pat, text)
            if not matches:
                continue

            policy = _resolve_policy(
                pattern_name=pat.name,
                pattern_id=getattr(pat, "pattern_id", None),
                field_type=field_type,
                tenant_policy=effective_tenant_policy,
                pattern_overrides=effective_overrides,
                field_policies=effective_field_policies,
            )

            for match in matches:
                all_matches.append(match)
                match_policies.append(policy)
                if log_sink is not None:
                    self._emit_log(
                        sink=log_sink,
                        match=match,
                        policy=policy,
                        field_type=field_type,
                        pat=pat,
                    )

        action_taken: Literal["advisory", "warn", "block"] = (
            _max_policy(*match_policies) if match_policies else "advisory"  # type: ignore[assignment]
        )

        pii_warning: str | None = None
        if action_taken == "warn":
            pattern_names = sorted({m.name for m in all_matches})
            pii_warning = (
                f"PII detected in field '{field_type}': "
                + ", ".join(pattern_names)
                + ". Write proceeded but review is recommended."
            )

        return PiiScanResponse(
            matched_patterns=all_matches,
            action_taken=action_taken,
            pii_warning=pii_warning,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _scan_pattern(pat: Any, text: str) -> list[PiiMatchResult]:
        """Scan *text* with *pat*, chunking if text exceeds 64 KB.

        Offsets in returned ``PiiMatchResult`` objects are always relative to
        the start of the full input text.
        """
        if len(text) <= _CHUNK_SIZE:
            try:
                return pat.scan(text)
            except Exception:  # noqa: BLE001
                return []

        # Chunked path: 8 KB chunks with 100-char overlap.
        results: list[PiiMatchResult] = []
        seen_spans: set[tuple[int, int]] = set()
        pos = 0
        text_len = len(text)

        while pos < text_len:
            chunk_end = min(pos + _CHUNK_SIZE + _CHUNK_OVERLAP, text_len)
            chunk = text[pos:chunk_end]
            try:
                chunk_matches = pat.scan(chunk)
            except Exception:  # noqa: BLE001
                chunk_matches = []

            for m in chunk_matches:
                abs_offset = pos + m.offset
                span = (abs_offset, m.length)
                if span not in seen_spans:
                    seen_spans.add(span)
                    results.append(
                        PiiMatchResult(
                            name=m.name,
                            offset=abs_offset,
                            length=m.length,
                            category=m.category,
                        )
                    )

            # Advance by chunk size (without overlap) so overlap region is
            # re-scanned in the next chunk to catch cross-boundary matches.
            pos += _CHUNK_SIZE

        return results

    @staticmethod
    def _emit_log(
        sink: Callable[[dict[str, Any]], None],
        match: PiiMatchResult,
        policy: str,
        field_type: str,
        pat: Any,
    ) -> None:
        """Call *sink* with a ``pii_detection_log`` row dict.

        ``target_type`` is set to *field_type*; ``target_id`` is ``None``
        (pre-write detection).  Callers responsible for back-filling once the
        row is persisted.
        """
        try:
            sink(
                {
                    "target_type": field_type,
                    "target_id": None,
                    "pattern_id": getattr(pat, "pattern_id", None),
                    "pattern_name": match.name,
                    "category": match.category,
                    "match_offset": match.offset,
                    "match_length": match.length,
                    "action_taken": policy,
                }
            )
        except Exception:  # noqa: BLE001
            # Logging MUST NOT interrupt the scan response.
            pass


# ---------------------------------------------------------------------------
# Factory — build a scanner from built-in patterns only (no DB required)
# ---------------------------------------------------------------------------


def build_builtin_scanner(tenant_policy: str = "advisory") -> PiiScanner:
    """Return a ``PiiScanner`` loaded with all built-in pattern singletons.

    Suitable for use in tests, CLI tooling, and any context where the DB is
    not available.  Tenant custom patterns are NOT included.
    """
    from registry.security.pii_patterns import BUILT_IN_PATTERNS  # noqa: PLC0415

    return PiiScanner(patterns=BUILT_IN_PATTERNS, tenant_policy=tenant_policy)
