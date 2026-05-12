"""Slug validation for human-readable resource handles.

A "slug" is the lowercase, hyphen-separated form a developer / copilot /
URL would use to refer to a resource: ``salt-design-system``, ``dev``,
``npm``. Slugs let us address capabilities, tenants, and external
systems by a stable name without needing the UUID.

Rules enforced here (and only here):
- ASCII lowercase letters, digits, and hyphens.
- 1-200 characters.
- Must start AND end with an alphanumeric (no leading or trailing hyphen).
- No consecutive hyphens.

Existing rows are not retroactively validated; this function runs at
write time only. A name created before the rule existed keeps working
in read paths because the resolver matches by exact (case-insensitive)
equality on the existing column.
"""

from __future__ import annotations

import re

from registry.exceptions import ValidationError

_SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,198}[a-z0-9])?$")
_CONSECUTIVE_HYPHENS: re.Pattern[str] = re.compile(r"--")


def validate_slug(value: str, *, field: str = "name") -> None:
    """Raise ValidationError if *value* is not a well-formed slug.

    Args:
        value: The candidate slug.
        field: Field name to surface in the error message
            (e.g. "name", "tenant slug").
    """
    if not isinstance(value, str) or not value:
        msg = f"{field} must be a non-empty string; got {value!r}"
        raise ValidationError(msg)
    if not _SLUG_RE.match(value):
        msg = (
            f"{field} must be lowercase alphanumeric + hyphens, "
            f"1-200 chars, starting and ending with alphanumeric; got {value!r}"
        )
        raise ValidationError(msg)
    if _CONSECUTIVE_HYPHENS.search(value):
        msg = f"{field} must not contain consecutive hyphens; got {value!r}"
        raise ValidationError(msg)


def is_valid_slug(value: str) -> bool:
    """Return True iff *value* would pass ``validate_slug``."""
    try:
        validate_slug(value)
    except ValidationError:
        return False
    return True


_ARTIFACT_TITLE_MAX_LEN = 200
_ALLOWED_BODY_FORMATS: frozenset[str] = frozenset({"markdown", "html", "plain"})


def validate_artifact_title(value: str) -> None:
    """Raise ValidationError if *value* isn't a usable artifact title.

    Rules: non-empty, 1-200 chars, no leading or trailing whitespace.
    UTF-8 contents are otherwise allowed (humans write titles in their
    own language; locking down to ASCII would be wrong here).
    """
    if not isinstance(value, str) or not value:
        raise ValidationError("title must be a non-empty string")
    if value != value.strip():
        raise ValidationError("title must not have leading or trailing whitespace")
    if len(value) > _ARTIFACT_TITLE_MAX_LEN:
        raise ValidationError(f"title must be at most {_ARTIFACT_TITLE_MAX_LEN} characters; got {len(value)}")


def validate_body_format(value: str) -> None:
    """Raise ValidationError if *value* isn't one of the allowed body formats."""
    if value not in _ALLOWED_BODY_FORMATS:
        allowed = sorted(_ALLOWED_BODY_FORMATS)
        raise ValidationError(f"body_format must be one of {allowed}; got {value!r}")


__all__ = [
    "is_valid_slug",
    "validate_artifact_title",
    "validate_body_format",
    "validate_slug",
]
