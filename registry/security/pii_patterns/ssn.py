"""Built-in PII pattern: US Social Security Number (SSN).

Detection approach
------------------
Canonical format: ``NNN-NN-NNNN`` with dash separators.
Additional variant: space-separated ``NNN NN NNNN``.
Bare 9-digit run (9 consecutive digits) is intentionally excluded — the false-positive
rate on IDs, account numbers, and other numeric tokens is too high.

False-positive filtering
------------------------
- Area code 000, 666, or 900–999 is invalid per SSA rules → excluded.
- Group code 00 is invalid → excluded.
- Serial code 0000 is invalid → excluded.
- All-identical digits (e.g. ``111-11-1111``) are test numbers → excluded.
- Word-boundary anchors prevent matching within longer digit strings.
"""

from __future__ import annotations

import re

from registry.types import PiiMatchResult

# Primary pattern: NNN-NN-NNNN (dashes) or NNN NN NNNN (spaces).
# Groups named for the validity checks below.
_SSN_RE = re.compile(
    r"""
    (?<!\d)
    (?P<area>\d{3})
    [-\s]
    (?P<group>\d{2})
    [-\s]
    (?P<serial>\d{4})
    (?!\d)
    """,
    re.VERBOSE,
)

# Invalid area codes per SSA: 000, 666, 900–999.
_INVALID_AREA = re.compile(r"^(000|666|9\d{2})$")
# All-identical-digit numbers are SSA test numbers.
_ALL_SAME = re.compile(r"^(\d)\1{8}$")


def _is_valid_ssn(area: str, group: str, serial: str) -> bool:
    """Return True iff the SSN components pass SSA validity heuristics."""
    if _INVALID_AREA.match(area):
        return False
    if group == "00":
        return False
    if serial == "0000":
        return False
    # Reject trivially repeated sequences (e.g. 123-12-1234 is fine, 111-11-1111 is not).
    digits = area + group + serial
    if _ALL_SAME.match(digits):
        return False
    return True


class _SsnPattern:
    name: str = "ssn"
    category: str = "GOVERNMENT_ID"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all SSN matches in *text*.

        Never raises; returns ``[]`` on any internal error.
        """
        try:
            results: list[PiiMatchResult] = []
            for m in _SSN_RE.finditer(text):
                if _is_valid_ssn(m.group("area"), m.group("group"), m.group("serial")):
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
pattern = _SsnPattern()
