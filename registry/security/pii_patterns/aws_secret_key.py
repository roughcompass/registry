"""Built-in PII pattern: AWS Secret Access Key (entropy-based).

Detection approach
------------------
AWS Secret Access Keys are 40-character base64url strings (``[A-Za-z0-9+/]``).
There is no canonical prefix that uniquely identifies them, so pure-regex
detection has an unacceptably high false-positive rate on any long base64 blob.

This module uses a sliding-window Shannon entropy scan:

1. Find all candidate substrings that match the character class for a
   potential AWS secret key (length exactly 40, base64url-ish alphabet).
2. Compute the Shannon entropy of each candidate.
3. Accept candidates whose entropy exceeds *4.5 bits/character* — a threshold
   empirically chosen to reject English prose and low-entropy padding strings
   while catching the high-entropy random keys that AWS generates.

Performance
-----------
The regex locates candidate windows in O(n); entropy computation on each 40-char
window is O(1).  The scanner comfortably meets the p95 < 50 ms budget for 64 KB
input.

DB row note
-----------
The ``pii_patterns`` row for this detector carries ``regex='__entropy__'`` and
``detector_module='catalog.security.pii_patterns.aws_secret_key'``.  This sentinel
tells the dispatcher that the Python module is the authoritative implementation.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from registry.types import PiiMatchResult

#: Minimum Shannon entropy (bits/char) to flag a candidate as a secret key.
_ENTROPY_THRESHOLD: float = 4.5

#: Exact length of an AWS Secret Access Key.
_KEY_LENGTH: int = 40

# Candidate windows: 40 consecutive base64url characters.
# The (?<!\w) / (?!\w) anchors prevent grabbing mid-word slices of longer
# base64 blobs, though for AWS keys the length constraint is already tight.
_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{40})(?![A-Za-z0-9+/=])")


def _shannon_entropy(s: str) -> float:
    """Return the Shannon entropy of *s* in bits per character."""
    if not s:
        return 0.0
    freq = Counter(s)
    total = len(s)
    return -sum((c / total) * math.log2(c / total) for c in freq.values())


class _AwsSecretKeyPattern:
    name: str = "aws_secret_key"
    category: str = "CREDENTIALS"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all high-entropy 40-char base64 candidates in *text*.

        Never raises; returns ``[]`` on any internal error.
        """
        try:
            results: list[PiiMatchResult] = []
            for m in _CANDIDATE_RE.finditer(text):
                candidate = m.group(1)
                if len(candidate) == _KEY_LENGTH and _shannon_entropy(candidate) > _ENTROPY_THRESHOLD:
                    results.append(
                        PiiMatchResult(
                            name=self.name,
                            offset=m.start(),
                            length=m.end() - m.start(),
                            category=self.category,
                        )
                    )
            return results
        except Exception:  # noqa: BLE001
            return []


#: Module-level singleton.
pattern = _AwsSecretKeyPattern()
