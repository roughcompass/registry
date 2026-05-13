"""Unit tests for PiiScanner.

Coverage
--------
- Policy resolution order: per-field > per-pattern > tenant default.
- Max-severity aggregation across multiple patterns.
- Action semantics: advisory / warn / block.
- No-match → advisory response; empty matched_patterns.
- Always-on log: log_sink called for every match regardless of policy.
- Log sink failure does NOT propagate to caller.
- pii_warning populated iff action_taken == 'warn'.
- Large-input chunking: matches detected across chunk boundary.
- RegexPattern tenant custom pattern behaves correctly.
- build_builtin_scanner factory returns a functional scanner.
- Performance: 64 KB random text scanned in < 100 ms.

No I/O, no network, no database required.

PII detection uses Luhn, entropy, and range checks to minimise false positives.
Field policies and tenant defaults override pattern-level defaults.
"""

from __future__ import annotations

import re
import string
import time
import uuid

import pytest

from registry.security.pii_scanner import (
    PiiScanner,
    RegexPattern,
    _max_policy,
    _resolve_policy,
    build_builtin_scanner,
)
from registry.types import PiiScanResponse

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_EMAIL_TEXT = "Contact alice@example.com for help."
_CC_TEXT = "Card number: 4111111111111111"  # Visa test; passes Luhn.
_CLEAN_TEXT = "No PII here at all."
_FIELD = "annotation.body"


def _make_pattern(name: str = "test_pattern", category: str = "TEST") -> RegexPattern:
    """Return a simple word-matching RegexPattern for testing."""
    return RegexPattern(name=name, category=category, regex=r"\bSECRET\b")


def _make_scanner(*patterns: object, tenant_policy: str = "advisory") -> PiiScanner:
    return PiiScanner(patterns=list(patterns), tenant_policy=tenant_policy)


# ---------------------------------------------------------------------------
# _resolve_policy unit tests
# ---------------------------------------------------------------------------


class TestResolvePolicy:
    def test_tenant_default_used_when_no_overrides(self) -> None:
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type=_FIELD,
            tenant_policy="warn",
            pattern_overrides={},
            field_policies={},
        )
        assert policy == "warn"

    def test_per_pattern_override_beats_tenant_default(self) -> None:
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type=_FIELD,
            tenant_policy="advisory",
            pattern_overrides={"email": "block"},
            field_policies={},
        )
        assert policy == "block"

    def test_per_field_specific_beats_per_pattern(self) -> None:
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type=_FIELD,
            tenant_policy="advisory",
            pattern_overrides={"email": "block"},
            field_policies={f"{_FIELD}:email": "warn"},
        )
        assert policy == "warn"

    def test_per_field_wildcard_beats_per_pattern(self) -> None:
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type=_FIELD,
            tenant_policy="advisory",
            pattern_overrides={"email": "block"},
            field_policies={f"{_FIELD}:*": "warn"},
        )
        assert policy == "warn"

    def test_per_field_specific_beats_wildcard(self) -> None:
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type=_FIELD,
            tenant_policy="advisory",
            pattern_overrides={},
            field_policies={
                f"{_FIELD}:email": "block",  # specific
                f"{_FIELD}:*": "warn",  # wildcard — should lose
            },
        )
        assert policy == "block"

    def test_invalid_policy_value_falls_through(self) -> None:
        # A corrupted DB value should fall through to the next level.
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type=_FIELD,
            tenant_policy="advisory",
            pattern_overrides={"email": "INVALID_LEVEL"},
            field_policies={},
        )
        assert policy == "advisory"

    def test_unknown_field_type_falls_through_to_tenant(self) -> None:
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type="unknown.field",
            tenant_policy="warn",
            pattern_overrides={},
            field_policies={},
        )
        assert policy == "warn"

    def test_advisory_default_when_tenant_policy_invalid(self) -> None:
        policy = _resolve_policy(
            pattern_name="email",
            pattern_id=None,
            field_type=_FIELD,
            tenant_policy="garbage",
            pattern_overrides={},
            field_policies={},
        )
        assert policy == "advisory"


# ---------------------------------------------------------------------------
# _max_policy unit tests
# ---------------------------------------------------------------------------


