"""Unit tests for the RSAM SEAL-prefix authority grammar parser.

Coverage:
- parse_authority: 11 edge cases covering canonical happy paths, SEAL width
  boundaries (4–6 digits), and all failure modes (bad digit count, missing
  prefix, lowercase resource, empty verb, etc.).
- verb_to_role: all 6 verb entries in the verb-to-role table.
- highest_role: precedence resolution across role pairs.
"""

from __future__ import annotations

import pytest

from registry.auth.rsam.grammar import (
    _PARSE_FAILED,
    _PARSE_SKIPPED,
    ROLE_PRECEDENCE,
    VERB_TO_ROLE,
    ParsedAuthority,
    highest_role,
    parse_authority,
    verb_to_role,
)

# ---------------------------------------------------------------------------
# parse_authority — success cases

def test_parse_canonical_owner() -> None:
    """Canonical happy-path authority with a 6-digit SEAL ID."""
    result = parse_authority("112025_DP_CHANNEL_Owner")
    assert result == ParsedAuthority(seal_id="112025", resource="DP_CHANNEL", verb="Owner")


def test_parse_foreign_seal_ru() -> None:
    """Foreign-SEAL read+update grant — 5-digit SEAL ID."""
    result = parse_authority("34612_DP_MODULE_RU")
    assert result == ParsedAuthority(seal_id="34612", resource="DP_MODULE", verb="RU")


def test_parse_four_digit_seal_lower_bound() -> None:
    """4-digit SEAL ID is the lower bound of the allowed range — must pass."""
    result = parse_authority("1234_DP_CHANNEL_CRUD")
    assert result == ParsedAuthority(seal_id="1234", resource="DP_CHANNEL", verb="CRUD")


# ---------------------------------------------------------------------------
# parse_authority — failure cases (all 11 documented edge cases)

def test_parse_seven_digit_seal_exceeds_upper_bound() -> None:
    """7-digit SEAL ID exceeds the 4–6 digit upper bound; must return None."""
    assert parse_authority("1234567_DP_CHANNEL_CRUD") is None


def test_parse_no_seal_prefix() -> None:
    """Authority without a SEAL prefix (platform-wide or unknown shape); must return None."""
    assert parse_authority("DP_CHANNEL_CRUD") is None


def test_parse_lowercase_resource_rejected() -> None:
    """Lowercase resource does not satisfy the uppercase grammar; must return None."""
    assert parse_authority("112025_dp_channel_owner") is None


def test_parse_hyphen_in_resource_rejected() -> None:
    """Hyphen in resource is outside the grammar; must return None."""
    assert parse_authority("112025_DP-CHANNEL_CRUD") is None


def test_parse_unknown_verb_rejected() -> None:
    """DELETE is not in the closed verb enumeration; must return None."""
    assert parse_authority("112025_DP_CHANNEL_DELETE") is None


def test_parse_trailing_token_rejected() -> None:
    """Trailing token after verb violates the $ anchor; must return None."""
    assert parse_authority("112025_DP_CHANNEL_RU_extra") is None


def test_parse_empty_string_rejected() -> None:
    """Empty string is trivially rejected."""
    assert parse_authority("") is None


def test_parse_missing_seal_id_rejected() -> None:
    """String starting with _ has no seal_id component; must return None."""
    assert parse_authority("_DP_CHANNEL_Owner") is None


# ---------------------------------------------------------------------------
# parse_authority — remaining verb coverage

def test_parse_verb_manager() -> None:
    result = parse_authority("112025_DP_MODULE_Manager")
    assert result is not None
    assert result.verb == "Manager"


def test_parse_verb_operate() -> None:
    result = parse_authority("112025_DP_STUDIO_Operate")
    assert result is not None
    assert result.verb == "Operate"


def test_parse_verb_r() -> None:
    result = parse_authority("112025_DP_CHANNEL_R")
    assert result is not None
    assert result.verb == "R"


# ---------------------------------------------------------------------------
# verb_to_role — all 6 verb-to-role table entries

@pytest.mark.parametrize("verb,expected_role", [
    ("Owner",   "admin"),
    ("Manager", "producer"),
    ("Operate", "auditor"),
    ("CRUD",    "admin"),
    ("RU",      "producer"),
    ("R",       "viewer"),
])
def test_verb_to_role_table(verb: str, expected_role: str) -> None:
    assert verb_to_role(verb) == expected_role


