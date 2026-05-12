"""Unit tests for BreakingChangeAdvisor.

Covers the advisor's responsibilities in isolation (the integration
test lives in tests/integration/test_breaking_change_advisor.py):

- Semver validation runs before normalize (cheapest failure first).
- Visibility chokepoint: invisible capability → PermissionError.
- diff_classification === interface_diff result.
- affected_consumers: empty for non-breaking with no consumer-impacting
  changes; populated for breaking; cross-tenant entries anonymised.
- version_pin satisfaction: ``^2.0`` rejected by ``3.0.0``; satisfied by ``2.4.1``.
- release_notes_scaffold includes severity header.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import ValidationError
from registry.service.breaking_change import (
    BreakingChangeAdvisor,
    _adoption_in_scope,
)
from registry.service.interface_diff import BREAKING, NON_BREAKING
from registry.service.version_predicates import evaluate_version_predicate
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()
_TENANT_B = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CAP = uuid.uuid4()


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT_A, actor_id=_ACTOR, roles=["producer"])


def _async_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_factory(
    *,
    current_surface_attr: dict | None = None,
    adoption_pins: dict[uuid.UUID, str | None] | None = None,
):
    async def _execute(stmt: Any, params: dict | None = None):
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "FROM attributes" in sql and "interface_canonical" in (params or {}).get("k", ""):
            if current_surface_attr is None:
                result.first = MagicMock(return_value=None)
            else:
                row = MagicMock(value=current_surface_attr)
                result.first = MagicMock(return_value=row)
            return result
        if "FROM adoption_events" in sql:
            rows = [MagicMock(consumer_tenant_id=tid, version_pin=pin) for tid, pin in (adoption_pins or {}).items()]
            result.all = MagicMock(return_value=rows)
            return result
        result.first = MagicMock(return_value=None)
        result.all = MagicMock(return_value=[])
        return result

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session)


def _make_visibility(allow: bool = True) -> MagicMock:
    vis = MagicMock()
    if allow:
        vis.assert_visible = AsyncMock(return_value=None)
    else:
        vis.assert_visible = AsyncMock(side_effect=PermissionError("invisible"))
    return vis


def _make_retrieval(consumer_nodes: list[Any] | None = None) -> MagicMock:
    """Return a RetrievalService mock that yields ``consumer_nodes`` for traversal."""
    svc = MagicMock()
    traversal = MagicMock()
    traversal.nodes = consumer_nodes or []
    svc.get_reverse_traversal = AsyncMock(return_value=traversal)
    return svc


def _consumer_node(tenant_id: uuid.UUID, name: str = "consumer-cap") -> MagicMock:
    node = MagicMock()
    node.entity_id = uuid.uuid4()
    node.tenant_id = tenant_id
    node.name = name
    return node


# ---------------------------------------------------------------------------
# Semver validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_proposed_version_raises_validation_error() -> None:
    advisor = BreakingChangeAdvisor(
        session_factory=_make_factory(),
        clock=FakeClock(_NOW),
        retrieval=_make_retrieval(),
        visibility=_make_visibility(),
    )
    with pytest.raises(ValidationError):
        await advisor.preview_version(
            ctx=_ctx(),
            capability_id=_CAP,
            proposed_version="latest",
            proposed_interface={"type": "object", "properties": {}},
            interface_format="json_schema",
        )


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invisible_capability_raises_permission_error() -> None:
    advisor = BreakingChangeAdvisor(
        session_factory=_make_factory(),
        clock=FakeClock(_NOW),
        retrieval=_make_retrieval(),
        visibility=_make_visibility(allow=False),
    )
    with pytest.raises(PermissionError):
        await advisor.preview_version(
            ctx=_ctx(),
            capability_id=_CAP,
            proposed_version="1.0.0",
            proposed_interface={"type": "object"},
            interface_format="json_schema",
        )


# ---------------------------------------------------------------------------
# Diff classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_breaking_with_no_current_returns_empty_affected_consumers() -> None:
    advisor = BreakingChangeAdvisor(
        session_factory=_make_factory(),  # no current surface
        clock=FakeClock(_NOW),
        retrieval=_make_retrieval([_consumer_node(_TENANT_B)]),
        visibility=_make_visibility(),
    )
    preview = await advisor.preview_version(
        ctx=_ctx(),
        capability_id=_CAP,
        proposed_version="1.0.0",
        proposed_interface={"type": "object", "properties": {}},
        interface_format="json_schema",
    )
    # No current → diff is empty / non-breaking; no consumers in scope.
    assert preview.diff_classification == NON_BREAKING
    assert preview.affected_consumers == []


@pytest.mark.asyncio
async def test_breaking_change_includes_same_tenant_consumer_with_full_identity() -> None:
    # Current surface declares two fields; proposed drops one.
    current = {
        "operations": [],
        "events": [],
        "fields": [
            {"name": "id", "type": "string", "required": True},
            {"name": "amount", "type": "number", "required": True},
        ],
    }
    consumer_node = _consumer_node(_TENANT_A, "internal-consumer")
    advisor = BreakingChangeAdvisor(
        session_factory=_make_factory(current_surface_attr=current),
        clock=FakeClock(_NOW),
        retrieval=_make_retrieval([consumer_node]),
        visibility=_make_visibility(),
    )
    preview = await advisor.preview_version(
        ctx=_ctx(),
        capability_id=_CAP,
        proposed_version="2.0.0",
        proposed_interface={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
        interface_format="json_schema",
    )
    assert preview.diff_classification == BREAKING
    assert len(preview.affected_consumers) == 1
    # Same-tenant consumer → full identifier (no anonymisation).
    assert preview.affected_consumers[0]["tenant_id"] == str(_TENANT_A)
    assert preview.affected_consumers[0]["name"] == "internal-consumer"


@pytest.mark.asyncio
async def test_cross_tenant_consumer_is_anonymised_via_adoption_events() -> None:
    """Cross-tenant consumers come from adoption_events (the canonical
    cross-tenant dep record); each is anonymised with opaque identifiers."""
    current = {
        "operations": [],
        "events": [],
        "fields": [{"name": "x", "type": "string", "required": True}],
    }
    advisor = BreakingChangeAdvisor(
        session_factory=_make_factory(
            current_surface_attr=current,
            adoption_pins={_TENANT_B: "^1.0.0"},
        ),
        clock=FakeClock(_NOW),
        retrieval=_make_retrieval([]),  # no same-tenant consumers
        visibility=_make_visibility(),
    )
    preview = await advisor.preview_version(
        ctx=_ctx(),
        capability_id=_CAP,
        proposed_version="2.0.0",
        proposed_interface={"type": "object", "properties": {}},
        interface_format="json_schema",
    )
    assert preview.diff_classification == BREAKING
    assert len(preview.affected_consumers) == 1
    entry = preview.affected_consumers[0]
    # Cross-tenant consumers use an opaque tenant counter + hashed entity_id.
    assert entry["tenant_id"].startswith("cross-tenant-")
    assert entry["entity_id"].startswith("opaque-")
    assert entry["name"] is None
    # The real tenant UUID never appears.
    assert str(_TENANT_B) not in entry["tenant_id"]
    assert str(_TENANT_B) not in entry["entity_id"]


@pytest.mark.asyncio
async def test_release_notes_scaffold_contains_severity_header() -> None:
    advisor = BreakingChangeAdvisor(
        session_factory=_make_factory(),
        clock=FakeClock(_NOW),
        retrieval=_make_retrieval(),
        visibility=_make_visibility(),
    )
    preview = await advisor.preview_version(
        ctx=_ctx(),
        capability_id=_CAP,
        proposed_version="1.0.0",
        proposed_interface={"type": "object"},
        interface_format="json_schema",
    )
    assert preview.release_notes_scaffold.startswith("# Severity:")


# ---------------------------------------------------------------------------
# Previously-divergent semver semantics — now locked in via
# evaluate_version_predicate (the single canonical implementation).
# These cases exposed the bugs in the deleted _pin_satisfies helper.
# ---------------------------------------------------------------------------


class TestSemverEvaluationViaCanonicalImplementation:
    # --- Pre-1.0 caret: ^0.2 means >=0.2.0 <0.3.0, not >=0.2.0 <1.0.0 ---

    def test_caret_pre_1_0_matches_within_minor(self) -> None:
        # ^0.2 := >=0.2.0 <0.3.0
        assert evaluate_version_predicate("0.2.5", "^0.2") is True

    def test_caret_pre_1_0_excludes_next_minor(self) -> None:
        # ^0.2 stops at 0.3.0 — old _pin_satisfies only checked major
        assert evaluate_version_predicate("0.3.0", "^0.2") is False

    def test_caret_pre_1_0_excludes_below_lower(self) -> None:
        assert evaluate_version_predicate("0.1.9", "^0.2") is False

    # --- Tilde expansion: ~1.2.3 allows patch bumps, not minor bumps ---

    def test_tilde_patch_bump_accepted(self) -> None:
        # ~1.2.3 := >=1.2.3 <1.3.0
        assert evaluate_version_predicate("1.2.4", "~1.2.3") is True

    def test_tilde_minor_bump_excluded(self) -> None:
        # old _pin_satisfies locked to same-minor by checking minor == minor,
        # but did not enforce the upper bound correctly for all cases
        assert evaluate_version_predicate("1.3.0", "~1.2.3") is False

    # --- Leading-v stripping: v1.2.3 is a valid version ---

    def test_leading_v_accepted_as_version(self) -> None:
        # version_predicates strips leading v; old _pin_satisfies did not
        assert evaluate_version_predicate("v1.2.3", "^1.2") is True

    def test_leading_v_excluded_when_below_range(self) -> None:
        assert evaluate_version_predicate("v1.1.0", "^1.2") is False

    # --- Multi-clause comma ranges ---

    def test_comma_range_satisfied(self) -> None:
        assert evaluate_version_predicate("1.5.0", ">=1.0,<2.0") is True

    def test_comma_range_not_satisfied_upper(self) -> None:
        assert evaluate_version_predicate("2.0.0", ">=1.0,<2.0") is False

    def test_comma_range_not_satisfied_lower(self) -> None:
        assert evaluate_version_predicate("0.9.9", ">=1.0,<2.0") is False

    # --- Basic cases that must still hold after migration ---

    def test_caret_same_major_satisfied(self) -> None:
        assert evaluate_version_predicate("2.4.1", "^2.0.0") is True

    def test_caret_next_major_not_satisfied(self) -> None:
        assert evaluate_version_predicate("3.0.0", "^2.0.0") is False

    def test_exact_match(self) -> None:
        assert evaluate_version_predicate("2.4.1", "==2.4.1") is True
        assert evaluate_version_predicate("2.4.2", "==2.4.1") is False

    def test_empty_pin_is_satisfied(self) -> None:
        # Empty predicate = no constraint; always True.
        assert evaluate_version_predicate("1.0.0", "") is True

    def test_unknown_pin_reports_unsatisfied(self) -> None:
        # A malformed pin returns False from evaluate_version_predicate, so
        # _adoption_in_scope treats the consumer as in-scope (conservative:
        # the producer can read the pin and decide). This is strictly safer
        # than the old behavior that silently excluded the consumer.
        assert evaluate_version_predicate("1.0.0", "freeform-not-a-pin") is False


# ---------------------------------------------------------------------------
# _adoption_in_scope integration — verifies the callsite wiring
# ---------------------------------------------------------------------------


class TestAdoptionInScope:
    def test_breaking_always_includes_any_pin(self) -> None:
        assert _adoption_in_scope("^2.0.0", "3.0.0", BREAKING, []) is True

    def test_non_breaking_no_pin_excluded(self) -> None:
        assert _adoption_in_scope(None, "1.0.0", NON_BREAKING, []) is False

    def test_non_breaking_satisfied_pin_excluded(self) -> None:
        # Pin is satisfied → consumer is not in scope.
        assert _adoption_in_scope("^1.0", "1.5.0", NON_BREAKING, []) is False

    def test_non_breaking_unsatisfied_pin_included(self) -> None:
        # Pin fails proposed version → consumer is in scope.
        assert _adoption_in_scope("^1.0", "2.0.0", NON_BREAKING, []) is True
