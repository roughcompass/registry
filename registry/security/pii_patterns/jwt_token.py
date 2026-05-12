"""Built-in PII pattern: JSON Web Token (JWT).

Detection approach
------------------
A JWT consists of three base64url-encoded segments separated by literal dots:
``<header>.<payload>.<signature>``.

Constraints applied:
- Each segment must contain only base64url characters (``[A-Za-z0-9_-]``) and
  optional ``=`` padding.
- Minimum total length of 20 characters (excludes trivial false positives like
  ``a.b.c``).
- Minimum segment lengths: header ≥ 10, payload ≥ 10, signature ≥ 10.  This
  eliminates short dotted identifiers and file-extension triplets.
- Word-boundary anchors prevent matching within longer strings.

The pattern does *not* validate the JWT structure (algorithm, expiry, etc.) —
detection only, no decoding.
"""

from __future__ import annotations

import re

from registry.types import PiiMatchResult

# Three base64url segments separated by dots.  Minimum lengths enforced via
# character-class quantifiers: {10,} for each segment.
_JWT_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_\-.])    # not preceded by alphanum/underscore/dash/dot
    [A-Za-z0-9_\-=]{10,}    # header (min 10 chars)
    \.
    [A-Za-z0-9_\-=]{10,}    # payload (min 10 chars)
    \.
    [A-Za-z0-9_\-=]{10,}    # signature (min 10 chars)
    (?![A-Za-z0-9_\-.])     # not followed by alphanum/underscore/dash/dot
    """,
    re.VERBOSE,
)


class _JwtPattern:
    name: str = "jwt_token"
    category: str = "CREDENTIALS"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all JWT-like token matches in *text*.

        Never raises; returns ``[]`` on any internal error.
        """
        try:
            results: list[PiiMatchResult] = []
            for m in _JWT_RE.finditer(text):
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
pattern = _JwtPattern()