class TestMaxPolicy:
    def test_single_advisory(self) -> None:
        assert _max_policy("advisory") == "advisory"

    def test_single_warn(self) -> None:
        assert _max_policy("warn") == "warn"

    def test_advisory_and_warn_returns_warn(self) -> None:
        assert _max_policy("advisory", "warn") == "warn"

    def test_advisory_and_block_returns_block(self) -> None:
        assert _max_policy("advisory", "block") == "block"

    def test_warn_and_block_returns_block(self) -> None:
        assert _max_policy("warn", "block") == "block"

    def test_all_same(self) -> None:
        assert _max_policy("warn", "warn", "warn") == "warn"

    def test_empty_returns_advisory(self) -> None:
        assert _max_policy() == "advisory"


# ---------------------------------------------------------------------------
# PiiScanner.scan() — no-match path
# ---------------------------------------------------------------------------


class TestScanNoMatch:
    def test_clean_text_returns_advisory(self) -> None:
        scanner = _make_scanner(_make_pattern())
        resp = scanner.scan(_CLEAN_TEXT, field_type=_FIELD)
        assert isinstance(resp, PiiScanResponse)
        assert resp.action_taken == "advisory"
        assert resp.matched_patterns == []
        assert resp.pii_warning is None

    def test_empty_text_returns_advisory(self) -> None:
        scanner = _make_scanner(_make_pattern())
        resp = scanner.scan("", field_type=_FIELD)
        assert resp.action_taken == "advisory"
        assert resp.matched_patterns == []


# ---------------------------------------------------------------------------
# Action semantics
# ---------------------------------------------------------------------------


