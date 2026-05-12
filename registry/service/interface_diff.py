"""Interface diff engine — classifies changes between two interface surfaces.

Compares two :class:`~catalog.types.InterfaceSurface` objects produced
by :mod:`registry.service.interface_normalize` and classifies the
overall change as one of three severities:

* ``non-breaking`` — strictly additive (new operation, widened output).
* ``deprecation`` — soft warning (e.g., error-code set changed).
* ``breaking`` — anything removed, narrowed, or type-changed.

The classification is the **maximum severity across all individual
changes** (ranked ``non-breaking < deprecation < breaking``).

The companion list-of-changes documents every per-element decision so
the breaking-change advisor (T20) and release-notes scaffolder can use
the same evidence the classifier did.
"""

from __future__ import annotations

import logging
from typing import Any

from registry.types import InterfaceSurface

_log = logging.getLogger(__name__)

#: Classification labels, ordered low → high severity.
NON_BREAKING = "non-breaking"
DEPRECATION = "deprecation"
BREAKING = "breaking"

_SEVERITY_RANK: dict[str, int] = {
    NON_BREAKING: 0,
    DEPRECATION: 1,
    BREAKING: 2,
}


def _escalate(current: str, candidate: str) -> str:
    """Return the higher-severity label of *current* vs *candidate*."""
    if _SEVERITY_RANK[candidate] > _SEVERITY_RANK[current]:
        return candidate
    return current


