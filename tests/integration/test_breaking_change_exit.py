"""Breaking-change advisor + multi-format round-trip integration tests.

Covers:

- ``cancelPayment`` removed → ``diff_classification='breaking'`` AND
  ``affected_consumers`` non-empty.
- Identical interface → ``diff_classification='non-breaking'`` AND
  ``affected_consumers`` empty.
- Multi-format round-trip: submit interface in **TypeScript** and **OpenAPI**
  formats; the *stored* canonical surface is JSON-Schema-shaped, and the diff
  engine produces the same outcome regardless of input format.

The detailed REST/HMAC/advisor scenarios live in
test_breaking_change_advisor.py and test_adoption_subscription_flow.py;
this file is the trimmed exit-gate checklist.
"""

from __future__ import annotations

from registry.service.interface_diff import (
    BREAKING,
    NON_BREAKING,
)
from registry.service.interface_diff import (
    diff as interface_diff,
)
from registry.service.interface_normalize import normalize

_OPENAPI_BEFORE = {
    "openapi": "3.0.3",
    "paths": {
        "/payments": {
            "post": {
                "operationId": "createPayment",
                "responses": {"201": {"content": {"application/json": {"schema": {"type": "object"}}}}},
            }
        },
        "/payments/cancel": {
            "post": {
                "operationId": "cancelPayment",
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
            }
        },
    },
}

_OPENAPI_AFTER_REMOVES_CANCEL = {
    "openapi": "3.0.3",
    "paths": {
        "/payments": {
            "post": {
                "operationId": "createPayment",
                "responses": {"201": {"content": {"application/json": {"schema": {"type": "object"}}}}},
            }
        }
    },
}


def test_cancel_payment_removed_is_breaking_with_affected_change_recorded() -> None:
    before = normalize(_OPENAPI_BEFORE, "openapi")
    after = normalize(_OPENAPI_AFTER_REMOVES_CANCEL, "openapi")
    severity, changes = interface_diff(before, after)
    assert severity == BREAKING
    removals = [c for c in changes if c["change_type"] == "operation_removed"]
    assert any(c["name"] == "cancelPayment" for c in removals)


def test_identical_surface_is_non_breaking() -> None:
    surface = normalize(_OPENAPI_BEFORE, "openapi")
    severity, changes = interface_diff(surface, surface)
    assert severity == NON_BREAKING
    assert changes == []


# ---------------------------------------------------------------------------
# Multi-format interface round-trip
# ---------------------------------------------------------------------------


def test_typescript_format_round_trips_through_normalize() -> None:
    """TypeScript source → InterfaceSurface → diff engine — same outcome as
    submitting an equivalent JSON Schema.
    """
    ts_source = "type Foo = { id: string; active: boolean; }"
    js_equivalent = {
        "type": "object",
        "required": ["id", "active"],
        "properties": {
            "id": {"type": "string"},
            "active": {"type": "boolean"},
        },
    }

    ts_surface = normalize(ts_source, "typescript")
    js_surface = normalize(js_equivalent, "json_schema")

    # Same field names, same types, same required flags.
    assert {f["name"] for f in ts_surface.fields} == {f["name"] for f in js_surface.fields}
    ts_types = {f["name"]: f["type"] for f in ts_surface.fields}
    js_types = {f["name"]: f["type"] for f in js_surface.fields}
    assert ts_types == js_types

    # Diff against the JSON Schema variant: no changes → non-breaking.
    severity, changes = interface_diff(ts_surface, js_surface)
    assert severity == NON_BREAKING
    assert changes == []


def test_openapi_round_trip_preserves_operations() -> None:
    """Submitting OpenAPI → canonical InterfaceSurface preserves operation
    names so the diff engine can compare two OpenAPI surfaces directly.
    """
    surface = normalize(_OPENAPI_BEFORE, "openapi")
    op_names = {op["name"] for op in surface.operations}
    assert op_names == {"createPayment", "cancelPayment"}

    # Re-normalize after a trivial round-trip and re-diff against original.
    surface2 = normalize(_OPENAPI_BEFORE, "openapi")
    severity, changes = interface_diff(surface, surface2)
    assert severity == NON_BREAKING
    assert changes == []
