"""Unit tests for validate_progression_definition and the meta-schema.

Pure Python — no DB, no HTTP. Each test exercises one validation constraint
from the ProgressionDefinition meta-schema.

The canonical valid example used in the first test mirrors the v1 contract
documented in the definition JSONB contract section of the relevant ADR.
"""

from __future__ import annotations

import pytest

from registry.exceptions import ValidationError
from registry.service.progression import validate_progression_definition

# ---------------------------------------------------------------------------
# Canonical valid definition (used as the baseline across tests)
# ---------------------------------------------------------------------------

_CANONICAL_VALID: dict = {
    "states": [
        {
            "id": "1",
            "name": "Intake & Inception",
            "gates": ["tier-confirmed", "executive-sponsor-approval", "scope-confirmed"],
        },
        {
            "id": "2",
            "name": "Discovery & Requirements",
            "gates": ["prd-completeness", "nfr-coverage"],
        },
        {
            "id": "3",
            "name": "Architecture & System Design",
            "gates": ["arb-approved", "threat-model-approved", "control-design-complete"],
        },
    ],
    "transitions": {
        "forward": "sequential",
        "reentry": {"allowed": True, "requires": ["reason"]},
        "skip": "tier-conditional",
    },
    "tier_rules": {
        "T5": {"required": ["1", "5"], "skip": ["2", "3", "4", "6", "9", "10"]},
        "T4": {"required": ["1", "5", "8"], "skip": ["2", "3", "6", "9", "10"]},
        "default": {"required": [], "skip": []},
    },
}


# ---------------------------------------------------------------------------
# 1. Valid definition passes without raising
# ---------------------------------------------------------------------------


def test_canonical_valid_definition_passes() -> None:
    """The canonical v1 definition must pass meta-schema validation without raising."""
    validate_progression_definition(_CANONICAL_VALID)


def test_minimal_valid_definition_passes() -> None:
    """A minimal definition (no optional keys) is also valid."""
    validate_progression_definition(
        {
            "states": [{"id": "start", "name": "Start"}],
            "transitions": {"forward": "any"},
        }
    )


def test_forward_any_with_explicit_list_skip_passes() -> None:
    """forward=any combined with skip=explicit-list (no tier_rules needed) is valid."""
    validate_progression_definition(
        {
            "states": [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}],
            "transitions": {"forward": "any", "skip": "explicit-list"},
        }
    )


# ---------------------------------------------------------------------------
# 2. forward = "explicit-graph" is rejected (not in the v1 enum)
# ---------------------------------------------------------------------------


def test_forward_explicit_graph_rejected() -> None:
    """explicit-graph is not a valid transitions.forward value; must be rejected."""
    bad = {
        **_CANONICAL_VALID,
        "transitions": {**_CANONICAL_VALID["transitions"], "forward": "explicit-graph"},
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    # Error message must reference the failing field and the allowed values.
    assert "forward" in msg or "transitions" in msg
    assert "explicit-graph" in msg or "'sequential'" in msg or "sequential" in msg


# ---------------------------------------------------------------------------
# 3. Empty states array is rejected (minItems: 1)
# ---------------------------------------------------------------------------


def test_empty_states_array_rejected() -> None:
    """An empty states array violates minItems: 1 and must be rejected."""
    bad = {**_CANONICAL_VALID, "states": []}
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    assert "states" in msg


# ---------------------------------------------------------------------------
# 4. skip=tier-conditional without tier_rules is rejected
# ---------------------------------------------------------------------------


def test_tier_conditional_without_tier_rules_rejected() -> None:
    """skip=tier-conditional requires tier_rules (with a default key); omitting it must fail."""
    bad = {
        "states": [{"id": "1", "name": "Intake"}],
        "transitions": {"forward": "sequential", "skip": "tier-conditional"},
        # tier_rules intentionally absent
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    assert "tier_rules" in msg


def test_tier_conditional_without_default_key_rejected() -> None:
    """tier_rules present but missing the required default key must fail."""
    bad = {
        "states": [{"id": "1", "name": "Intake"}],
        "transitions": {"forward": "sequential", "skip": "tier-conditional"},
        "tier_rules": {
            "T5": {"required": ["1"], "skip": []},
            # "default" key intentionally absent
        },
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    assert "default" in msg or "tier_rules" in msg


# ---------------------------------------------------------------------------
# 5. Additional invalid shapes
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_rejected() -> None:
    """additionalProperties: false at the top level must reject unknown keys."""
    bad = {**_CANONICAL_VALID, "unexpected_key": "value"}
    with pytest.raises(ValidationError):
        validate_progression_definition(bad)


def test_state_missing_id_rejected() -> None:
    """Each state element must carry an id string; missing it must fail."""
    bad = {
        "states": [{"name": "No ID State"}],
        "transitions": {"forward": "sequential"},
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    assert "id" in msg


def test_state_missing_name_rejected() -> None:
    """Each state element must carry a name string; missing it must fail."""
    bad = {
        "states": [{"id": "1"}],
        "transitions": {"forward": "sequential"},
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    assert "name" in msg


def test_reentry_missing_allowed_rejected() -> None:
    """reentry object must have allowed (boolean); missing it must fail."""
    bad = {
        "states": [{"id": "1", "name": "Start"}],
        "transitions": {
            "forward": "sequential",
            "reentry": {"requires": ["reason"]},  # allowed missing
        },
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    assert "allowed" in msg


def test_missing_states_rejected() -> None:
    """states is a required key; omitting it must fail."""
    bad = {"transitions": {"forward": "sequential"}}
    with pytest.raises(ValidationError):
        validate_progression_definition(bad)


def test_missing_transitions_rejected() -> None:
    """transitions is a required key; omitting it must fail."""
    bad = {"states": [{"id": "1", "name": "Start"}]}
    with pytest.raises(ValidationError):
        validate_progression_definition(bad)


def test_invalid_skip_value_rejected() -> None:
    """transitions.skip only accepts the three documented values."""
    bad = {
        "states": [{"id": "1", "name": "Start"}],
        "transitions": {"forward": "sequential", "skip": "always"},
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_progression_definition(bad)

    msg = str(exc_info.value)
    assert "skip" in msg
