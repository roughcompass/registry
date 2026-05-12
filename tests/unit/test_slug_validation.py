"""Unit tests for `registry.service.slugs.validate_slug`.

Slugs are the URL/MCP-tool-friendly handles used to address capabilities,
tenants, and external systems by name. The validator runs on every
write that introduces a new slug; existing rows are not retroactively
validated.
"""

from __future__ import annotations

import pytest

from registry.exceptions import ValidationError
from registry.service.slugs import is_valid_slug, validate_slug


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "z",
        "0",
        "salt-design-system",
        "salt-ds",
        "user-preferences",
        "x-1-y",
        "abc-123-xyz",
        "a" * 200,
        "ab",
    ],
)
def test_accepts_slug_form(value: str) -> None:
    validate_slug(value)
    assert is_valid_slug(value) is True


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        ("Salt", "uppercase"),
        ("Salt-DS", "uppercase"),
        ("salt ds", "space"),
        ("salt_ds", "underscore"),
        ("salt.ds", "dot"),
        ("salt/ds", "slash"),
        ("-salt", "leading hyphen"),
        ("salt-", "trailing hyphen"),
        ("salt--ds", "consecutive hyphens"),
        ("a" * 201, "too long"),
        ("salté", "non-ascii"),
        ("salt ds ", "trailing space"),
        (" salt-ds", "leading space"),
    ],
)
def test_rejects_non_slug(value: str, reason: str) -> None:
    with pytest.raises(ValidationError):
        validate_slug(value)
    assert is_valid_slug(value) is False, reason


def test_field_name_appears_in_error_message() -> None:
    with pytest.raises(ValidationError, match="tenant slug"):
        validate_slug("Salt", field="tenant slug")


def test_non_string_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        validate_slug(None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validate_slug(123)  # type: ignore[arg-type]
