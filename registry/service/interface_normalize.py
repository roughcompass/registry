"""Interface normalizer — converts capability interface declarations to a canonical shape.

Normalises a capability's interface declaration into the canonical
:class:`~registry.types.InterfaceSurface` shape. Three input formats are
accepted:

* ``json_schema`` — a JSON Schema object. ``properties`` are flattened
  into ``fields``; an optional top-level ``operations`` array maps
  through unchanged.
* ``openapi`` — an OpenAPI 3.x document. ``paths.*.{method}`` become
  ``operations``; parameters and request bodies become each
  operation's params; response schema becomes ``returns``.
* ``typescript`` — a restricted subset. Only ``type X = { field: T; }``
  and ``interface X { field: T; }`` are accepted, where ``T`` is one of
  ``string | number | boolean | null`` or a ``T[]``. Unions, generics,
  intersections, methods, and anything else → 422.

The diff engine operates exclusively on the canonical shape, so this
module is the single funnel point for surface representations.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from registry.exceptions import ValidationError
from registry.types import InterfaceSurface

_log = logging.getLogger(__name__)

#: Allowed input formats.
VALID_FORMATS: frozenset[str] = frozenset({"json_schema", "typescript", "openapi"})

#: Primitive TypeScript types accepted by the restricted parser.
_TS_PRIMITIVES: frozenset[str] = frozenset({"string", "number", "boolean", "null"})

#: ``identifier: type;`` lines inside a TypeScript body block.
#: identifier (\w+), optional ?, type (one of primitives or primitives[]).
_TS_FIELD_RE = re.compile(r"^\s*(?P<name>\w+)\s*(?P<opt>\??)\s*:\s*" r"(?P<type>\w+(?:\[\])?)\s*$")

#: ``type X = { ... }`` or ``interface X { ... }`` block matcher.
_TS_BLOCK_RE = re.compile(
    r"(?:type\s+(\w+)\s*=\s*|interface\s+(\w+)\s*)\{(?P<body>[^{}]*)\}",
    flags=re.MULTILINE | re.DOTALL,
)

_TS_UNSUPPORTED_MSG = (
    "TypeScript interface parsing supports simple property types only "
    "(type X = {...} or interface X {...} with primitive fields). "
    "Got unsupported syntax."
)


def normalize(
    raw_interface: dict[str, Any] | str,
    interface_format: str,
) -> InterfaceSurface:
    """Public entry point — see module docstring.

    Raises :class:`ValidationError` (→ 422 at the REST layer) on any
    parse failure or unsupported syntax.
    """
    if interface_format not in VALID_FORMATS:
        raise ValidationError(f"interface_format must be one of {sorted(VALID_FORMATS)}, " f"got {interface_format!r}")

    if interface_format == "json_schema":
        return _normalize_json_schema(raw_interface)
    if interface_format == "openapi":
        return _normalize_openapi(raw_interface)
    return _normalize_typescript(raw_interface)


# ---------------------------------------------------------------------------
# json_schema
# ---------------------------------------------------------------------------


def _normalize_json_schema(raw: dict[str, Any] | str) -> InterfaceSurface:
    """Accept a JSON Schema or its string-serialised form.

    Maps ``properties`` → ``fields``; an optional top-level ``operations``
    list passes through unchanged; an optional top-level ``events`` list
    passes through unchanged.
    """
    doc = _coerce_json_object(raw, "json_schema")
    fields: list[dict[str, Any]] = []
    required = set(doc.get("required") or [])
    props = doc.get("properties") or {}
    if not isinstance(props, dict):
        raise ValidationError("json_schema 'properties' must be an object, got " f"{type(props).__name__}")
    for name, spec in props.items():
        if not isinstance(spec, dict):
            raise ValidationError(f"json_schema property {name!r} must be an object")
        fields.append(
            {
                "name": name,
                "type": spec.get("type") or "unknown",
                "required": name in required,
            }
        )

    operations = _coerce_list(doc.get("operations"), "operations")
    events = _coerce_list(doc.get("events"), "events")
    return InterfaceSurface(operations=operations, events=events, fields=fields)


# ---------------------------------------------------------------------------
# openapi 3.x
# ---------------------------------------------------------------------------


def _normalize_openapi(raw: dict[str, Any] | str) -> InterfaceSurface:
    """Extract operations from an OpenAPI 3.x document.

    Each ``paths.{path}.{method}`` operation produces one entry in
    ``operations``. The ``method`` is upper-cased; ``name`` is the
    ``operationId`` if present, otherwise ``{METHOD} {path}``. Params
    combine path/query parameters with request-body schemas; ``returns``
    is the 2xx success schema's type when one is declared.
    """
    doc = _coerce_json_object(raw, "openapi")
    openapi_version = doc.get("openapi") or doc.get("swagger") or ""
    if not str(openapi_version).startswith(("3.", "3")):
        raise ValidationError(
            f"openapi 3.x required (got openapi={openapi_version!r}). "
            "Convert older Swagger 2 docs to OpenAPI 3 before submitting."
        )

    paths = doc.get("paths")
    if paths is None:
        paths = {}
    if not isinstance(paths, dict):
        raise ValidationError("openapi 'paths' must be an object")

    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations: list[dict[str, Any]] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in methods or not isinstance(op, dict):
                continue
            name = op.get("operationId") or f"{method.upper()} {path}"
            params = _openapi_params(op)
            returns = _openapi_return_type(op)
            operations.append(
                {
                    "name": name,
                    "method": method.upper(),
                    "path": path,
                    "params": params,
                    "returns": returns,
                }
            )

    return InterfaceSurface(operations=operations, events=[], fields=[])


def _openapi_params(op: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in op.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        schema = p.get("schema") or {}
        out.append(
            {
                "name": p.get("name"),
                "type": schema.get("type") if isinstance(schema, dict) else None,
                "required": bool(p.get("required")),
                "in": p.get("in"),
            }
        )
    rb = op.get("requestBody") or {}
    if isinstance(rb, dict):
        content = rb.get("content") or {}
        for media_type, media in content.items() if isinstance(content, dict) else []:
            if not isinstance(media, dict):
                continue
            schema = media.get("schema") or {}
            out.append(
                {
                    "name": "body",
                    "type": schema.get("type") if isinstance(schema, dict) else None,
                    "required": bool(rb.get("required")),
                    "in": "body",
                    "content_type": media_type,
                }
            )
    return out


def _openapi_return_type(op: dict[str, Any]) -> str | None:
    responses = op.get("responses") or {}
    if not isinstance(responses, dict):
        return None
    # Prefer the first declared 2xx response with a typed schema.
    for code in ("200", "201", "202", "204", "default"):
        spec = responses.get(code)
        if not isinstance(spec, dict):
            continue
        content = spec.get("content") or {}
        if not isinstance(content, dict):
            continue
        for media in content.values():
            if not isinstance(media, dict):
                continue
            schema = media.get("schema")
            if isinstance(schema, dict) and schema.get("type"):
                return str(schema["type"])
    return None


# ---------------------------------------------------------------------------
# typescript (restricted subset)
# ---------------------------------------------------------------------------


def _normalize_typescript(raw: dict[str, Any] | str) -> InterfaceSurface:
    """Restricted TypeScript parser.

    Accepts ``type X = { f: P; }`` and ``interface X { f: P; }`` where
    each ``P`` is a primitive (string|number|boolean|null) or a
    ``P[]`` array. Anything else (generics, unions, methods, nested
    objects) → :class:`ValidationError`.
    """
    if isinstance(raw, dict):
        # Allow the caller to pass {"source": "..."} for symmetry with
        # the other formats; the rest of the dict is ignored.
        source = raw.get("source")
        if not isinstance(source, str):
            raise ValidationError("typescript input must be a string or {source: '...'}")
    elif isinstance(raw, str):
        source = raw
    else:
        raise ValidationError(f"typescript input must be a string, got {type(raw).__name__}")

    blocks = list(_TS_BLOCK_RE.finditer(source))
    if not blocks:
        raise ValidationError(_TS_UNSUPPORTED_MSG)

    # Validate that the trimmed source is *only* the blocks we matched
    # (no extra TypeScript constructs sneaking through).
    stripped = _TS_BLOCK_RE.sub("", source).strip()
    if stripped:
        raise ValidationError(_TS_UNSUPPORTED_MSG)

    fields: list[dict[str, Any]] = []
    for m in blocks:
        body = m.group("body")
        # Statements end with ';' or newline — split on both so single-line
        # bodies (``{ id: string; active: boolean; }``) and multi-line
        # bodies parse identically.
        for chunk in re.split(r"[;\n]", body):
            line = chunk.strip()
            if not line:
                continue
            field = _parse_ts_field(line)
            fields.append(field)

    if not fields:
        raise ValidationError(_TS_UNSUPPORTED_MSG)

    return InterfaceSurface(operations=[], events=[], fields=fields)


def _parse_ts_field(line: str) -> dict[str, Any]:
    """Parse one ``identifier: type;`` line into a canonical field dict."""
    m = _TS_FIELD_RE.match(line)
    if m is None:
        raise ValidationError(_TS_UNSUPPORTED_MSG)
    ts_type = m.group("type")
    is_array = ts_type.endswith("[]")
    base = ts_type[:-2] if is_array else ts_type
    if base not in _TS_PRIMITIVES:
        raise ValidationError(_TS_UNSUPPORTED_MSG)
    canonical_type = f"{base}[]" if is_array else base
    return {
        "name": m.group("name"),
        "type": canonical_type,
        "required": m.group("opt") != "?",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_json_object(raw: dict[str, Any] | str, label: str) -> dict[str, Any]:
    """Accept a dict or a JSON string; everything else → 422."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{label} input is not valid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ValidationError(f"{label} input must decode to a JSON object")
        return decoded
    raise ValidationError(f"{label} input must be an object or JSON string, " f"got {type(raw).__name__}")


def _coerce_list(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"{label!r} must be a list when provided")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError(f"{label!r} entries must be objects; got {type(item).__name__}")
        out.append(item)
    return out


__all__ = [
    "VALID_FORMATS",
    "normalize",
]
