"""Unit tests for registry.api.cursor — opaque cursor encode/decode."""

from __future__ import annotations

import base64
import datetime
import json
import uuid

import pytest

from registry.api.cursor import InvalidCursorError, decode_cursor, encode_cursor


def _make_legacy_audit_cursor(ts: datetime.datetime, audit_id: uuid.UUID) -> str:
    """Reproduce the token shape issued by the old audit-log encoder.

    Old encoder: ``base64.b64encode(json.dumps(payload).encode()).decode()``
    — standard base64 with padding, ASCII-only JSON payload.
    """
    payload = {"ts": ts.isoformat(), "audit_id": str(audit_id)}
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"id": "abc"},
        {"ts": "2026-05-11T00:00:00+00:00", "id": str(uuid.uuid4())},
        {"page": 42, "key": "salt-design-system"},
        {"nested": {"a": 1, "b": [1, 2, 3]}},
    ],
)
def test_roundtrip(payload: dict[str, object]) -> None:
    encoded = encode_cursor(payload)
    assert isinstance(encoded, str)
    assert "=" not in encoded  # no base64 padding leaked
    decoded = decode_cursor(encoded)
    assert decoded == payload


def test_decode_empty_returns_empty_dict() -> None:
    assert decode_cursor(None) == {}
    assert decode_cursor("") == {}


def test_decode_malformed_returns_empty_dict() -> None:
    # Graceful degradation: malformed cursors don't crash the request.
    assert decode_cursor("not-base64!!!") == {}
    assert decode_cursor("YWJjZGVm") == {}  # valid base64, not valid JSON


def test_encode_is_stable() -> None:
    """Encoding the same payload yields the same cursor (sort_keys=True)."""
    payload = {"b": 2, "a": 1}
    assert encode_cursor(payload) == encode_cursor({"a": 1, "b": 2})


def test_datetime_serialization_uses_iso_format() -> None:
    """Callers must stringify datetimes before encoding — the payload is JSON."""
    ts = datetime.datetime(2026, 5, 11, tzinfo=datetime.UTC)
    encoded = encode_cursor({"ts": ts.isoformat()})
    decoded = decode_cursor(encoded)
    assert decoded == {"ts": "2026-05-11T00:00:00+00:00"}


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------


def test_strict_mode_raises_on_malformed_cursor() -> None:
    """strict=True raises InvalidCursorError instead of returning {}."""
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-base64!!!", strict=True)


def test_strict_mode_raises_on_invalid_json() -> None:
    """Valid base64 but not JSON is also an error in strict mode."""
    token = base64.urlsafe_b64encode(b"this is not json").rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        decode_cursor(token, strict=True)


def test_strict_mode_accepts_valid_cursor() -> None:
    """A well-formed cursor decodes correctly in strict mode."""
    payload = {"ts": "2026-05-11T00:00:00+00:00", "audit_id": str(uuid.uuid4())}
    token = encode_cursor(payload)
    assert decode_cursor(token, strict=True) == payload


def test_strict_mode_empty_cursor_returns_empty_dict() -> None:
    """None/empty string still returns {} even in strict mode — it's not malformed."""
    assert decode_cursor(None, strict=True) == {}
    assert decode_cursor("", strict=True) == {}


def test_strict_mode_error_message_is_generic() -> None:
    """The error message must not leak internal exception state."""
    with pytest.raises(InvalidCursorError, match="invalid cursor"):
        decode_cursor("garbage!!!!", strict=True)


# ---------------------------------------------------------------------------
# Backward-compat: legacy standard-padded base64 audit cursors
# ---------------------------------------------------------------------------


def test_legacy_audit_cursor_decodes_in_default_mode() -> None:
    """Audit cursors issued before the encoding was unified still decode.

    The old encoder used standard base64 with padding; the new encoder
    uses URL-safe base64 with no padding. For all-ASCII JSON payloads
    (timestamps + UUIDs) the two alphabets produce identical characters —
    only the trailing ``=`` differs. Adding back the padding on decode
    handles both shapes without a separate fallback path.
    """
    ts = datetime.datetime(2026, 4, 1, 12, 0, 0, tzinfo=datetime.UTC)
    audit_id = uuid.uuid4()
    legacy_token = _make_legacy_audit_cursor(ts, audit_id)
    result = decode_cursor(legacy_token)
    assert datetime.datetime.fromisoformat(result["ts"]) == ts
    assert uuid.UUID(result["audit_id"]) == audit_id


def test_legacy_audit_cursor_decodes_in_strict_mode() -> None:
    """Legacy audit cursors also decode correctly in strict mode."""
    ts = datetime.datetime(2026, 3, 15, 8, 30, 0, tzinfo=datetime.UTC)
    audit_id = uuid.uuid4()
    legacy_token = _make_legacy_audit_cursor(ts, audit_id)
    result = decode_cursor(legacy_token, strict=True)
    assert datetime.datetime.fromisoformat(result["ts"]) == ts
    assert uuid.UUID(result["audit_id"]) == audit_id


def test_legacy_token_with_padding_roundtrips() -> None:
    """A token with explicit ``=`` padding decodes to the same payload as the stripped version."""
    payload = {"ts": "2026-05-11T00:00:00+00:00", "audit_id": str(uuid.uuid4())}
    stripped = encode_cursor(payload)  # no padding
    padded = stripped + "=" * ((-len(stripped)) % 4)
    # Both forms must decode to the same payload.
    assert decode_cursor(stripped) == payload
    assert decode_cursor(padded) == payload
