"""Unit tests for semver 2.0.0 enforcement on capability.attributes.version.

Semver enforcement: a capability's version attribute must be a valid semver 2.0.0
string. The string "latest" is rejected because it prevents deterministic pinning
by consumers.

Contract under test
-------------------
``registry.service.catalog._validate_semver_attribute(attributes)``:

- If ``attributes['version']`` is unset or ``None`` → silent no-op.
- Else if the value parses via ``semver.Version.parse()`` → silent no-op.
  Pre-release (``-alpha.1``) and build metadata (``+sha.deadbeef``) suffixes
  are accepted.
- Else → raises :class:`catalog.exceptions.ValidationError` (mapped to HTTP 422
  by the API layer) with message:
  ``"'<value>' is not valid semver 2.0.0. Example: '2.4.1', '3.0.0-alpha.1'."``.

The helper is invoked from both ``CatalogService.create_entity`` and
``CatalogService.update_entity`` so the same rule applies on every write.

No I/O, no database. The helper is pure.
"""

from __future__ import annotations

import pytest

from registry.exceptions import ValidationError
from registry.service.catalog import _validate_semver_attribute

EXPECTED_MESSAGE_SUFFIX = " is not valid semver 2.0.0. Example: '2.4.1', '3.0.0-alpha.1'."


# ---------------------------------------------------------------------------
# Acceptance cases (no raise)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "2.4.1",  # canonical major.minor.patch (task contract)
        "3.0.0-alpha.1",  # pre-release suffix (task contract)
        "3.0.0+sha.abc",  # build metadata suffix (task contract)
        "0.0.1",  # zero major/minor
        "10.20.30",  # multi-digit components
        "1.0.0-rc.1+build.7",  # pre-release AND build metadata
    ],
)
def test_valid_semver_is_accepted(value: str) -> None:
    """Well-formed semver 2.0.0 strings must not raise."""
    _validate_semver_attribute({"version": value})  # no raise


def test_missing_version_is_noop() -> None:
    """Attributes without a ``version`` key are silently accepted."""
    _validate_semver_attribute({"owner": "team-foo"})  # no raise


def test_empty_attributes_is_noop() -> None:
    """An empty attributes dict is silently accepted."""
    _validate_semver_attribute({})  # no raise


def test_none_version_is_noop() -> None:
    """An explicit ``version=None`` is silently accepted (treat as unset)."""
    _validate_semver_attribute({"version": None})  # no raise


# ---------------------------------------------------------------------------
# Rejection cases (must raise ValidationError → 422)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "latest",  # keyword, not a version (task contract)
        "2.3",  # missing patch (task contract)
        "2",  # missing minor and patch
        "v2.4.1",  # leading v is non-canonical for semver.Version
        "2.4.1.5",  # too many components
        "abc",  # not numeric at all
        "",  # empty string
        "1.2.3-",  # dangling pre-release separator
        "1.2.3+",  # dangling build metadata separator
    ],
)
def test_invalid_semver_raises_validation_error(value: str) -> None:
    """Invalid semver values must raise ValidationError (mapped to HTTP 422)."""
    with pytest.raises(ValidationError) as exc_info:
        _validate_semver_attribute({"version": value})
    msg = str(exc_info.value)
    # Message must contain the offending value (quoted) and the canonical suffix.
    assert repr(value) in msg, f"expected {value!r} in message, got: {msg}"
    assert EXPECTED_MESSAGE_SUFFIX in msg, f"missing canonical suffix in: {msg}"


def test_non_string_version_is_rejected() -> None:
    """Non-string version values must be rejected (semver.Version.parse needs str)."""
    with pytest.raises(ValidationError) as exc_info:
        _validate_semver_attribute({"version": 2})
    assert EXPECTED_MESSAGE_SUFFIX in str(exc_info.value)


# ---------------------------------------------------------------------------
# Message-format precision (task contract specifies the exact form)
# ---------------------------------------------------------------------------


def test_message_format_for_latest() -> None:
    """Exact message for the canonical ``latest`` example from the contract."""
    with pytest.raises(ValidationError) as exc_info:
        _validate_semver_attribute({"version": "latest"})
    assert str(exc_info.value) == "'latest' is not valid semver 2.0.0. Example: '2.4.1', '3.0.0-alpha.1'."


def test_message_format_for_missing_patch() -> None:
    """Exact message for the ``2.3`` (missing patch) example from the contract."""
    with pytest.raises(ValidationError) as exc_info:
        _validate_semver_attribute({"version": "2.3"})
    assert str(exc_info.value) == "'2.3' is not valid semver 2.0.0. Example: '2.4.1', '3.0.0-alpha.1'."