def diff(current: InterfaceSurface, proposed: InterfaceSurface) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(severity, changes)`` for the *current → proposed* delta.

    *changes* entries always carry ``{name, change_type, details}`` where
    ``change_type`` is one of the constants emitted below
    (``operation_added``, ``operation_removed``, ``operation_input_narrowed``,
    etc.) and ``details`` is a dict with the per-change evidence.
    """
    changes: list[dict[str, Any]] = []
    severity = NON_BREAKING

    severity = _escalate(severity, _diff_operations(current, proposed, changes))
    severity = _escalate(severity, _diff_fields(current, proposed, changes))
    severity = _escalate(severity, _diff_events(current, proposed, changes))

    return severity, changes


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _diff_operations(
    current: InterfaceSurface,
    proposed: InterfaceSurface,
    changes: list[dict[str, Any]],
) -> str:
    """Compare ``operations`` and append findings to *changes*."""
    cur_ops = {op.get("name"): op for op in current.operations if op.get("name")}
    prop_ops = {op.get("name"): op for op in proposed.operations if op.get("name")}

    severity = NON_BREAKING

    # Removed operations → breaking.
    for name in cur_ops.keys() - prop_ops.keys():
        changes.append(
            {
                "name": name,
                "change_type": "operation_removed",
                "details": {"operation": cur_ops[name]},
            }
        )
        severity = _escalate(severity, BREAKING)

    # Added operations → non-breaking.
    for name in prop_ops.keys() - cur_ops.keys():
        changes.append(
            {
                "name": name,
                "change_type": "operation_added",
                "details": {"operation": prop_ops[name]},
            }
        )

    # Changed operations.
    for name in cur_ops.keys() & prop_ops.keys():
        sub = _diff_one_operation(name, cur_ops[name], prop_ops[name], changes)
        severity = _escalate(severity, sub)

    return severity


def _diff_one_operation(
    name: str,
    current: dict[str, Any],
    proposed: dict[str, Any],
    changes: list[dict[str, Any]],
) -> str:
    """Compare params + return type of a single operation."""
    severity = NON_BREAKING

    cur_params = {p.get("name"): p for p in (current.get("params") or [])}
    prop_params = {p.get("name"): p for p in (proposed.get("params") or [])}

    # Input narrowed: any required param added, or required-flag flipped to true.
    for pname in prop_params.keys() - cur_params.keys():
        if prop_params[pname].get("required"):
            changes.append(
                {
                    "name": f"{name}.{pname}",
                    "change_type": "operation_input_narrowed",
                    "details": {"reason": "new required param"},
                }
            )
            severity = _escalate(severity, BREAKING)

    for pname in cur_params.keys() & prop_params.keys():
        cur_req = bool(cur_params[pname].get("required"))
        prop_req = bool(prop_params[pname].get("required"))
        cur_type = cur_params[pname].get("type")
        prop_type = prop_params[pname].get("type")
        if (not cur_req) and prop_req:
            changes.append(
                {
                    "name": f"{name}.{pname}",
                    "change_type": "operation_input_narrowed",
                    "details": {"reason": "param required flipped to true"},
                }
            )
            severity = _escalate(severity, BREAKING)
        if cur_type != prop_type and cur_type is not None and prop_type is not None:
            changes.append(
                {
                    "name": f"{name}.{pname}",
                    "change_type": "operation_input_type_changed",
                    "details": {"from": cur_type, "to": prop_type},
                }
            )
            severity = _escalate(severity, BREAKING)

    # Param removal: widens input → non-breaking (clients sending the old
    # field will fail validation if the server rejects unknown fields, but
    # that's a server-side policy decision, not a contract change).
    for pname in cur_params.keys() - prop_params.keys():
        changes.append(
            {
                "name": f"{name}.{pname}",
                "change_type": "operation_input_widened",
                "details": {"reason": "param removed"},
            }
        )

    # Return-type changes.
    cur_ret = current.get("returns")
    prop_ret = proposed.get("returns")
    if cur_ret != prop_ret:
        if cur_ret is None and prop_ret is not None:
            changes.append(
                {
                    "name": name,
                    "change_type": "operation_output_widened",
                    "details": {"from": None, "to": prop_ret},
                }
            )
        elif cur_ret is not None and prop_ret is None:
            # Removing a documented return type is a breaking docstring
            # change at minimum. Mark as deprecation (warn, don't block).
            changes.append(
                {
                    "name": name,
                    "change_type": "operation_output_undocumented",
                    "details": {"from": cur_ret, "to": None},
                }
            )
            severity = _escalate(severity, DEPRECATION)
        else:
            changes.append(
                {
                    "name": name,
                    "change_type": "operation_output_type_changed",
                    "details": {"from": cur_ret, "to": prop_ret},
                }
            )
            severity = _escalate(severity, BREAKING)

    return severity


# ---------------------------------------------------------------------------
# Fields (JSON-Schema / TypeScript surface)
# ---------------------------------------------------------------------------


def _diff_fields(
    current: InterfaceSurface,
    proposed: InterfaceSurface,
    changes: list[dict[str, Any]],
) -> str:
    cur_fields = {f.get("name"): f for f in current.fields if f.get("name")}
    prop_fields = {f.get("name"): f for f in proposed.fields if f.get("name")}
    severity = NON_BREAKING

    for name in cur_fields.keys() - prop_fields.keys():
        changes.append(
            {
                "name": name,
                "change_type": "field_removed",
                "details": {"field": cur_fields[name]},
            }
        )
        severity = _escalate(severity, BREAKING)

    for name in prop_fields.keys() - cur_fields.keys():
        # Adding a required field tightens input → breaking; optional → ok.
        if prop_fields[name].get("required"):
            changes.append(
                {
                    "name": name,
                    "change_type": "field_added_required",
                    "details": {"field": prop_fields[name]},
                }
            )
            severity = _escalate(severity, BREAKING)
        else:
            changes.append(
                {
                    "name": name,
                    "change_type": "field_added",
                    "details": {"field": prop_fields[name]},
                }
            )

    for name in cur_fields.keys() & prop_fields.keys():
        cur = cur_fields[name]
        prop = prop_fields[name]
        if cur.get("type") != prop.get("type"):
            changes.append(
                {
                    "name": name,
                    "change_type": "field_type_changed",
                    "details": {"from": cur.get("type"), "to": prop.get("type")},
                }
            )
            severity = _escalate(severity, BREAKING)
        if not cur.get("required") and prop.get("required"):
            changes.append(
                {
                    "name": name,
                    "change_type": "field_required_added",
                    "details": {"reason": "optional → required"},
                }
            )
            severity = _escalate(severity, BREAKING)

    return severity


# ---------------------------------------------------------------------------
# Events (subscription emissions)
# ---------------------------------------------------------------------------


def _diff_events(
    current: InterfaceSurface,
    proposed: InterfaceSurface,
    changes: list[dict[str, Any]],
) -> str:
    cur = {e.get("name"): e for e in current.events if e.get("name")}
    prop = {e.get("name"): e for e in proposed.events if e.get("name")}
    severity = NON_BREAKING

    for name in cur.keys() - prop.keys():
        changes.append(
            {
                "name": name,
                "change_type": "event_removed",
                "details": {"event": cur[name]},
            }
        )
        severity = _escalate(severity, BREAKING)

    for name in prop.keys() - cur.keys():
        changes.append(
            {
                "name": name,
                "change_type": "event_added",
                "details": {"event": prop[name]},
            }
        )

    for name in cur.keys() & prop.keys():
        # Event payload narrowing: required field added to payload schema.
        cur_fields = {f.get("name"): f for f in (cur[name].get("payload_fields") or [])}
        prop_fields = {f.get("name"): f for f in (prop[name].get("payload_fields") or [])}
        if cur_fields.keys() - prop_fields.keys():
            changes.append(
                {
                    "name": name,
                    "change_type": "event_payload_narrowed",
                    "details": {
                        "removed_fields": list(cur_fields.keys() - prop_fields.keys()),
                    },
                }
            )
            severity = _escalate(severity, BREAKING)

    return severity


def generate_release_notes_scaffold(severity: str, changes: list[dict[str, Any]]) -> str:
    """Build a plain-text release-notes draft from a diff result.

    Used by the breaking-change advisor (T20) so producers see what the
    diff classifier saw — same evidence, no separate path.
    """
    sev_line = f"# Severity: {severity}\n"
    if not changes:
        return sev_line + "\nNo interface changes detected.\n"

    buckets: dict[str, list[dict[str, Any]]] = {}
    for c in changes:
        buckets.setdefault(c["change_type"], []).append(c)

    lines: list[str] = [sev_line, ""]
    for change_type, entries in sorted(buckets.items()):
        lines.append(f"## {change_type} ({len(entries)})")
        for entry in entries:
            lines.append(f"- {entry['name']}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "BREAKING",
    "DEPRECATION",
    "NON_BREAKING",
    "diff",
    "generate_release_notes_scaffold",
]
