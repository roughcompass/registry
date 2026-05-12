"""Built-in PII pattern: phone number (E.164 + US/EU formats).

Detection approach
------------------
Regex covers:

- E.164 international format: ``+<country_code><number>`` (7–15 digits).
- US NANP: ``(NXX) NXX-XXXX``, ``NXX-NXX-XXXX``, ``NXX.NXX.XXXX``,
  ``1-NXX-NXX-XXXX`` (with or without country code 1).
- EU (generic): ``+<2-digit CC><5-12 digits>`` optionally grouped with spaces
  or hyphens.

False-positive mitigation
--------------------------
- Require at least 7 digits after stripping separators.
- Use word-boundary anchors to avoid matching IDs like ``ORDER-12345678``.
- Exclude pure digit strings that look like dates or version numbers by
  requiring at least one separator in the non-E.164 paths.
"""

from __future__ import annotations

import re

from registry.types import PiiMatchResult

# E.164: + followed by 7..15 digits (no spaces within the number itself).
_E164_RE = re.compile(r"(?<!\d)\+\d{7,15}(?!\d)")

# NANP: optional leading 1-; then NXX-NXX-XXXX with dot/dash/space separators.
# First digit of each NXX block must be 2–9.
_NANP_RE = re.compile(
    r"""
    (?<!\d)
    (?:1[-.\s])?            # optional country code
    \(?[2-9]\d{2}\)?       # area code
    [-.\s]
    [2-9]\d{2}              # exchange
    [-.\s]
    \d{4}
    (?!\d)
    """,
    re.VERBOSE,
)

# EU / generic international: +CC (2 digits) then 5..12 digits, optionally
# grouped with spaces or hyphens.
_EU_RE = re.compile(
    r"""
    (?<!\d)
    \+[1-9]\d{1}            # country code (2 digits total, first non-zero)
    [\s\-]?
    (?:\d[\s\-]?){5,12}     # 5..12 more digits with optional separators
    \d                      # must end on a digit
    (?!\d)
    """,
    re.VERBOSE,
)

_PATTERNS = [_E164_RE, _NANP_RE, _EU_RE]


class _PhonePattern:
    name: str = "phone"
    category: str = "CONTACT"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all phone number matches in *text*.

        Never raises; returns ``[]`` on any internal error.
        Overlapping matches from different sub-patterns are deduplicated by
        offset so the caller never sees the same character span twice.
        """
        try:
            seen_offsets: set[int] = set()
            results: list[PiiMatchResult] = []
            for regex in _PATTERNS:
                for m in regex.finditer(text):
                    if m.start() not in seen_offsets:
                        seen_offsets.add(m.start())
                        results.append(
                            PiiMatchResult(
                                name=self.name,
                                offset=m.start(),
                                length=m.end() - m.start(),
                                category=self.category,
                            )
                        )
            results.sort(key=lambda r: r.offset)
            return results
        except Exception:  # noqa: BLE001
            return []


#: Module-level singleton.
pattern = _PhonePattern()
