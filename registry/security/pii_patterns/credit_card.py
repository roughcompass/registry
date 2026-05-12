"""Built-in PII pattern: credit/debit card numbers (Luhn-checked).

Detection approach
------------------
Two-stage detection:

1. **Regex** — matches candidate digit strings (with optional spaces or dashes
   as separators) that match the length and prefix rules for major card brands:

   | Brand | Prefix(es) | Length |
   |-------|-----------|--------|
   | Visa  | 4 | 13 or 16 |
   | Mastercard | 51–55 or 2221–2720 | 16 |
   | Amex | 34 or 37 | 15 |
   | Discover | 6011, 622126–622925, 644–649, 65 | 16 |

2. **Luhn check** — all regex candidates are passed through the Luhn algorithm.
   Candidates that fail the check are rejected.  This dramatically reduces
   false positives (version strings, phone numbers, order IDs, etc.).

Separator handling
------------------
Groups of 4 digits separated by a single space or dash are detected as a unit
(e.g. ``4111 1111 1111 1111``).  The separator characters are included in the
``length`` reported so the caller can reconstruct the exact match span.

References
----------
- ISO/IEC 7812 (card numbering)
- https://en.wikipedia.org/wiki/Luhn_algorithm
"""

from __future__ import annotations

import re

from registry.types import PiiMatchResult

# ---------------------------------------------------------------------------
# Candidate regex — major card brand patterns.
# Each sub-pattern captures digits with optional single space/dash separators.
# ---------------------------------------------------------------------------

# Visa: starts with 4, 13 or 16 digits.
_VISA = r"4\d{3}(?:[\s\-]?\d{4}){2}(?:[\s\-]?\d{4}|(?:[\s\-]?\d{1})?)"
# Mastercard: 51–55 (16 digits) or 2221–2720 (16 digits).
_MC = r"(?:5[1-5]\d{2}|2(?:2[2-9]\d|[3-6]\d{2}|7[01]\d|720))(?:[\s\-]?\d{4}){3}"
# Amex: starts with 34 or 37, 15 digits.
_AMEX = r"3[47]\d{2}[\s\-]?\d{6}[\s\-]?\d{5}"
# Discover: starts with 6011, 65, or 644–649.
_DISCOVER = r"(?:6011|65\d{2}|64[4-9]\d)(?:[\s\-]?\d{4}){3}"

_CARD_RE = re.compile(r"(?<!\d)" r"(?:" + "|".join([_AMEX, _VISA, _MC, _DISCOVER]) + r")" r"(?!\d)")


def _luhn_check(digits: str) -> bool:
    """Return True iff *digits* passes the Luhn algorithm.

    *digits* must contain only ASCII digit characters (no separators).
    """
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _strip_separators(s: str) -> str:
    """Remove spaces and dashes from a candidate string."""
    return s.replace(" ", "").replace("-", "")


class _CreditCardPattern:
    name: str = "credit_card"
    category: str = "FINANCIAL"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all credit card number matches in *text* that pass Luhn.

        Never raises; returns ``[]`` on any internal error.
        """
        try:
            results: list[PiiMatchResult] = []
            for m in _CARD_RE.finditer(text):
                matched = m.group()
                digits = _strip_separators(matched)
                if _luhn_check(digits):
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
pattern = _CreditCardPattern()
