"""Weak ETag computation + If-Match precondition check.

Used by detail-GET handlers (capabilities, artifacts) to emit an
``ETag: W/"<hex>"`` header callers can echo back as ``If-Match`` on
subsequent PATCH/DELETE to get optimistic concurrency.

Format: weak ETag (``W/"<sha256-hex>"``). Weak because content
equivalence is the bar — byte-exact equality across encodings isn't
required.

Mode: advisory.  PATCH endpoints log a warning when ``If-Match`` is
absent but accept the write.  When ``If-Match`` is present and stale,
the route returns 412 Precondition Failed with the structured error
envelope.  A future env-var (``REGISTRY_REQUIRE_IF_MATCH=1``) would
flip this to strict; left as carry-over.

ETag inputs are intentionally small:
- ``entity_id`` (or ``fact_id`` for artifacts) — identity.
- The most-recent transaction timestamp the response includes
  (max of ``t_ingested_at`` across the row + its child attributes /
  facts / edges) — content version.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Any

_log = logging.getLogger(__name__)


def compute_etag(*parts: Any) -> str:
    """Compute a weak ETag from the given parts.

    Parts are stringified and joined with ``|``; the sha256 hex of that
    string is wrapped in the ``W/"<hex>"`` form. Non-ordered inputs
    (e.g. sets) MUST be sorted by the caller — this function is order-
    sensitive.
    """
    payload = "|".join("" if p is None else str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f'W/"{digest}"'


def latest_timestamp(*timestamps: datetime.datetime | None) -> datetime.datetime | None:
    """Return the latest non-None timestamp, or None if all are None."""
    present = [t for t in timestamps if t is not None]
    return max(present) if present else None


def check_if_match(
    request_header: str | None,
    current_etag: str,
    *,
    resource_kind: str,
) -> None:
    """Compare the caller-supplied ``If-Match`` to the current ETag.

    Raises HTTPException(412) on mismatch (the global error handler wraps
    it into the structured envelope).  When the header is absent, logs a
    warning and returns — advisory mode.  Strict mode (env-gated) is a
    carry-over.
    """
    from fastapi import HTTPException, status  # noqa: PLC0415

    if request_header is None or not request_header.strip():
        _log.debug("if_match_absent resource=%s", resource_kind)
        return
    # Multiple values allowed in If-Match (comma-separated). Match if any.
    candidates = [c.strip() for c in request_header.split(",") if c.strip()]
    if "*" in candidates:
        # "*" matches any current representation (per RFC 7232 §3.1).
        return
    if current_etag in candidates:
        return
    raise HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail={
            "code": "precondition_failed",
            "message": (f"{resource_kind} changed since the If-Match ETag was issued; " "refetch and retry."),
            "path": None,
        },
    )


__all__ = ["check_if_match", "compute_etag", "latest_timestamp"]
