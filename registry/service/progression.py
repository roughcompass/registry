"""Progression definition validation and transition enforcement.

Two entry points live here:

  1. ``validate_progression_definition`` — checks an incoming definition dict
     against the ProgressionDefinition meta-schema. Called by the admin router
     on POST/PUT; translates ValidationError into HTTP 422 with structured error
     paths.

  2. ``ProgressionService.validate_transition`` — the core state-machine
     validator. Called by EntityService.update_entity after _assert_tenant
     whenever the ``stage_progression`` attribute changes. Implements gate
     satisfaction (attribute-keyed, truthy predicate), tier-conditional skip
     rules, re-entry rules, override consumption, and audit emission.

The definition cache and single-flight coalescing are a follow-up concern;
this module ships the no-cache path. Every call loads the active definition
from the database directly.

Advisory vs enforcing:
  - ``is_advisory = True``  → rule violations produce warnings; the write
    still proceeds. Audit event: ``progression.transition.warned``.
  - ``is_advisory = False`` → rule violations raise ProgressionError (HTTP 422)
    and the write is aborted. Audit event: ``progression.transition.rejected``.

Override single-use invariant: ``consumed_at IS NULL`` means the override is
available. This module sets ``consumed_at`` in the same DB round-trip as the
transition check; no DB constraint enforces it — the service owns this
invariant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.audit import actions
from registry.exceptions import ValidationError
from registry.storage.models import ProgressionDefinition, ProgressionOverride
from registry.types import Clock, TenantContext

_SCHEMA_PATH = Path(__file__).with_name("progression_definition_schema.json")
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())
_VALIDATOR = Draft202012Validator(_SCHEMA)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process definition cache
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """One cached slot for an active ProgressionDefinition row.

    ``definition`` may be None — "no active definition for this key" is a
    valid result that is worth caching so repeated queries for unmanaged
    entity types do not hit the database.

    ``lock`` is per-entry so concurrent misses for the same key serialise
    through a single DB query (single-flight) while misses for *different*
    keys proceed in parallel.
    """

    definition: ProgressionDefinition | None
    expires_at: float  # monotonic timestamp; 0.0 means "not yet populated"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of a transition validation call.

    ``valid`` is always True when returned (ProgressionError is raised for
    invalid enforcing-mode transitions). ``warnings`` is non-empty when an
    advisory-mode rule was violated or an override was consumed.
    """

    valid: bool
    warnings: list[str]


class ProgressionError(Exception):
    """Raised when an enforcing-mode transition is rejected by the rule engine.

    Callers (EntityService, HTTP routers) map this to HTTP 422.
    The exception message describes the specific rule that blocked the transition
    so the operator can direct the producer to the correct remediation.
    """


# ---------------------------------------------------------------------------
# Meta-schema validator (T16)
# ---------------------------------------------------------------------------


def validate_progression_definition(definition: dict) -> None:
    """Validate a progression definition JSONB body against the meta-schema.

    Raises ValidationError on failure. The error message includes all
    constraint violations with their JSON paths, so callers can surface
    structured error details without re-validating.

    On success this function returns None. The caller decides whether to
    persist the definition or pass it on to the rule engine.
    """
    errors = sorted(
        _VALIDATOR.iter_errors(definition),
        key=lambda e: list(e.absolute_path),
    )
    if not errors:
        return

    # Consolidate all violations into one readable message. Each line carries
    # the JSON path and the constraint that failed so the operator can pinpoint
    # the offending field without re-reading the schema.
    messages = []
    for err in errors:
        path = ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "<root>"
        messages.append(f"{path}: {err.message}")

    raise ValidationError(
        "progression definition failed meta-schema validation:\n"
        + "\n".join(f"  - {m}" for m in messages)
    )


# ---------------------------------------------------------------------------
# Gate satisfaction predicate (F03)
# ---------------------------------------------------------------------------


