"""Unit tests for ProgressionService.validate_transition and is_gate_satisfied.

All tests use cache_ttl_seconds=0 (no-cache path) and AsyncMock for the
session factory. The audit helper is spied on via patch to verify correct
event names and payloads are emitted.

Test map
--------
1.  No definition exists -> pass-through (valid=True, no audit).
2.  Valid sequential forward transition (gates satisfied) -> accepted, audit emitted.
3.  Sequential forward transition skipping a state (no tier_rules allowing it) -> rejected.
4.  forward="any" -> any state -> state allowed.
5.  Tier-conditional skip allows skipping a state for tier=T5 -> accepted.
6.  Re-entry to same state with `reason` attribute set -> accepted.
7.  Re-entry without `reason` (when requires=["reason"]) -> rejected.
8a. Gate attribute is True -> satisfied.
8b. Gate attribute is non-empty string -> satisfied.
8c. Gate attribute is {"at": "2026-05-12T10:00:00Z"} -> satisfied.
8d. Gate attribute absent -> not satisfied.
8e. Gate attribute is False -> not satisfied.
8f. Gate attribute is None -> not satisfied.
8g. Gate attribute is 0 -> not satisfied.
8h. Gate attribute is [] -> not satisfied.
9.  Advisory mode + failing gate -> valid=True, warnings populated, warned audit.
10. Enforcing mode + failing gate, no override -> ProgressionError raised, rejected audit.
11. Failing gate WITH matching unconsumed override -> accepted; consumed_at set; overridden audit.
12. Failing gate WITH matching CONSUMED override -> rejected (consumed_at not null disqualifies).
13. tier_not_resolvable - tier_rules defined, entity has no tier, no "default" key -> ProgressionError.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.progression import (
    ProgressionError,
    ProgressionService,
    is_gate_satisfied,
)
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime.datetime(2026, 5, 12, 10, 0, 0, tzinfo=datetime.UTC)


def _clock() -> FakeClock:
    return FakeClock(_FIXED_TS)


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
    )


@dataclass
class _Entity:
    """Minimal entity view for tests."""

    entity_id: uuid.UUID
    entity_type: str
    attributes: dict[str, Any]


def _entity(entity_type: str = "initiative", attributes: dict | None = None) -> _Entity:
    return _Entity(
        entity_id=uuid.uuid4(),
        entity_type=entity_type,
        attributes=attributes or {},
    )


def _definition(
    states: list[dict] | None = None,
    forward: str = "sequential",
    skip: str = "never",
    reentry: dict | None = None,
    tier_rules: dict | None = None,
) -> dict:
    """Build a minimal progression definition JSONB dict."""
    if states is None:
        states = [
            {"id": "1", "name": "Intake", "gates": []},
            {"id": "2", "name": "Discovery", "gates": []},
            {"id": "3", "name": "Build", "gates": []},
        ]
    defn: dict[str, Any] = {
        "states": states,
        "transitions": {
            "forward": forward,
            "skip": skip,
        },
    }
    if reentry is not None:
        defn["transitions"]["reentry"] = reentry
    if tier_rules is not None:
        defn["tier_rules"] = tier_rules
    return defn


def _make_defn_row(
    definition: dict,
    is_advisory: bool = False,
    progression_id: uuid.UUID | None = None,
) -> MagicMock:
    row = MagicMock()
    row.progression_id = progression_id or uuid.uuid4()
    row.definition = definition
    row.is_advisory = is_advisory
    return row


def _make_override(
    entity_id: uuid.UUID,
    from_state: str,
    to_state: str,
    gate_id: str = "*",
    consumed_at: datetime.datetime | None = None,
) -> MagicMock:
    row = MagicMock()
    row.override_id = uuid.uuid4()
    row.entity_id = entity_id
    row.from_state = from_state
    row.to_state = to_state
    row.gate_id = gate_id
    row.consumed_at = consumed_at
    row.authorized_by = uuid.uuid4()
    return row


def _async_noop_ctx() -> MagicMock:
    """Async context manager that does nothing — used for session.begin().

    session.begin() must return an async context manager object, not a
    coroutine. Using a plain MagicMock with async __aenter__/__aexit__ satisfies
    the `async with session.begin():` protocol without introducing a coroutine.
    """
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _session_no_definition() -> AsyncMock:
    """Session where the definition query returns None."""
    scalar = MagicMock()
    scalar.scalar_one_or_none = MagicMock(return_value=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar)
    # begin() must return a context manager, not a coroutine.
    session.begin = MagicMock(return_value=_async_noop_ctx())
    return session


def _session_with_definition(
    defn_row: MagicMock,
    override_row: MagicMock | None = None,
) -> AsyncMock:
    """Session that returns a definition on first query and an optional override on second."""
    defn_result = MagicMock()
    defn_result.scalar_one_or_none = MagicMock(return_value=defn_row)

    override_result = MagicMock()
    override_result.scalar_one_or_none = MagicMock(return_value=override_row)

    # audit INSERT returns a generic result
    audit_result = MagicMock()

    session = AsyncMock()
    # Order: definition select, then possibly override select, then audit inserts.
    session.execute = AsyncMock(side_effect=[defn_result, override_result, audit_result, audit_result, audit_result])
    session.flush = AsyncMock()
    # begin() must return a context manager, not a coroutine.
    session.begin = MagicMock(return_value=_async_noop_ctx())
    return session


def _make_service(session: AsyncMock) -> ProgressionService:
    """Build a ProgressionService with a mock session factory."""
    factory = MagicMock()
    # Support `async with factory() as session, session.begin(): ...`
    # factory() returns cm synchronously; cm.__aenter__ yields the session.
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = cm
    return ProgressionService(
        session_factory=factory,
        clock=_clock(),
        cache_ttl_seconds=0,
    )


# ---------------------------------------------------------------------------
# Gate satisfaction predicate tests (8a–8h)
# ---------------------------------------------------------------------------


class TestIsGateSatisfied:
    def test_8a_bool_true(self) -> None:
        assert is_gate_satisfied("g", {"g": True}) is True

    def test_8b_non_empty_string(self) -> None:
        assert is_gate_satisfied("g", {"g": "approved"}) is True

    def test_8c_non_empty_dict(self) -> None:
        assert is_gate_satisfied("g", {"g": {"at": "2026-05-12T10:00:00Z"}}) is True

    def test_8d_absent_key(self) -> None:
        assert is_gate_satisfied("g", {}) is False

    def test_8e_bool_false(self) -> None:
        assert is_gate_satisfied("g", {"g": False}) is False

    def test_8f_none(self) -> None:
        assert is_gate_satisfied("g", {"g": None}) is False

    def test_8g_zero(self) -> None:
        assert is_gate_satisfied("g", {"g": 0}) is False

    def test_8h_empty_list(self) -> None:
        assert is_gate_satisfied("g", {"g": []}) is False

    def test_positive_int_is_satisfied(self) -> None:
        assert is_gate_satisfied("g", {"g": 1}) is True

    def test_empty_dict_is_not_satisfied(self) -> None:
        assert is_gate_satisfied("g", {"g": {}}) is False

    def test_empty_string_is_not_satisfied(self) -> None:
        assert is_gate_satisfied("g", {"g": ""}) is False


# ---------------------------------------------------------------------------
# Transition tests
# ---------------------------------------------------------------------------


class TestValidateTransition:
    # ------------------------------------------------------------------
    # Test 1: no definition -> pass-through
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_1_no_definition_returns_valid(self) -> None:
        """When no active definition exists the service returns valid with no audit."""
        session = _session_no_definition()
        svc = _make_service(session)

        result = await svc.validate_transition(_ctx(), _entity(), "1", "2")

        assert result.valid is True
        assert result.warnings == []
        # Only one execute call (the definition lookup); no audit INSERT.
        assert session.execute.call_count == 1

    # ------------------------------------------------------------------
    # Test 2: valid sequential forward, gates satisfied -> accepted
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_2_valid_sequential_forward_accepted(self) -> None:
        """State 1->2 with no gates; accepted audit emitted."""
        defn = _definition(
            states=[
                {"id": "1", "name": "Intake", "gates": []},
                {"id": "2", "name": "Discovery", "gates": []},
            ]
        )
        defn_row = _make_defn_row(defn)
        session = _session_with_definition(defn_row)
        svc = _make_service(session)
        ctx = _ctx()

        result = await svc.validate_transition(ctx, _entity(), "1", "2")

        assert result.valid is True
        assert result.warnings == []
        # Execute calls: definition query + audit INSERT.
        # The override query is also called (second execute), then audit (third).
        # We verify the audit action by inspecting the SQL text of the last call.
        audit_sql = str(session.execute.call_args[0][0])
        assert "audit_log" in audit_sql
        audit_params = session.execute.call_args[0][1]
        assert audit_params["action"] == "progression.transition.accepted"

    # ------------------------------------------------------------------
    # Test 3: sequential skip with no tier_rules -> rejected
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_3_sequential_skip_without_tier_rules_rejected(self) -> None:
        """Jumping from state 1 to state 3 (skipping 2) without tier skip rules is rejected."""
        defn = _definition(
            states=[
                {"id": "1", "name": "Intake", "gates": []},
                {"id": "2", "name": "Discovery", "gates": []},
                {"id": "3", "name": "Build", "gates": []},
            ],
            forward="sequential",
            skip="never",
        )
        defn_row = _make_defn_row(defn, is_advisory=False)
        session = _session_with_definition(defn_row, override_row=None)
        svc = _make_service(session)

        with pytest.raises(ProgressionError):
            await svc.validate_transition(_ctx(), _entity(), "1", "3")

        audit_params = session.execute.call_args[0][1]
        assert audit_params["action"] == "progression.transition.rejected"

    # ------------------------------------------------------------------
    # Test 4: forward="any" allows any transition
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_4_forward_any_allows_any_state(self) -> None:
        """With forward=any, jumping from state 1 directly to state 3 is allowed."""
        defn = _definition(
            states=[
                {"id": "1", "name": "A", "gates": []},
                {"id": "2", "name": "B", "gates": []},
                {"id": "3", "name": "C", "gates": []},
            ],
            forward="any",
        )
        defn_row = _make_defn_row(defn)
        session = _session_with_definition(defn_row)
        svc = _make_service(session)

        result = await svc.validate_transition(_ctx(), _entity(), "1", "3")

        assert result.valid is True

    # ------------------------------------------------------------------
    # Test 5: tier-conditional skip for tier=T5 -> accepted
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_5_tier_conditional_skip_allowed_for_t5(self) -> None:
        """T5 entities may skip state 2 per tier_rules; 1->3 is valid."""
        defn = _definition(
            states=[
                {"id": "1", "name": "Intake", "gates": []},
                {"id": "2", "name": "Discovery", "gates": []},
                {"id": "3", "name": "Build", "gates": []},
            ],
            forward="sequential",
            skip="tier-conditional",
            tier_rules={
                "T5": {"required": ["1", "3"], "skip": ["2"]},
                "default": {"required": [], "skip": []},
            },
        )
        defn_row = _make_defn_row(defn)
        session = _session_with_definition(defn_row)
        svc = _make_service(session)
        entity = _entity(attributes={"tier": "T5"})

        result = await svc.validate_transition(_ctx(), entity, "1", "3")

        assert result.valid is True

    # ------------------------------------------------------------------
    # Test 6: re-entry with reason -> accepted
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_6_reentry_with_reason_accepted(self) -> None:
        """Same-state transition with reason attribute present is accepted."""
        defn = _definition(
            states=[{"id": "1", "name": "Intake", "gates": []}],
            reentry={"allowed": True, "requires": ["reason"]},
        )
        defn_row = _make_defn_row(defn)
        session = _session_with_definition(defn_row)
        svc = _make_service(session)
        entity = _entity(attributes={"reason": "updating scope document"})

        result = await svc.validate_transition(_ctx(), entity, "1", "1")

        assert result.valid is True

    # ------------------------------------------------------------------
    # Test 7: re-entry without reason -> rejected
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_7_reentry_without_reason_rejected(self) -> None:
        """Same-state transition when reason is required but absent is rejected."""
        defn = _definition(
            states=[{"id": "1", "name": "Intake", "gates": []}],
            reentry={"allowed": True, "requires": ["reason"]},
        )
        defn_row = _make_defn_row(defn, is_advisory=False)
        session = _session_with_definition(defn_row)
        svc = _make_service(session)
        entity = _entity(attributes={})  # no reason attribute

        with pytest.raises(ProgressionError):
            await svc.validate_transition(_ctx(), entity, "1", "1")

    # ------------------------------------------------------------------
    # Test 9: advisory mode + failing gate -> warned
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_9_advisory_failing_gate_warns(self) -> None:
        """Advisory mode: failing gate produces warnings, write still proceeds."""
        defn = _definition(
            states=[
                {"id": "1", "name": "Intake", "gates": []},
                {"id": "2", "name": "Review", "gates": ["arb-approved"]},
            ]
        )
        defn_row = _make_defn_row(defn, is_advisory=True)
        session = _session_with_definition(defn_row, override_row=None)
        svc = _make_service(session)
        entity = _entity(attributes={})  # arb-approved absent

        result = await svc.validate_transition(_ctx(), entity, "1", "2")

        assert result.valid is True
        assert len(result.warnings) > 0
        audit_params = session.execute.call_args[0][1]
        assert audit_params["action"] == "progression.transition.warned"

    # ------------------------------------------------------------------
    # Test 10: enforcing mode + failing gate, no override -> ProgressionError
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_10_enforcing_failing_gate_raises(self) -> None:
        """Enforcing mode: failing gate raises ProgressionError; rejected audit emitted."""
        defn = _definition(
            states=[
                {"id": "1", "name": "Intake", "gates": []},
                {"id": "2", "name": "Review", "gates": ["arb-approved"]},
            ]
        )
        defn_row = _make_defn_row(defn, is_advisory=False)
        session = _session_with_definition(defn_row, override_row=None)
        svc = _make_service(session)
        entity = _entity(attributes={})

        with pytest.raises(ProgressionError):
            await svc.validate_transition(_ctx(), entity, "1", "2")

        audit_params = session.execute.call_args[0][1]
        assert audit_params["action"] == "progression.transition.rejected"

    # ------------------------------------------------------------------
    # Test 11: failing gate WITH matching unconsumed override -> overridden
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_11_failing_gate_with_valid_override_accepted(self) -> None:
        """Matching unconsumed override: consumed_at set; overridden audit emitted."""
        defn = _definition(
            states=[
                {"id": "1", "name": "Intake", "gates": []},
                {"id": "2", "name": "Review", "gates": ["arb-approved"]},
            ]
        )
        defn_row = _make_defn_row(defn, is_advisory=False)
        entity = _entity(attributes={})
        override = _make_override(
            entity_id=entity.entity_id,
            from_state="1",
            to_state="2",
            gate_id="*",
            consumed_at=None,
        )
        session = _session_with_definition(defn_row, override_row=override)
        svc = _make_service(session)

        result = await svc.validate_transition(_ctx(), entity, "1", "2")

        assert result.valid is True
        # consumed_at should have been set on the override row.
        assert override.consumed_at == _FIXED_TS
        audit_params = session.execute.call_args[0][1]
        assert audit_params["action"] == "progression.transition.overridden"

    # ------------------------------------------------------------------
    # Test 12: failing gate WITH consumed override -> rejected
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_12_consumed_override_not_reapplied(self) -> None:
        """A previously consumed override (consumed_at IS NOT NULL) does not satisfy the check.

        The DB query filters consumed_at IS NULL, so a consumed override row
        will not be returned; the service treats this as no-override and rejects.
        """
        defn = _definition(
            states=[
                {"id": "1", "name": "Intake", "gates": []},
                {"id": "2", "name": "Review", "gates": ["arb-approved"]},
            ]
        )
        defn_row = _make_defn_row(defn, is_advisory=False)
        # The query filters consumed_at IS NULL, so return None to simulate
        # the DB correctly excluding the consumed row.
        session = _session_with_definition(defn_row, override_row=None)
        svc = _make_service(session)
        entity = _entity(attributes={})

        with pytest.raises(ProgressionError):
            await svc.validate_transition(_ctx(), entity, "1", "2")

        audit_params = session.execute.call_args[0][1]
        assert audit_params["action"] == "progression.transition.rejected"

    # ------------------------------------------------------------------
    # Test 13: tier_not_resolvable
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_13_tier_not_resolvable_raises(self) -> None:
        """tier_rules defined, entity has no tier, no default key -> ProgressionError."""
        defn = _definition(
            states=[
                {"id": "1", "name": "A", "gates": []},
                {"id": "2", "name": "B", "gates": []},
            ],
            skip="tier-conditional",
            tier_rules={
                "T5": {"required": [], "skip": []},
                # No "default" key.
            },
        )
        defn_row = _make_defn_row(defn, is_advisory=False)
        session = _session_with_definition(defn_row)
        svc = _make_service(session)
        entity = _entity(attributes={})  # no tier attribute

        with pytest.raises(ProgressionError, match="tier_not_resolvable"):
            await svc.validate_transition(_ctx(), entity, "1", "2")
