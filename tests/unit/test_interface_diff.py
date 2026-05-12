"""Unit tests for interface_diff.

Verifies the classification rules for interface changes:

- operation added             → non-breaking
- operation removed           → breaking
- operation input narrowed    → breaking (new required param OR type change)
- operation output widened    → non-breaking (None → typed)
- operation output undocumented → deprecation (typed → None)
- operation output type changed → breaking (typed → other typed)
- field added (optional)      → non-breaking
- field added (required)      → breaking
- field removed               → breaking
- field type changed          → breaking
- event added                 → non-breaking
- event removed               → breaking
- event payload narrowed      → breaking

Severity is the max over all individual changes:
``non-breaking < deprecation < breaking``.

Also covers:
- No changes → non-breaking + empty list.
- generate_release_notes_scaffold groups by change_type.
"""

from __future__ import annotations

from typing import Any

from registry.service.interface_diff import (
    BREAKING,
    DEPRECATION,
    NON_BREAKING,
    diff,
    generate_release_notes_scaffold,
)
from registry.types import InterfaceSurface


def _surface(
    *,
    operations: list[dict[str, Any]] | None = None,
    fields: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> InterfaceSurface:
    return InterfaceSurface(
        operations=operations or [],
        events=events or [],
        fields=fields or [],
    )


def _op(
    name: str,
    *,
    params: list[dict[str, Any]] | None = None,
    returns: str | None = "object",
    method: str = "POST",
) -> dict[str, Any]:
    return {
        "name": name,
        "method": method,
        "path": f"/{name}",
        "params": params or [],
        "returns": returns,
    }


# ---------------------------------------------------------------------------
# Identity / empty
# ---------------------------------------------------------------------------


def test_identical_surfaces_are_non_breaking() -> None:
    s = _surface(operations=[_op("createPayment")])
    sev, changes = diff(s, s)
    assert sev == NON_BREAKING
    assert changes == []


def test_empty_to_empty_is_non_breaking() -> None:
    sev, changes = diff(_surface(), _surface())
    assert sev == NON_BREAKING
    assert changes == []


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def test_operation_added_is_non_breaking() -> None:
    cur = _surface()
    prop = _surface(operations=[_op("ping")])
    sev, changes = diff(cur, prop)
    assert sev == NON_BREAKING
    assert any(c["change_type"] == "operation_added" for c in changes)


def test_operation_removed_is_breaking() -> None:
    cur = _surface(operations=[_op("cancelPayment")])
    prop = _surface()
    sev, changes = diff(cur, prop)
    assert sev == BREAKING
    assert any(c["change_type"] == "operation_removed" and c["name"] == "cancelPayment" for c in changes)


def test_operation_input_required_param_added_is_breaking() -> None:
    cur = _surface(operations=[_op("createPayment", params=[])])
    prop = _surface(
        operations=[
            _op(
                "createPayment",
                params=[{"name": "idempotency_key", "type": "string", "required": True}],
            )
        ]
    )
    sev, changes = diff(cur, prop)
    assert sev == BREAKING
    assert any(c["change_type"] == "operation_input_narrowed" for c in changes)


def test_operation_input_optional_param_added_is_non_breaking() -> None:
    cur = _surface(operations=[_op("x", params=[])])
    prop = _surface(operations=[_op("x", params=[{"name": "trace_id", "type": "string", "required": False}])])
    sev, _ = diff(cur, prop)
    assert sev == NON_BREAKING


def test_operation_param_required_flipped_to_true_is_breaking() -> None:
    cur = _surface(operations=[_op("x", params=[{"name": "p", "type": "string", "required": False}])])
    prop = _surface(operations=[_op("x", params=[{"name": "p", "type": "string", "required": True}])])
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


def test_operation_param_type_changed_is_breaking() -> None:
    cur = _surface(operations=[_op("x", params=[{"name": "p", "type": "string", "required": True}])])
    prop = _surface(operations=[_op("x", params=[{"name": "p", "type": "number", "required": True}])])
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


def test_operation_output_widened_is_non_breaking() -> None:
    # None → typed is widening the documented surface.
    cur = _surface(operations=[_op("x", returns=None)])
    prop = _surface(operations=[_op("x", returns="object")])
    sev, changes = diff(cur, prop)
    assert sev == NON_BREAKING
    assert any(c["change_type"] == "operation_output_widened" for c in changes)


def test_operation_output_undocumented_is_deprecation() -> None:
    cur = _surface(operations=[_op("x", returns="object")])
    prop = _surface(operations=[_op("x", returns=None)])
    sev, changes = diff(cur, prop)
    assert sev == DEPRECATION
    assert any(c["change_type"] == "operation_output_undocumented" for c in changes)


def test_operation_output_type_changed_is_breaking() -> None:
    cur = _surface(operations=[_op("x", returns="object")])
    prop = _surface(operations=[_op("x", returns="array")])
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def test_field_added_optional_is_non_breaking() -> None:
    cur = _surface(fields=[{"name": "id", "type": "string", "required": True}])
    prop = _surface(
        fields=[
            {"name": "id", "type": "string", "required": True},
            {"name": "label", "type": "string", "required": False},
        ]
    )
    sev, _ = diff(cur, prop)
    assert sev == NON_BREAKING


def test_field_added_required_is_breaking() -> None:
    cur = _surface(fields=[{"name": "id", "type": "string", "required": True}])
    prop = _surface(
        fields=[
            {"name": "id", "type": "string", "required": True},
            {"name": "tenant_id", "type": "string", "required": True},
        ]
    )
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


def test_field_removed_is_breaking() -> None:
    cur = _surface(fields=[{"name": "x", "type": "string", "required": False}])
    prop = _surface()
    sev, changes = diff(cur, prop)
    assert sev == BREAKING
    assert any(c["change_type"] == "field_removed" for c in changes)


def test_field_type_changed_is_breaking() -> None:
    cur = _surface(fields=[{"name": "x", "type": "string", "required": True}])
    prop = _surface(fields=[{"name": "x", "type": "number", "required": True}])
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


def test_field_required_flipped_to_true_is_breaking() -> None:
    cur = _surface(fields=[{"name": "x", "type": "string", "required": False}])
    prop = _surface(fields=[{"name": "x", "type": "string", "required": True}])
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_event_added_is_non_breaking() -> None:
    cur = _surface()
    prop = _surface(events=[{"name": "paid", "payload_fields": []}])
    sev, _ = diff(cur, prop)
    assert sev == NON_BREAKING


def test_event_removed_is_breaking() -> None:
    cur = _surface(events=[{"name": "paid", "payload_fields": []}])
    prop = _surface()
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


def test_event_payload_narrowed_is_breaking() -> None:
    cur = _surface(
        events=[
            {
                "name": "paid",
                "payload_fields": [
                    {"name": "amount", "type": "number"},
                    {"name": "currency", "type": "string"},
                ],
            }
        ]
    )
    prop = _surface(
        events=[
            {
                "name": "paid",
                "payload_fields": [{"name": "amount", "type": "number"}],
            }
        ]
    )
    sev, changes = diff(cur, prop)
    assert sev == BREAKING
    assert any(c["change_type"] == "event_payload_narrowed" for c in changes)


# ---------------------------------------------------------------------------
# Severity escalation (mixed change types)
# ---------------------------------------------------------------------------


def test_severity_is_max_across_changes() -> None:
    cur = _surface(
        operations=[_op("a"), _op("b", returns="object")],
        fields=[{"name": "x", "type": "string", "required": False}],
    )
    prop = _surface(
        operations=[
            _op("a"),
            _op("b", returns=None),  # deprecation
            _op("c"),  # added — non-breaking
        ],
        fields=[],  # field removed — breaking
    )
    sev, _ = diff(cur, prop)
    assert sev == BREAKING


def test_severity_deprecation_alone() -> None:
    cur = _surface(operations=[_op("x", returns="object")])
    prop = _surface(operations=[_op("x", returns=None)])
    sev, _ = diff(cur, prop)
    assert sev == DEPRECATION


# ---------------------------------------------------------------------------
# Release notes scaffold
# ---------------------------------------------------------------------------


def test_release_notes_groups_by_change_type() -> None:
    cur = _surface(operations=[_op("delete_me"), _op("keep")])
    prop = _surface(operations=[_op("keep"), _op("new_thing")])
    sev, changes = diff(cur, prop)
    notes = generate_release_notes_scaffold(sev, changes)
    assert sev == BREAKING
    assert "Severity: breaking" in notes
    assert "operation_removed" in notes
    assert "operation_added" in notes
    assert "delete_me" in notes
    assert "new_thing" in notes


def test_release_notes_empty_changes() -> None:
    notes = generate_release_notes_scaffold(NON_BREAKING, [])
    assert "No interface changes detected" in notes
