"""Built-in PII pattern: AWS Access Key ID.

Detection approach
------------------
AWS Access Key IDs always begin with ``AKIA`` (for long-term user credentials)
or ``ASIA`` (for temporary STS credentials) followed by exactly 16 uppercase
alphanumeric characters.  Total length: 20 characters.

References
----------
- https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html
- AWS IAM key anatomy blog posts from AWS Security team.

False-positive mitigation
--------------------------
Word-boundary anchors on both sides prevent matching the pattern mid-word.
The distinctive ``AKIA`` / ``ASIA`` prefix gives very low false-positive rates.
"""

from __future__ import annotations

import re

from registry.types import PiiMatchResult

# Matches AKIA or ASIA followed by 16 uppercase alphanumeric characters.
_AWS_ACCESS_KEY_RE = re.compile(r"(?<!\w)(AKIA|ASIA)[0-9A-Z]{16}(?!\w)")


class _AwsAccessKeyPattern:
    name: str = "aws_access_key"
    category: str = "CREDENTIALS"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all AWS Access Key ID matches in *text*.

        Never raises; returns ``[]`` on any internal error.
        """
        try:
            results: list[PiiMatchResult] = []
            for m in _AWS_ACCESS_KEY_RE.finditer(text):
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
pattern = _AwsAccessKeyPattern()
