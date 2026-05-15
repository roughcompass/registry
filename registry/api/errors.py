"""Structured error envelope for the REST API.

Every error response — 4xx and 5xx — comes back in the shape::

    {
        "errors": [
            {
                "path": "$.name",         # JSON Pointer, or null when not field-specific
                "code": "slug_invalid",   # machine-readable, snake_case
                "message": "..."          # human-readable
            },
            ...
        ]
    }

Routers can emit errors three ways and all three produce the envelope:

1. ``raise HTTPException(status_code=422, detail="bad slug")`` — the
   global handler wraps the string into a single ErrorItem with
   ``path=null`` and ``code`` derived from the status code.

2. ``raise build_error(422, code="slug_invalid", message="...", path="$.name")``
   — explicit field-level error.

3. FastAPI's own request-body validation (Pydantic) — the override in
   ``main.py`` converts each Pydantic error into an ErrorItem with the
   JSON-Pointer path it produced.

The envelope is the contract every UI / agent client reads.  Clients
that previously read ``response.json()["detail"]`` migrate to
``response.json()["errors"][0]["message"]`` (or ``...[0]["code"]`` for
machine-readable handling).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel

from registry.exceptions import (
    CatalogError,
    ConflictError,
    LifecycleError,
    NotFoundError,
    TenantIsolationError,
    ValidationError,
    VocabularyError,
)

# Map HTTP status codes to a default machine-readable code.  Used when a
# router raises ``HTTPException(detail="..."``) without specifying its
# own code; the global handler reads this table to fill in ``code``.
_STATUS_TO_CODE: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    501: "not_implemented",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


class ErrorItem(BaseModel):
    """One row in the error envelope.

    ``path`` is a JSON Pointer ("$.name", "$.attributes.lifecycle.state")
    when the error is field-specific, ``None`` otherwise.

    ``code`` is the stable, machine-readable identifier clients
    program against. Don't localise or rephrase it across responses —
    that's what ``message`` is for.
    """

    path: str | None = None
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    errors: list[ErrorItem]


def build_error(
    status_code: int,
    *,
    code: str,
    message: str,
    path: str | None = None,
    extra: list[ErrorItem] | None = None,
) -> HTTPException:
    """Construct an HTTPException whose detail is a list-of-ErrorItem.

    The global handler in ``main.py`` recognises the list form and
    wraps it into the envelope ``{"errors": [...]}``.  Routers should
    prefer this helper to ``HTTPException(detail=...)`` when they have
    field-specific information.
    """
    items: list[dict[str, Any]] = [
        {"path": path, "code": code, "message": message},
    ]
    if extra:
        items.extend(item.model_dump() for item in extra)
    return HTTPException(status_code=status_code, detail=items)


def coerce_to_envelope(status_code: int, detail: Any) -> dict[str, Any]:
    """Normalise *detail* into the envelope shape.

    Cases the global handler must handle:
    1. ``detail`` is already a list of ErrorItem-shaped dicts (from
       ``build_error``) — return as-is, wrapped.
    2. ``detail`` is a single ErrorItem-shaped dict — wrap in a list.
    3. ``detail`` is a string (legacy ``raise HTTPException(detail=...)``)
       — synthesise one ErrorItem with ``code`` defaulted from the
       status code.
    4. ``detail`` is anything else (None, a list of strings, etc.) —
       coerce to a single-item envelope keyed off ``str(detail)``.
    """
    default_code = _STATUS_TO_CODE.get(status_code, "error")

    if isinstance(detail, list) and detail and all(_looks_like_item(d) for d in detail):
        # Pre-shaped items: keep them, just wrap.
        return {"errors": [_normalise_item(d, default_code) for d in detail]}

    if isinstance(detail, dict) and _looks_like_item(detail):
        return {"errors": [_normalise_item(detail, default_code)]}

    message = "" if detail is None else str(detail)
    return {
        "errors": [
            {"path": None, "code": default_code, "message": message},
        ],
    }


def _looks_like_item(value: Any) -> bool:
    return isinstance(value, dict) and "message" in value and "code" in value


_RESERVED_ITEM_KEYS: frozenset[str] = frozenset({"path", "code", "message"})


def _normalise_item(value: dict[str, Any], default_code: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": value.get("path"),
        "code": value.get("code") or default_code,
        "message": str(value.get("message", "")),
    }
    # Preserve any additional fields callers attached to the detail dict
    # (e.g. ``available_tenants`` from the multi-grant 400 response).
    # Only the three reserved keys above are normalised; everything else
    # passes through verbatim.
    for key, val in value.items():
        if key not in _RESERVED_ITEM_KEYS:
            item[key] = val
    return item


def map_catalog_error(exc: Exception) -> HTTPException:
    """Convert a catalog-domain exception into an ``HTTPException``.

    One helper, called from every router. Replaces the per-router
    ``_map_error`` clones that drifted in coverage (some handled
    ``TenantIsolationError``, others didn't; some handled
    ``PermissionError``, others didn't). The mapping here is the
    superset.

    The ``TenantIsolationError`` mapping returns ``"not found"`` — not
    ``str(exc)`` — so cross-tenant existence isn't leaked through the
    error message.
    """
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, TenantIsolationError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ValidationError | VocabularyError | LifecycleError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, CatalogError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    # Non-CatalogError exceptions still get a 400; the caller chose to
    # route through this helper rather than let the exception propagate.
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


__all__ = [
    "ErrorEnvelope",
    "ErrorItem",
    "build_error",
    "coerce_to_envelope",
    "map_catalog_error",
]
