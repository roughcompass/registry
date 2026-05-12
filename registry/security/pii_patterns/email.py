"""Built-in PII pattern: e-mail address (RFC 5322-lite).

Detection approach
------------------
Regex anchored to word/token boundaries.  Covers the vast majority of real
e-mail addresses while keeping false-positive rates low.  Full RFC 5322
grammar (quoted strings, comments, folding whitespace) is intentionally out
of scope — the goal is practical in-context detection, not a mail library.

Pattern notes
-------------
- Local part: printable ASCII minus whitespace and ``@``; allows ``+`` tags.
- Domain: hostname or IP literal (bare IP is intentionally excluded to avoid
  false positives on version strings like ``1.2.3.4``).
- TLD: at least two letters.
- Word-boundary anchors prevent matching substrings of larger tokens.
"""

from __future__ import annotations

import re

from registry.types import PiiMatchResult

# Compiled once at module load — never per call.
_EMAIL_RE = re.compile(
    r"""
    (?<![.\w@])             # negative lookbehind: not preceded by word/dot/@
    (?P<addr>
        [a-zA-Z0-9._%+\-]+  # local part
        @
        [a-zA-Z0-9.\-]+     # domain labels
        \.[a-zA-Z]{2,}      # top-level domain (min 2 chars)
    )
    (?![.\w@])              # negative lookahead: not followed by word/dot/@
    """,
    re.VERBOSE,
)


class _EmailPattern:
    name: str = "email"
    category: str = "CONTACT"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all e-mail address matches in *text*.

        Never raises; returns ``[]`` on any internal error.
        """
        try:
            results: list[PiiMatchResult] = []
            for m in _EMAIL_RE.finditer(text):
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


#: Module-level singleton.  Imported by the dispatcher.
pattern = _EmailPattern()
