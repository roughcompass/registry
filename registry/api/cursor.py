"""Cursor pagination helpers for list endpoints.

Cursor pagination over offset:
- Predictable performance at any depth (`WHERE (col1, col2) > (?, ?)`
  is a keyset scan on an index; offset N scans + skips N rows).
- Stable under concurrent inserts — offset pagination skips or
  re-shows rows when the underlying set changes.

Cursor format: opaque base64-encoded JSON. The payload's keys are the
columns the route's ORDER BY uses (e.g. `{"ts": "2026-05-11T...",
"id": "..."}` for the audit log). Opaque means clients MUST NOT
interpret or mutate the cursor — change the encoding freely.

Routes integrate as:

    cursor_payload = decode_cursor(request.query_params.get("cursor"))
    rows = await query.where(
        (Audit.ts, Audit.audit_id) < (cursor_payload.get("ts"),
                                     cursor_payload.get("id"))
    ).order_by(Audit.ts.desc(), Audit.audit_id.desc()).limit(page_size + 1)
    has_more = len(rows) > page_size
    items = rows[:page_size]
    next_cursor = encode_cursor({"ts": ..., "id": ...}) if has_more else None

Encoding notes:
- New tokens: URL-safe base64, no padding (= signs stripped).
- Legacy tokens (issued by the old audit-log encoder): standard base64,
  with padding. For all-ASCII JSON payloads (timestamps + UUIDs), the
  URL-safe and standard alphabets produce identical character streams
  — only the trailing ``=`` padding differs. Stripping padding on decode
  already handles those tokens transparently. No separate fallback path
  is needed.

Strict mode (``strict=True``) raises ``InvalidCursorError`` on malformed
input rather than returning ``{}``. Use for endpoints where a broken
cursor is almost certainly a client error.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

_log = logging.getLogger(__name__)


class InvalidCursorError(Exception):
    """Raised by ``decode_cursor(strict=True)`` when the cursor token is malformed.

    Callers at router boundaries catch this and map it to a 422 with
    ``code: "invalid_cursor"``. The message is always a generic string
    so no internal state leaks to the API consumer.
    """


def encode_cursor(payload: dict[str, Any]) -> str:
    """Encode the payload as an opaque base64 string.

    URL-safe base64 with no padding (= signs are stripped); decoding
    handles the missing padding. Strings, ints, UUID-strings, ISO-8601
    datetimes — anything JSON-serializable — are fine.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None, *, strict: bool = False) -> dict[str, Any]:
    """Decode the opaque cursor. Empty/None → empty dict (start at top).

    Default mode (``strict=False``): malformed cursors return ``{}`` so
    the request falls back to page one gracefully. Use for new list
    endpoints where a broken cursor should degrade silently.

    Strict mode (``strict=True``): malformed cursors raise
    ``InvalidCursorError``. Use for endpoints (e.g. audit log) where an
    invalid cursor almost certainly indicates a client error or a tampered
    token, and returning page one silently would be confusing.

    Backward compatibility: the old audit-log cursor encoder used standard
    base64 with padding (``b64encode``). For all-ASCII JSON payloads
    (timestamps + UUIDs), the standard and URL-safe alphabets produce the
    same character stream — only trailing ``=`` differs. Adding back the
    padding before decode handles both token shapes transparently without
    any special-case logic. Tokens containing ``+`` or ``/`` from
    non-ASCII content would not decode correctly, but the audit-log payload
    never contains such bytes. Document this when the audit router is
    eventually extended to support non-ASCII fields.
    """
    if cursor is None or not cursor:
        return {}
    try:
        # Pad to a multiple of 4 for base64 decode. This handles both
        # new tokens (stripped of padding) and old tokens (padded with =).
        padding = (-len(cursor)) % 4
        raw = base64.urlsafe_b64decode(cursor + "=" * padding)
        return json.loads(raw)  # type: ignore[no-any-return]
    except (ValueError, json.JSONDecodeError) as exc:
        if strict:
            raise InvalidCursorError("invalid cursor") from exc
        _log.debug("decode_cursor: malformed cursor, treating as empty")
        return {}


__all__ = ["InvalidCursorError", "decode_cursor", "encode_cursor"]