# ---------------------------------------------------------------------------
# highest_role — precedence resolution

def test_highest_role_producer_beats_auditor() -> None:
    assert highest_role(["producer", "auditor"]) == "producer"


def test_highest_role_auditor_beats_viewer() -> None:
    assert highest_role(["viewer", "auditor"]) == "auditor"


def test_highest_role_admin_beats_producer() -> None:
    assert highest_role(["admin", "producer"]) == "admin"


def test_highest_role_single_element() -> None:
    assert highest_role(["viewer"]) == "viewer"


def test_highest_role_empty_returns_viewer() -> None:
    """Empty list safe-defaults to viewer (least-privilege)."""
    assert highest_role([]) == "viewer"


# ---------------------------------------------------------------------------
# Structural invariants

def test_verb_to_role_covers_all_verbs() -> None:
    """Every verb accepted by the regex has a corresponding role mapping."""
    accepted_verbs = {"Owner", "Manager", "Operate", "CRUD", "RU", "R"}
    assert set(VERB_TO_ROLE.keys()) == accepted_verbs


def test_role_precedence_is_ordered_highest_first() -> None:
    """ROLE_PRECEDENCE list must begin with admin and end with viewer."""
    assert ROLE_PRECEDENCE[0] == "admin"
    assert ROLE_PRECEDENCE[-1] == "viewer"


# ---------------------------------------------------------------------------
# Metric emission — counters fire on non-match paths

def test_metric_parse_skipped_emits_on_non_digit_prefix() -> None:
    """parse_authority on a non-digit-prefix string increments parse_skipped."""
    authority = "DP_CHANNEL_CRUD"
    skipped_before = _PARSE_SKIPPED.labels(source="rsam", shape="DP_CHANN...")._value.get()  # type: ignore[attr-defined]
    failed_before = _PARSE_FAILED.labels(source="rsam", shape="DP_CHANN...")._value.get()  # type: ignore[attr-defined]

    result = parse_authority(authority)

    assert result is None
    skipped_after = _PARSE_SKIPPED.labels(source="rsam", shape="DP_CHANN...")._value.get()  # type: ignore[attr-defined]
    failed_after = _PARSE_FAILED.labels(source="rsam", shape="DP_CHANN...")._value.get()  # type: ignore[attr-defined]
    assert skipped_after == skipped_before + 1, "parse_skipped must increment for non-digit-prefix authority"
    assert failed_after == failed_before, "parse_failed must not increment for non-digit-prefix authority"


def test_metric_parse_failed_emits_on_digit_prefix_invalid_shape() -> None:
    """parse_authority on a digit-prefix string that fails the grammar increments parse_failed."""
    authority = "1234567_DP_CHANNEL_CRUD"  # 7-digit SEAL exceeds the 4–6 digit upper bound
    shape = "1234567_..."
    skipped_before = _PARSE_SKIPPED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]
    failed_before = _PARSE_FAILED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]

    result = parse_authority(authority)

    assert result is None
    skipped_after = _PARSE_SKIPPED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]
    failed_after = _PARSE_FAILED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]
    assert failed_after == failed_before + 1, (
        "parse_failed must increment for digit-prefix authority that fails grammar"
    )
    assert skipped_after == skipped_before, "parse_skipped must not increment for digit-prefix authority"


def test_metric_no_emission_on_match() -> None:
    """parse_authority on a valid authority emits neither parse_skipped nor parse_failed."""
    authority = "112025_DP_CHANNEL_Owner"
    shape = "112025_D..."
    skipped_before = _PARSE_SKIPPED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]
    failed_before = _PARSE_FAILED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]

    result = parse_authority(authority)

    assert result is not None
    skipped_after = _PARSE_SKIPPED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]
    failed_after = _PARSE_FAILED.labels(source="rsam", shape=shape)._value.get()  # type: ignore[attr-defined]
    assert skipped_after == skipped_before, "parse_skipped must not increment on a successful match"
    assert failed_after == failed_before, "parse_failed must not increment on a successful match"