def is_gate_satisfied(gate_id: str, entity_attributes: dict[str, Any]) -> bool:
    """Return True when entity_attributes[gate_id] is present AND truthy.

    Truthy values: True, non-empty string, number > 0, object with at least
    one key, array with at least one element.

    Falsy values (gate not satisfied): absent key, False, None, 0, empty
    string "", empty list [], empty dict {}.

    This predicate is pure — it never touches the database. Gate IDs are
    attribute keys; the tenant maps gate conditions to attribute writes.
    """
    if gate_id not in entity_attributes:
        return False
    val = entity_attributes[gate_id]
    if val is None or val is False:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, int | float):
        return val > 0
    if isinstance(val, str):
        return len(val) > 0
    if isinstance(val, list | tuple):
        return len(val) > 0
    if isinstance(val, dict):
        return len(val) > 0
    # Any other truthy Python value (e.g. a dataclass) is treated as satisfied.
    return bool(val)


# ---------------------------------------------------------------------------
# ProgressionService
# ---------------------------------------------------------------------------


class ProgressionService:
    """State-machine validator for stage_progression attribute transitions.

    Definition rows are cached in-process so that repeated calls for the same
    (tenant_id, entity_type) within the TTL window do not issue a DB query.
    Concurrent cache misses for the same key are coalesced (single-flight) via
    a per-entry asyncio.Lock so exactly one coroutine performs the DB lookup
    and the rest await its result.

    Set ``cache_ttl_seconds=0`` to disable caching entirely (every call hits
    the DB). This is appropriate in tests that need precise control over what
    the DB returns without worrying about cache state.

    Constructor args
    ----------------
    session_factory:
        Same async_sessionmaker used by EntityService. A new session is
        opened per validate_transition call to keep transaction scope narrow.
    clock:
        Injected clock (SystemClock in production, FakeClock in tests).
    cache_ttl_seconds:
        How long (in real seconds) a loaded definition stays valid in the
        in-process cache. Default 60. Pass 0 to disable.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        cache_ttl_seconds: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._cache_ttl_seconds = cache_ttl_seconds
        # Keyed by (tenant_id_str, entity_type). Populated on first miss.
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        # Protects structural mutations to self._cache (insert of new entries).
        # It does NOT protect the DB call — that is per-entry via entry.lock.
        self._cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def validate_transition(
        self,
        ctx: TenantContext,
        entity: Any,
        from_state: str | None,
        to_state: str,
    ) -> ValidationResult:
        """Validate a proposed stage_progression transition.

        Steps:
          1. Load the active progression definition for (tenant_id, entity_type).
             If none exists, return valid (pass-through for unmanaged types).
          2. Resolve tier from entity attributes.
          3. Validate forward rule (sequential / any).
          4. Validate skip rule (tier-conditional).
          5. Validate re-entry (same-state transitions).
          6. Check gate satisfaction for the destination state.
          7. If gates fail, look for a matching unconsumed override.
          8. Determine outcome: accepted / warned / overridden / rejected.

        Raises ProgressionError on enforcing-mode rejections.
        Returns ValidationResult(valid=True) for all other outcomes (warnings
        are in ValidationResult.warnings).

        Tenant isolation: tenant_id comes exclusively from ctx. This method is
        downstream of EntityService._assert_tenant — it never opens a new
        cross-tenant query path.
        """
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            defn_row = await self._load_active_definition_cached(
                session, ctx.tenant_id, entity.entity_type, now
            )

            if defn_row is None:
                # Entity types without a definition are not enforced.
                return ValidationResult(valid=True, warnings=[])

            definition = defn_row.definition
            is_advisory = defn_row.is_advisory
            progression_id = defn_row.progression_id

            # Build a quick lookup: state_id → {index, state_dict}
            states = definition.get("states", [])
            state_index: dict[str, int] = {s["id"]: i for i, s in enumerate(states)}
            state_by_id: dict[str, dict] = {s["id"]: s for s in states}

            # ---- Tier resolution ------------------------------------------------
            entity_attrs = self._get_attributes(entity)
            tier_rules = definition.get("tier_rules")
            tier: str | None = entity_attrs.get("tier")

            resolved_tier_rule: dict | None = None
            if tier_rules is not None:
                if tier and tier in tier_rules:
                    resolved_tier_rule = tier_rules[tier]
                elif "default" in tier_rules:
                    resolved_tier_rule = tier_rules["default"]
                else:
                    # tier_rules defined, entity has no tier, no "default" key.
                    await self._emit_audit(
                        session,
                        ctx,
                        action=actions.PROGRESSION_TRANSITION_REJECTED,
                        payload={
                            "entity_id": str(entity.entity_id),
                            "from_state": from_state,
                            "to_state": to_state,
                            "definition_id": str(progression_id),
                            "reason": "tier_not_resolvable",
                        },
                        now=now,
                    )
                    raise ProgressionError("tier_not_resolvable")

            # ---- Transitions config --------------------------------------------
            transitions = definition.get("transitions", {})
            forward_rule = transitions.get("forward", "sequential")
            skip_rule = transitions.get("skip", "never")
            reentry_cfg = transitions.get("reentry", {"allowed": False})

            # ---- Re-entry (same-state transition) ------------------------------
            if from_state == to_state:
                if not reentry_cfg.get("allowed", False):
                    reason = "reentry_not_allowed"
                    return await self._reject_or_warn(
                        session, ctx, entity, from_state, to_state, progression_id,
                        reason, is_advisory, now
                    )
                # If re-entry requires certain attributes, check them.
                requires = reentry_cfg.get("requires", [])
                for req_attr in requires:
                    if not entity_attrs.get(req_attr):
                        reason = f"reentry_requires_{req_attr}"
                        return await self._reject_or_warn(
                            session, ctx, entity, from_state, to_state, progression_id,
                            reason, is_advisory, now
                        )
                # Re-entry accepted.
                await self._emit_audit(
                    session, ctx,
                    action=actions.PROGRESSION_TRANSITION_ACCEPTED,
                    payload={
                        "entity_id": str(entity.entity_id),
                        "from_state": from_state,
                        "to_state": to_state,
                        "definition_id": str(progression_id),
                    },
                    now=now,
                )
                return ValidationResult(valid=True, warnings=[])

            # ---- Forward rule --------------------------------------------------
            if forward_rule == "sequential":
                from_pos = state_index.get(from_state) if from_state else -1
                to_pos = state_index.get(to_state)

                if to_pos is None:
                    reason = f"unknown_to_state:{to_state}"
                    return await self._reject_or_warn(
                        session, ctx, entity, from_state, to_state, progression_id,
                        reason, is_advisory, now
                    )

                if from_state is None:
                    # First transition from null → first state only.
                    if to_pos != 0:
                        reason = "must_start_from_first_state"
                        return await self._reject_or_warn(
                            session, ctx, entity, from_state, to_state, progression_id,
                            reason, is_advisory, now
                        )
                else:
                    if from_pos is None:
                        reason = f"unknown_from_state:{from_state}"
                        return await self._reject_or_warn(
                            session, ctx, entity, from_state, to_state, progression_id,
                            reason, is_advisory, now
                        )

                    expected_next = from_pos + 1

                    # Skip rule: check if this state may be skipped by tier.
                    if to_pos > expected_next:
                        # Jumping more than one step — evaluate skip rule.
                        if skip_rule == "tier-conditional" and resolved_tier_rule is not None:
                            # States between from+1 and to (exclusive of to) must all
                            # be in the tier's skip list.
                            skippable = set(resolved_tier_rule.get("skip", []))
                            intermediate = [states[i]["id"] for i in range(expected_next, to_pos)]
                            non_skippable = [s for s in intermediate if s not in skippable]
                            if non_skippable:
                                reason = f"skip_not_allowed_for_states:{','.join(non_skippable)}"
                                return await self._reject_or_warn(
                                    session, ctx, entity, from_state, to_state, progression_id,
                                    reason, is_advisory, now
                                )
                            # All intermediates are skippable for this tier — fall through.
                        else:
                            reason = f"forward_sequential_skip:from={from_state},to={to_state}"
                            return await self._reject_or_warn(
                                session, ctx, entity, from_state, to_state, progression_id,
                                reason, is_advisory, now
                            )

                    elif to_pos < expected_next:
                        # Backward transition.
                        reason = f"backward_transition:from={from_state},to={to_state}"
                        return await self._reject_or_warn(
                            session, ctx, entity, from_state, to_state, progression_id,
                            reason, is_advisory, now
                        )

            elif forward_rule == "any":
                # Any state → state transition is allowed; skip and reentry
                # rules still apply above but we already handled reentry.
                if to_state not in state_index:
                    reason = f"unknown_to_state:{to_state}"
                    return await self._reject_or_warn(
                        session, ctx, entity, from_state, to_state, progression_id,
                        reason, is_advisory, now
                    )

            # ---- Gate check ----------------------------------------------------
            dest_state = state_by_id.get(to_state, {})
            gate_ids = dest_state.get("gates", [])
            failing_gates = [g for g in gate_ids if not is_gate_satisfied(g, entity_attrs)]

            if failing_gates:
                # Look for an unconsumed, currently-valid override.
                override = await self._find_override(
                    session, ctx.tenant_id, entity.entity_id, from_state, to_state, now
                )

                if override is not None and (
                    override.gate_id == "*" or override.gate_id in failing_gates
                ):
                    # Consume the override — single-use invariant.
                    override.consumed_at = now
                    await session.flush()

                    await self._emit_audit(
                        session, ctx,
                        action=actions.PROGRESSION_TRANSITION_OVERRIDDEN,
                        payload={
                            "entity_id": str(entity.entity_id),
                            "override_id": str(override.override_id),
                            "from_state": from_state,
                            "to_state": to_state,
                            "gate_id": override.gate_id,
                            "authorized_by": str(override.authorized_by),
                        },
                        now=now,
                    )
                    return ValidationResult(
                        valid=True,
                        warnings=[f"transition override applied for gate(s): {', '.join(failing_gates)}"],
                    )

                # No matching override — apply advisory/enforcing policy.
                reason = f"gates_not_satisfied:{','.join(failing_gates)}"
                return await self._reject_or_warn(
                    session, ctx, entity, from_state, to_state, progression_id,
                    reason, is_advisory, now
                )

            # ---- All checks passed: accepted -----------------------------------
            await self._emit_audit(
                session, ctx,
                action=actions.PROGRESSION_TRANSITION_ACCEPTED,
                payload={
                    "entity_id": str(entity.entity_id),
                    "from_state": from_state,
                    "to_state": to_state,
                    "definition_id": str(progression_id),
                },
                now=now,
            )
            return ValidationResult(valid=True, warnings=[])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _get_attributes(entity: Any) -> dict[str, Any]:
        """Extract entity attributes as a plain dict.

        Handles two shapes: a dict (as built by EntityService before a DB
        round-trip) or an object with an `attributes` property that is either
        a dict or an iterable of (key, value) pairs.
        """
        if isinstance(entity, dict):
            return entity.get("attributes", {})
        attrs = getattr(entity, "attributes", {})
        if isinstance(attrs, dict):
            return attrs
        # Iterable of ORM Attribute rows — build dict from key/value.
        return {row.key: row.value for row in attrs}

    async def _load_active_definition_cached(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        entity_type: str,
        now: Any,
    ) -> ProgressionDefinition | None:
        """Return the active definition, serving from the in-process cache when possible.

        When ``cache_ttl_seconds`` is 0 the cache is fully bypassed and every
        call issues a DB query. This is intentional — callers that need
        per-call freshness (tests, admin tooling) construct the service with
        TTL=0.

        Cache miss / expiry path uses a two-lock protocol:
          1. ``_cache_lock`` (structural) — only held long enough to check/insert
             the entry placeholder; never held across the DB call.
          2. ``entry.lock`` (per-key) — serialises concurrent misses for the
             same key so exactly one coroutine performs the DB query.

        A re-check after acquiring ``entry.lock`` handles the window between
        the structural lock release and the per-key lock acquisition: another
        coroutine may have refreshed the entry in that window.
        """
        if self._cache_ttl_seconds == 0:
            return await self._load_active_definition_uncached(
                session, tenant_id, entity_type, now
            )

        key = (str(tenant_id), entity_type)
        wall = monotonic()

        # Fast path: cache hit — no lock needed.
        entry = self._cache.get(key)
        if entry is not None and entry.expires_at > wall:
            return entry.definition

        # Slow path: miss or expired. Ensure an entry placeholder exists.
        async with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > wall:
                # Another coroutine refreshed the entry while we waited.
                return entry.definition
            if entry is None:
                # Insert placeholder so other coroutines queue on entry.lock
                # rather than all racing to insert.
                entry = _CacheEntry(definition=None, expires_at=0.0)
                self._cache[key] = entry
        # _cache_lock is released here — the DB call happens outside it.

        # Per-key lock: exactly one coroutine executes the DB query; the rest
        # wait and then return the result the winner populated.
        async with entry.lock:
            wall = monotonic()
            if entry.expires_at > wall:
                # Winner already refreshed while we were queued.
                return entry.definition
            definition = await self._load_active_definition_uncached(
                session, tenant_id, entity_type, now
            )
            entry.definition = definition
            entry.expires_at = monotonic() + self._cache_ttl_seconds
            return definition

    async def _load_active_definition_uncached(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        entity_type: str,
        now: Any,
    ) -> ProgressionDefinition | None:
        """Load the single active progression definition for (tenant_id, entity_type).

        Active means: t_valid_from <= now AND (t_valid_to IS NULL OR t_valid_to > now)
        AND t_invalidated_at IS NULL, scoped to tenant_id + entity_type.

        Returns None when no definition exists — callers treat this as pass-through.
        """
        result = await session.execute(
            select(ProgressionDefinition).where(
                ProgressionDefinition.tenant_id == tenant_id,
                ProgressionDefinition.entity_type == entity_type,
                ProgressionDefinition.t_valid_from <= now,
                ProgressionDefinition.t_invalidated_at.is_(None),
            ).where(
                # t_valid_to IS NULL OR t_valid_to > now
                (ProgressionDefinition.t_valid_to.is_(None))
                | (ProgressionDefinition.t_valid_to > now)
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_override(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        entity_id: uuid.UUID,
        from_state: str | None,
        to_state: str,
        now: Any,
    ) -> ProgressionOverride | None:
        """Return the first matching unconsumed, currently-valid override or None."""
        result = await session.execute(
            select(ProgressionOverride).where(
                ProgressionOverride.tenant_id == tenant_id,
                ProgressionOverride.entity_id == entity_id,
                ProgressionOverride.from_state == (from_state or ""),
                ProgressionOverride.to_state == to_state,
                ProgressionOverride.consumed_at.is_(None),
                ProgressionOverride.t_valid_from <= now,
                ProgressionOverride.t_valid_to > now,
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _emit_audit(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        action: str,
        payload: dict[str, Any],
        now: Any,
    ) -> None:
        """Write one audit_log row for a progression transition event.

        Uses the same raw-SQL audit-emit pattern used elsewhere in the service
        layer so the audit subsystem receives all transition events in a uniform
        shape. The payload is serialized to JSONB in the after_jsonb column;
        before_jsonb is NULL for all progression events (transitions are
        point-in-time assertions, not record mutations).
        """
        import json as _json  # noqa: PLC0415

        await session.execute(
            text(
                "INSERT INTO audit_log "
                "(audit_id, tenant_id, actor_id, action, target_type, "
                " target_id, before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES "
                "(:audit_id, :tenant_id, :actor_id, :action, 'progression', "
                " :target_id, NULL, CAST(:after_jsonb AS jsonb), :ts, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tenant_id": ctx.tenant_id,
                "actor_id": ctx.actor_id,
                "action": action,
                "target_id": uuid.UUID(payload["entity_id"]),
                "after_jsonb": _json.dumps(payload),
                "ts": now,
            },
        )

    async def _reject_or_warn(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        entity: Any,
        from_state: str | None,
        to_state: str,
        progression_id: uuid.UUID,
        reason: str,
        is_advisory: bool,
        now: Any,
    ) -> ValidationResult:
        """Emit the appropriate audit event and either warn or raise ProgressionError."""
        entity_id = str(entity.entity_id)
        payload = {
            "entity_id": entity_id,
            "from_state": from_state,
            "to_state": to_state,
            "definition_id": str(progression_id),
            "reason": reason,
        }

        if is_advisory:
            await self._emit_audit(
                session, ctx,
                action=actions.PROGRESSION_TRANSITION_WARNED,
                payload=payload,
                now=now,
            )
            return ValidationResult(valid=True, warnings=[reason])
        else:
            await self._emit_audit(
                session, ctx,
                action=actions.PROGRESSION_TRANSITION_REJECTED,
                payload=payload,
                now=now,
            )
            # Commit the audit row before raising so the rejection record is
            # persisted even though ProgressionError unwinds the caller's session.
            await session.commit()
            raise ProgressionError(reason)


__all__ = [
    "validate_progression_definition",
    "is_gate_satisfied",
    "ValidationResult",
    "ProgressionError",
    "ProgressionService",
]
