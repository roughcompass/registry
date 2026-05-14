"""Parses RSAM authority strings into ParsedAuthority.

SEAL ID is the registry's tenant_external_id; verb maps to the registry role
vocabulary (admin | producer | auditor | viewer).

Authority strings follow the shape:
    <seal_id>_<resource>_<verb>

where seal_id is 4–6 decimal digits, resource is an uppercase identifier, and
verb is a closed enumeration drawn from the verb-to-role table below. A
non-matching string is not a tenant-scoped RSAM authority and yields None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from prometheus_client import Counter

# ---------------------------------------------------------------------------
# Grammar constant — bump when the regex or verb table changes so metric tags
# can distinguish parser revisions in the operations dashboard.
GRAMMAR_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Metrics — two counters instrument non-match outcomes from parse_authority.
#
# _PARSE_SKIPPED fires when the authority string clearly is not SEAL-shaped
# (non-digit prefix or empty). These are expected — platform-wide authorities
# route through this parser and simply don't match.
#
# _PARSE_FAILED fires when the string starts with digits (looks SEAL-shaped)
# but fails the full grammar. This signals an unexpected RSAM emission shape
# (e.g. a 7-digit SEAL or a lowercase resource segment) and warrants alerting.
#
# The `shape` label is a truncated prefix of the authority string, capped at
# 8 characters, so dashboards can identify patterns without unbounded cardinality.
_PARSE_SKIPPED = Counter(
    "auth_authority_parse_skipped_total",
    "Authority string did not match the SEAL prefix grammar (probably a platform-wide authority).",
    ["source", "shape"],
)

_PARSE_FAILED = Counter(
    "auth_authority_parse_failed_total",
    "Authority string looked SEAL-shaped but failed the full grammar (unexpected RSAM emission).",
    ["source", "shape"],
)

# Digits-only prefix pattern — used to distinguish skip from fail on non-match.
_DIGIT_PREFIX_RE = re.compile(r"^\d")

# ---------------------------------------------------------------------------
# Core regex — the grammar is intentionally strict:
#   - seal_id: decimal-only (alphanumeric SEAL IDs are rejected to prevent
#     resource-name fragments from being mis-classified as SEAL IDs).
#   - resource: uppercase + underscores + digits, ≥ 2 chars, must end in
#     uppercase letter or digit (single-char resource names do not match).
#   - verb: closed enumeration; the $ anchor rejects trailing tokens.
_AUTHORITY_RE = re.compile(
    r"^"
    r"(?P<seal_id>\d{4,6})"
    r"_"
    r"(?P<resource>[A-Z][A-Z0-9_]*[A-Z0-9])"
    r"_"
    r"(?P<verb>Owner|Manager|Operate|CRUD|RU|R)"
    r"$"
)

# ---------------------------------------------------------------------------
# Verb-to-role mapping.
# "Operate" maps to "auditor" as a conservative safe-fail choice: the RSAM
# verb may convey read+mutate-operational-state semantics (closer to
# "producer"), but until that is confirmed externally the mapping errs toward
# under-permission rather than over-permission.
VERB_TO_ROLE: dict[str, str] = {
    "Owner": "admin",
    "Manager": "producer",
    "Operate": "auditor",  # conservative — pending external confirmation
    "CRUD": "admin",
    "RU": "producer",
    "R": "viewer",
}

# Alias used internally (tasks.md naming convention).
VERB_ROLE = VERB_TO_ROLE

# ---------------------------------------------------------------------------
# Role precedence — highest role wins when a user holds multiple authorities
# for the same SEAL ID.  admin > producer > auditor > viewer.
ROLE_PRECEDENCE: list[str] = ["admin", "producer", "auditor", "viewer"]

# Numeric-weight variant for callers that need fast comparison.
_ROLE_WEIGHT: dict[str, int] = {role: len(ROLE_PRECEDENCE) - i for i, role in enumerate(ROLE_PRECEDENCE)}


# ---------------------------------------------------------------------------
# Data model


@dataclass(frozen=True)
class ParsedAuthority:
    """Decomposed RSAM authority string."""

    seal_id: str  # 4–6 decimal digits; maps to tenant_external_id
    resource: str  # uppercase identifier; opaque for role derivation
    verb: str  # one of Owner | Manager | Operate | CRUD | RU | R


# ---------------------------------------------------------------------------
# Helpers


def _shape_label(authority: str) -> str:
    """Return a cardinality-safe label value for a given authority string.

    The label is the first 8 characters of the authority followed by "..." when
    the string is longer than 8 characters, or the full string otherwise. An
    empty string is represented as the literal "<empty>" so it is visible in
    dashboards and does not collapse with other empty label values.
    """
    if not authority:
        return "<empty>"
    return authority[:8] + ("..." if len(authority) > 8 else "")


# ---------------------------------------------------------------------------
# Public API


def parse_authority(authority: str) -> ParsedAuthority | None:
    """Return ParsedAuthority on match; None on non-match. Never raises.

    On non-match, increments one of two Prometheus counters:
    - auth_authority_parse_skipped_total: authority did not start with digits,
      so it is clearly not a SEAL-prefixed authority (e.g. a platform-wide token).
    - auth_authority_parse_failed_total: authority starts with digits (looks
      SEAL-shaped) but failed the full grammar — indicates an unexpected RSAM
      emission shape and warrants investigation.
    """
    m = _AUTHORITY_RE.match(authority)
    if m is None:
        shape = _shape_label(authority)
        if authority and _DIGIT_PREFIX_RE.match(authority):
            _PARSE_FAILED.labels(source="rsam", shape=shape).inc()
        else:
            _PARSE_SKIPPED.labels(source="rsam", shape=shape).inc()
        return None
    return ParsedAuthority(
        seal_id=m.group("seal_id"),
        resource=m.group("resource"),
        verb=m.group("verb"),
    )


# Shorter alias matching tasks.md naming.
def parse(authority: str) -> ParsedAuthority | None:
    """Return ParsedAuthority on match; None on non-match. Never raises."""
    return parse_authority(authority)


def verb_to_role(verb: str) -> str:
    """Return the registry role for a known verb per the verb-to-role table.

    Raises KeyError for unknown verbs — callers should validate the verb via
    parse_authority() first, which only accepts the closed verb enumeration.
    """
    return VERB_TO_ROLE[verb]


def highest_role(roles: list[str]) -> str:
    """Return the highest-precedence role from roles per ROLE_PRECEDENCE.

    If roles is empty, returns "viewer" as a safe default.
    """
    if not roles:
        return "viewer"
    return max(roles, key=lambda r: _ROLE_WEIGHT.get(r, 0))