class TestActionSemantics:
    def _secret_text(self) -> str:
        return "value = SECRET"

    def test_advisory_action_on_match(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(self._secret_text(), field_type=_FIELD)
        assert resp.action_taken == "advisory"
        assert len(resp.matched_patterns) == 1
        assert resp.pii_warning is None

    def test_warn_action_populates_pii_warning(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(
            self._secret_text(),
            field_type=_FIELD,
            pattern_overrides={"test_pattern": "warn"},
        )
        assert resp.action_taken == "warn"
        assert isinstance(resp.pii_warning, str)
        assert "test_pattern" in resp.pii_warning
        assert _FIELD in resp.pii_warning

    def test_block_action_no_pii_warning(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="block")
        resp = scanner.scan(self._secret_text(), field_type=_FIELD)
        assert resp.action_taken == "block"
        assert resp.pii_warning is None

    def test_advisory_and_warn_pattern_produces_warn(self) -> None:
        pat1 = RegexPattern("pat_advisory", "CAT", r"\bFOO\b")
        pat2 = RegexPattern("pat_warn", "CAT", r"\bBAR\b")
        scanner = _make_scanner(pat1, pat2, tenant_policy="advisory")
        resp = scanner.scan(
            "FOO BAR",
            field_type=_FIELD,
            pattern_overrides={"pat_warn": "warn"},
        )
        assert resp.action_taken == "warn"
        assert len(resp.matched_patterns) == 2

    def test_block_wins_over_warn(self) -> None:
        pat_warn = RegexPattern("pat_warn", "CAT", r"\bFOO\b")
        pat_block = RegexPattern("pat_block", "CAT", r"\bBAR\b")
        scanner = _make_scanner(pat_warn, pat_block, tenant_policy="advisory")
        resp = scanner.scan(
            "FOO BAR",
            field_type=_FIELD,
            pattern_overrides={"pat_warn": "warn", "pat_block": "block"},
        )
        assert resp.action_taken == "block"

    def test_block_fires_even_with_single_match(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(
            self._secret_text(),
            field_type=_FIELD,
            field_policies={f"{_FIELD}:test_pattern": "block"},
        )
        assert resp.action_taken == "block"
        assert len(resp.matched_patterns) == 1


# ---------------------------------------------------------------------------
# Policy resolution levels in scan()
# ---------------------------------------------------------------------------


class TestPolicyScanIntegration:
    def test_tenant_default_advisory_propagates(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan("SECRET", field_type=_FIELD)
        assert resp.action_taken == "advisory"

    def test_tenant_default_block_propagates(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="block")
        resp = scanner.scan("SECRET", field_type=_FIELD)
        assert resp.action_taken == "block"

    def test_per_pattern_override_overrides_tenant(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(
            "SECRET",
            field_type=_FIELD,
            pattern_overrides={"test_pattern": "block"},
        )
        assert resp.action_taken == "block"

    def test_per_field_specific_overrides_per_pattern(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(
            "SECRET",
            field_type=_FIELD,
            pattern_overrides={"test_pattern": "block"},
            field_policies={f"{_FIELD}:test_pattern": "warn"},
        )
        # warn < block; per-field specific should win (warn), not per-pattern (block).
        assert resp.action_taken == "warn"

    def test_per_field_wildcard_applies_to_all_patterns(self) -> None:
        pat1 = RegexPattern("pat_a", "CAT", r"\bAAA\b")
        pat2 = RegexPattern("pat_b", "CAT", r"\bBBB\b")
        scanner = _make_scanner(pat1, pat2, tenant_policy="advisory")
        resp = scanner.scan(
            "AAA BBB",
            field_type=_FIELD,
            field_policies={f"{_FIELD}:*": "warn"},
        )
        assert resp.action_taken == "warn"
        assert len(resp.matched_patterns) == 2

    def test_scanner_default_policy_used_as_fallback(self) -> None:
        # Scanner constructed with warn default; scan() gets no tenant_policy.
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="warn")
        resp = scanner.scan("SECRET", field_type=_FIELD)
        assert resp.action_taken == "warn"

    def test_scan_tenant_policy_overrides_scanner_default(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="block")
        resp = scanner.scan("SECRET", field_type=_FIELD, tenant_policy="advisory")
        assert resp.action_taken == "advisory"


# ---------------------------------------------------------------------------
# Always-on log
# ---------------------------------------------------------------------------


class TestAlwaysOnLog:
    def test_log_sink_called_for_advisory_match(self) -> None:
        logged: list[dict] = []
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        scanner.scan("SECRET", field_type=_FIELD, log_sink=logged.append)
        assert len(logged) == 1
        row = logged[0]
        assert row["pattern_name"] == "test_pattern"
        assert row["action_taken"] == "advisory"

    def test_log_sink_called_for_block_match(self) -> None:
        logged: list[dict] = []
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="block")
        scanner.scan("SECRET", field_type=_FIELD, log_sink=logged.append)
        assert len(logged) == 1
        assert logged[0]["action_taken"] == "block"

    def test_log_sink_called_for_warn_match(self) -> None:
        logged: list[dict] = []
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="warn")
        scanner.scan("SECRET", field_type=_FIELD, log_sink=logged.append)
        assert len(logged) == 1
        assert logged[0]["action_taken"] == "warn"

    def test_log_sink_not_called_on_no_match(self) -> None:
        logged: list[dict] = []
        pat = _make_pattern()
        scanner = _make_scanner(pat)
        scanner.scan(_CLEAN_TEXT, field_type=_FIELD, log_sink=logged.append)
        assert logged == []

    def test_log_sink_called_once_per_match(self) -> None:
        """Two matches in one text → two log rows."""
        pat = RegexPattern("rep_pattern", "TEST", r"\bSECRET\b")
        scanner = _make_scanner(pat, tenant_policy="advisory")
        logged: list[dict] = []
        scanner.scan("SECRET and SECRET again", field_type=_FIELD, log_sink=logged.append)
        assert len(logged) == 2

    def test_log_sink_called_for_all_patterns(self) -> None:
        """Multiple patterns each producing matches each log independently."""
        pat_a = RegexPattern("pat_a", "CAT", r"\bAAA\b")
        pat_b = RegexPattern("pat_b", "CAT", r"\bBBB\b")
        scanner = _make_scanner(pat_a, pat_b, tenant_policy="advisory")
        logged: list[dict] = []
        scanner.scan("AAA BBB", field_type=_FIELD, log_sink=logged.append)
        assert len(logged) == 2
        names = {r["pattern_name"] for r in logged}
        assert names == {"pat_a", "pat_b"}

    def test_log_sink_failure_does_not_propagate(self) -> None:
        def bad_sink(_row: dict) -> None:
            raise RuntimeError("sink exploded")

        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        # Must NOT raise; response must be correct.
        resp = scanner.scan("SECRET", field_type=_FIELD, log_sink=bad_sink)
        assert resp.action_taken == "advisory"
        assert len(resp.matched_patterns) == 1

    def test_log_row_has_required_fields(self) -> None:
        logged: list[dict] = []
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="warn")
        scanner.scan("SECRET", field_type=_FIELD, log_sink=logged.append)
        row = logged[0]
        for key in (
            "target_type",
            "target_id",
            "pattern_id",
            "pattern_name",
            "category",
            "match_offset",
            "match_length",
            "action_taken",
        ):
            assert key in row, f"Missing key: {key}"

    def test_log_row_target_id_is_none_pre_write(self) -> None:
        logged: list[dict] = []
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        scanner.scan("SECRET", field_type=_FIELD, log_sink=logged.append)
        assert logged[0]["target_id"] is None

    def test_log_row_target_type_is_field_type(self) -> None:
        logged: list[dict] = []
        pat = _make_pattern()
        scanner = _make_scanner(pat)
        scanner.scan("SECRET", field_type="workspace_entry.body", log_sink=logged.append)
        assert logged[0]["target_type"] == "workspace_entry.body"

    def test_no_log_sink_no_error(self) -> None:
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        # Passing no log_sink must not raise.
        resp = scanner.scan("SECRET", field_type=_FIELD)
        assert resp.action_taken == "advisory"


# ---------------------------------------------------------------------------
# RegexPattern custom pattern
# ---------------------------------------------------------------------------


class TestRegexPattern:
    def test_basic_match(self) -> None:
        pat = RegexPattern("my_pat", "CUSTOM", r"\b\d{4}-\d{4}\b")
        results = pat.scan("order 1234-5678 placed")
        assert len(results) == 1
        assert results[0].name == "my_pat"
        assert results[0].category == "CUSTOM"

    def test_no_match_returns_empty(self) -> None:
        pat = RegexPattern("my_pat", "CUSTOM", r"\bSECRET\b")
        assert pat.scan("nothing here") == []

    def test_never_raises_on_empty(self) -> None:
        pat = RegexPattern("my_pat", "CUSTOM", r"\bSECRET\b")
        assert pat.scan("") == []

    def test_invalid_regex_raises_on_init(self) -> None:
        with pytest.raises(re.error):
            RegexPattern("bad", "CAT", r"[unclosed")

    def test_pattern_id_attached(self) -> None:
        pid = uuid.uuid4()
        pat = RegexPattern("pid_pat", "CAT", r"\bSECRET\b", pattern_id=pid)
        assert pat.pattern_id == pid

    def test_pattern_id_propagates_to_log(self) -> None:
        pid = uuid.uuid4()
        pat = RegexPattern("pid_pat", "CAT", r"\bSECRET\b", pattern_id=pid)
        scanner = _make_scanner(pat, tenant_policy="advisory")
        logged: list[dict] = []
        scanner.scan("SECRET", field_type=_FIELD, log_sink=logged.append)
        assert logged[0]["pattern_id"] == pid


# ---------------------------------------------------------------------------
# Chunked large-input path
# ---------------------------------------------------------------------------


class TestLargeInputChunking:
    def test_match_detected_in_large_text(self) -> None:
        # 10 KB of padding + target + more padding → well above 8 KB chunk size.
        padding = "x" * 10_000
        text = padding + " SECRET " + padding
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(text, field_type=_FIELD)
        assert len(resp.matched_patterns) == 1

    def test_match_at_chunk_boundary(self) -> None:
        # Place " SECRET " so the space before it lands exactly at the chunk
        # boundary — the word + trailing space cross into the next chunk.
        # The pattern uses \bSECRET\b which needs a non-word char on both sides.
        from registry.security.pii_scanner import _CHUNK_SIZE

        pre = "a" * (_CHUNK_SIZE - 1)  # 1 char before boundary for the space
        text = pre + " SECRET " + "b" * 100
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(text, field_type=_FIELD)
        assert len(resp.matched_patterns) >= 1

    def test_no_duplicate_matches_in_overlap_region(self) -> None:
        from registry.security.pii_scanner import _CHUNK_SIZE

        # Place SECRET well within the overlap region.
        pre = "a" * (_CHUNK_SIZE - 10)
        text = pre + " SECRET " + "b" * 200
        pat = _make_pattern()
        scanner = _make_scanner(pat, tenant_policy="advisory")
        resp = scanner.scan(text, field_type=_FIELD)
        # Despite overlap, deduplication must keep exactly one match.
        assert len(resp.matched_patterns) == 1


# ---------------------------------------------------------------------------
# build_builtin_scanner factory
# ---------------------------------------------------------------------------


class TestBuildBuiltinScanner:
    def test_factory_returns_scanner_instance(self) -> None:
        scanner = build_builtin_scanner()
        assert isinstance(scanner, PiiScanner)

    def test_factory_detects_email(self) -> None:
        scanner = build_builtin_scanner(tenant_policy="advisory")
        resp = scanner.scan(_EMAIL_TEXT, field_type=_FIELD)
        assert any(m.name == "email" for m in resp.matched_patterns)

    def test_factory_detects_credit_card(self) -> None:
        scanner = build_builtin_scanner(tenant_policy="advisory")
        resp = scanner.scan(_CC_TEXT, field_type=_FIELD)
        assert any(m.name == "credit_card" for m in resp.matched_patterns)

    def test_factory_clean_text_is_advisory(self) -> None:
        scanner = build_builtin_scanner()
        resp = scanner.scan(_CLEAN_TEXT, field_type=_FIELD)
        assert resp.action_taken == "advisory"
        assert resp.matched_patterns == []

    def test_factory_custom_tenant_policy(self) -> None:
        scanner = build_builtin_scanner(tenant_policy="block")
        resp = scanner.scan(_EMAIL_TEXT, field_type=_FIELD)
        # Email detected + block policy → action_taken == block.
        assert resp.action_taken == "block"


# ---------------------------------------------------------------------------
# PiiScanResponse dataclass
# ---------------------------------------------------------------------------


class TestPiiScanResponse:
    def test_response_shape(self) -> None:
        from registry.types import PiiScanResponse

        resp = PiiScanResponse(matched_patterns=[], action_taken="advisory")
        assert resp.matched_patterns == []
        assert resp.action_taken == "advisory"
        assert resp.pii_warning is None

    def test_response_with_warning(self) -> None:
        from registry.types import PiiScanResponse

        resp = PiiScanResponse(
            matched_patterns=[],
            action_taken="warn",
            pii_warning="Some PII detected.",
        )
        assert resp.pii_warning == "Some PII detected."


# ---------------------------------------------------------------------------
# Performance assertion
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_scan_64kb_under_100ms(self) -> None:
        """64 KB of random-ish text should complete in < 100 ms (unit-test budget)."""
        # Build 64 KB of content unlikely to produce many false positives.
        chunk = (string.ascii_lowercase + " \n") * 100
        text = (chunk * 30)[:65_536]  # exactly 64 KB

        scanner = build_builtin_scanner()
        start = time.perf_counter()
        scanner.scan(text, field_type=_FIELD)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 100, f"scan took {elapsed_ms:.1f} ms (limit 100 ms)"


# ---------------------------------------------------------------------------
# Chunk-boundary dedup — loose-regex suffix false-positive
# ---------------------------------------------------------------------------


def test_pii_scanner_does_not_double_report_cross_chunk_token() -> None:
    """A token straddling a chunk boundary must be reported once, not twice.

    With an exact-(offset, length) dedup, a loose regex like ``\\d+`` could
    match the full token in chunk N and then match just its suffix in
    chunk N+1 at a different (offset, length) — the keys differ and the
    suffix slips through as a spurious second finding. The fix dedups by
    span overlap, suppressing any subsequent match whose range intersects
    an already-accepted one.
    """
    pattern = RegexPattern(name="digits", category="TEST", regex=r"\d+")
    scanner = PiiScanner([pattern], "advisory")

    # 8185 'A's + 10 digits + 5 'B's = 8200 chars total. The digit run
    # straddles the 8192-byte chunk boundary.
    text = "A" * 8185 + "1234567890" + "B" * 5
    response = scanner.scan(text, field_type=_FIELD)

    assert len(response.matched_patterns) == 1, (
        f"expected exactly one match for the digit run; got "
        f"{[(m.offset, m.length) for m in response.matched_patterns]}"
    )
    only = response.matched_patterns[0]
    assert only.offset == 8185, only.offset
    assert only.length == 10, only.length
