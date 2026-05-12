"""Performance test — PII scanner p95 < 50 ms for 64 KB input.

SLO
------------------------
Write-time PII scanner (all built-in patterns) on 64 KB input must complete
with p95 latency < 50 ms.

Setup
-----
- 64 KB text generated once: mix of clean prose + PII fragments spread throughout.
- ``build_builtin_scanner()`` used (no DB required for this test).
- 20 warm-up calls discarded; 50 timed samples recorded.
- Threshold: 95th percentile of the 50 timed samples must be < 50 ms.

Marks
-----
``@pytest.mark.perf`` + ``@pytest.mark.slow`` — may be excluded from unit-only CI.
This test does NOT require Postgres (no pg_container needed); it runs against
the compiled regex patterns only.

To run: ``pytest tests/perf/test_perf_pii_scanner.py -m perf -v``.

Note: pattern regexes are compiled once at module import, not per call.  The
measured time covers dispatch + match + policy resolution + response assembly.
"""

from __future__ import annotations

import random
import statistics
import time

import pytest

from registry.security.pii_scanner import build_builtin_scanner

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TARGET_INPUT_BYTES = 64 * 1024  # 64 KB
_WARM_UP_CALLS = 20
_TIMED_CALLS = 50
_P95_TARGET_MS = 50.0

# Seed for reproducible synthetic text.
_RNG_SEED = 42


# ---------------------------------------------------------------------------
# Synthetic 64 KB input fixture
# ---------------------------------------------------------------------------


def _build_64kb_input() -> str:
    """Generate a ~64 KB text string that includes scattered PII fragments.

    The text is a mix of:
    - Random alphanumeric prose (95% of content — clean background noise).
    - A few PII fragments injected at deterministic positions (ensures the
      scanner must actually evaluate the full input, not short-circuit).

    The PII fragments are Luhn-valid / syntactically correct but are distributed
    throughout the text so all chunk windows are exercised.
    """
    rng = random.Random(_RNG_SEED)

    # Build clean prose blocks.
    words = [
        "the",
        "quick",
        "brown",
        "fox",
        "jumps",
        "over",
        "lazy",
        "dog",
        "capability",
        "fabric",
        "edge",
        "graph",
        "traversal",
        "version",
        "predicate",
        "closure",
        "cache",
        "scanner",
        "tenant",
        "policy",
    ]

    def _random_prose(n_chars: int) -> str:
        parts = []
        count = 0
        while count < n_chars:
            w = rng.choice(words)
            parts.append(w)
            count += len(w) + 1
        return " ".join(parts)[:n_chars]

    # Target: 64 KB with PII injected at 3 positions.
    total = _TARGET_INPUT_BYTES
    chunk1 = total // 4
    chunk2 = total // 4
    chunk3 = total // 4
    chunk4 = total - chunk1 - chunk2 - chunk3

    pii_fragments = [
        "Contact billing: alice@example.com for invoice details.",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE assigned to pipeline.",
        "Card number on file: 4111111111111111 (Visa).",
    ]

    text = (
        _random_prose(chunk1)
        + "\n"
        + pii_fragments[0]
        + "\n"
        + _random_prose(chunk2)
        + "\n"
        + pii_fragments[1]
        + "\n"
        + _random_prose(chunk3)
        + "\n"
        + pii_fragments[2]
        + "\n"
        + _random_prose(chunk4)
    )

    # Pad / trim to exactly 64 KB.
    if len(text) < total:
        text += _random_prose(total - len(text))
    return text[:total]


# Build the fixture once at module import to remove allocation time from measurements.
_INPUT_64KB: str = _build_64kb_input()


# ---------------------------------------------------------------------------
# Performance test (no Postgres required)
# ---------------------------------------------------------------------------


@pytest.mark.perf
@pytest.mark.slow
def test_pii_scanner_p95_under_50ms_for_64kb_input() -> None:
    """PII scanner p95 must be < 50 ms for 64 KB input.

    Methodology:
    - build_builtin_scanner() called once (patterns compiled at module load).
    - 20 warm-up calls (not measured) to ensure JIT-compiled regex paths.
    - 50 timed calls; 95th percentile must be < 50 ms.
    """
    scanner = build_builtin_scanner(tenant_policy="advisory")

    assert (
        len(_INPUT_64KB) == _TARGET_INPUT_BYTES
    ), f"Input must be exactly {_TARGET_INPUT_BYTES} bytes, got {len(_INPUT_64KB)}"

    # Warm-up.
    for _ in range(_WARM_UP_CALLS):
        scanner.scan(
            _INPUT_64KB,
            field_type="perf.test",
            pattern_overrides={},
            field_policies={},
        )

    # Timed calls.
    latencies_ms: list[float] = []
    for _ in range(_TIMED_CALLS):
        t0 = time.perf_counter()
        resp = scanner.scan(
            _INPUT_64KB,
            field_type="perf.test",
            pattern_overrides={},
            field_policies={},
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed_ms)

    # Sanity: scanner must find the injected PII fragments.
    assert resp.matched_patterns, (
        "Scanner returned no matches on the 64 KB input; " "PII fragments may have been altered during build."
    )

    p95_ms = statistics.quantiles(latencies_ms, n=20)[18]
    mean_ms = statistics.mean(latencies_ms)
    max_ms = max(latencies_ms)

    print(
        f"\nPII scanner 64 KB latency ({_TIMED_CALLS} calls): "
        f"mean={mean_ms:.2f}ms p95={p95_ms:.2f}ms max={max_ms:.2f}ms "
        f"matches={len(resp.matched_patterns)}"
    )

    assert p95_ms < _P95_TARGET_MS, (
        f"PII scanner p95 ({p95_ms:.2f} ms) exceeds SLO of {_P95_TARGET_MS} ms. "
        f"mean={mean_ms:.2f}ms max={max_ms:.2f}ms"
    )
