"""Breaking-change advisor — read-only advisory for proposed interface changes.

Combines four moving parts into a single advisory response:

1. :mod:`registry.service.interface_normalize` — turn the producer's
   proposed interface declaration into a canonical
   :class:`~catalog.types.InterfaceSurface`.
2. Semver validation — reject malformed proposed versions early.
3. :mod:`registry.service.interface_diff` — classify the change and emit
   per-element evidence.
4. Reverse traversal (depth=5) — find consumers; filter to those whose
   adoption ``version_pin`` does not satisfy the proposed version *or*
   whose usage references a removed operation/field/event.

Cross-tenant consumer identifiers are anonymised in the response (opaque
counter for tenant, hash for entity_id). The provider learns counts and
shape; the consumer's identity remains private. Same-tenant consumers
retain full identifiers so internal teams can investigate.

No state is mutated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

import semver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import ValidationError
from registry.service.interface_diff import (
    BREAKING,
    NON_BREAKING,
    generate_release_notes_scaffold,
)
from registry.service.interface_diff import (
    diff as interface_diff,
)
from registry.service.interface_normalize import normalize
from registry.service.retrieval import RetrievalService
from registry.service.version_predicates import evaluate_version_predicate
from registry.service.visibility import VisibilityService
from registry.types import (
    BreakingChangePreview,
    Clock,
    InterfaceSurface,
    TenantContext,
)

_log = logging.getLogger(__name__)

#: Attribute key under which we persist the canonical InterfaceSurface.
INTERFACE_CANONICAL_KEY = "interface_canonical"


class BreakingChangeAdvisor:
    """Stateless advisor — every method is read-only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        retrieval: RetrievalService,
        visibility: VisibilityService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._retrieval = retrieval
        self._visibility = visibility

    async def preview_version(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID,
        proposed_version: str,
        proposed_interface: dict[str, Any] | str,
        interface_format: str,
    ) -> BreakingChangePreview:
        """Compute the advisory for a hypothetical version+interface change.

        Order of operations matches the contract: semver first (cheapest
        failure), then normalize (next-cheapest), then load current
        surface, diff, blast-radius, filter, anonymise, scaffold.
        """
        _validate_semver(proposed_version)
        proposed = normalize(proposed_interface, interface_format)

        await self._visibility.assert_visible(ctx, capability_id)

        current = await self._load_current_surface(capability_id)
        classification, changes = interface_diff(current, proposed)

        affected = await self._build_affected_consumers(
            ctx=ctx,
            capability_id=capability_id,
            classification=classification,
            proposed_version=proposed_version,
            changes=changes,
        )

        scaffold = generate_release_notes_scaffold(classification, changes)

        return BreakingChangePreview(
            capability_id=capability_id,
            proposed_version=proposed_version,
            diff_classification=classification,
            changes=changes,
            affected_consumers=affected,
            release_notes_scaffold=scaffold,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load_current_surface(self, capability_id: uuid.UUID) -> InterfaceSurface:
        """Read the active ``interface_canonical`` attribute, if any.

        When no canonical surface exists yet, treat the current state as
        an empty surface — the diff then reports every part of the
        proposed surface as additive (non-breaking).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT value FROM attributes
                    WHERE entity_id = :eid
                      AND key = :k
                      AND t_invalidated_at IS NULL
                      AND t_valid_to IS NULL
                    ORDER BY t_valid_from DESC
                    LIMIT 1
                    """
                ),
                {"eid": capability_id, "k": INTERFACE_CANONICAL_KEY},
            )
            row = result.first()

        if row is None:
            return InterfaceSurface(operations=[], events=[], fields=[])

        raw = row.value
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:  # pragma: no cover
                _log.warning("malformed interface_canonical for %s: %s", capability_id, exc)
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return InterfaceSurface(
            operations=list(raw.get("operations") or []),
            events=list(raw.get("events") or []),
            fields=list(raw.get("fields") or []),
        )

    async def _build_affected_consumers(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID,
        classification: str,
        proposed_version: str,
        changes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Find consumers and apply the cross-tenant anonymisation policy.

        Cross-tenant consumers come from ``adoption_events`` (the canonical
        cross-tenant dependency record); same-tenant consumers come from the
        reverse traversal (which is tenant-scoped).

        A consumer is *affected* if any of:
        - the diff is breaking, OR
        - the diff has removed/narrowed consumer-impacting elements, OR
        - the consumer's adoption ``version_pin`` does not satisfy the
          proposed_version (only matters at deprecation severity or
          below — breaking diffs already list every consumer).
        """
        # Purely additive change with no consumer-impacting elements: nobody
        # is affected and we can skip both DB queries.
        if classification == NON_BREAKING and not _has_consumer_impacting(changes):
            return []

        # Same-tenant consumers via reverse traversal (still tenant-scoped).
        traversal = await self._retrieval.get_reverse_traversal(
            ctx=ctx,
            entity_id=capability_id,
            depth=5,
            edge_types=None,
        )
        same_tenant_nodes = [n for n in traversal.nodes if n.entity_id != capability_id]

        # Cross-tenant consumers via adoption_events (the reverse-traversal CTE
        # is tenant-scoped and won't surface them).
        adoptions = await self._fetch_active_adoptions(capability_id)

        out: list[dict[str, Any]] = []
        opaque_counter = 0

        # Same-tenant entries first — fully identified.
        for node in same_tenant_nodes:
            pin = adoptions.get(node.tenant_id, {}).get("version_pin")
            if not _adoption_in_scope(pin, proposed_version, classification, changes):
                continue
            out.append(
                {
                    "tenant_id": str(node.tenant_id),
                    "entity_id": str(node.entity_id),
                    "name": node.name,
                    "version_pin": pin,
                }
            )

        # Cross-tenant entries — anonymised (opaque counter + hashed entity_id).
        for ctid, info in adoptions.items():
            if ctid == ctx.tenant_id:
                continue  # already covered by reverse traversal
            pin = info.get("version_pin")
            if not _adoption_in_scope(pin, proposed_version, classification, changes):
                continue
            opaque_counter += 1
            out.append(
                {
                    "tenant_id": f"cross-tenant-{opaque_counter}",
                    "entity_id": _opaque_hash(ctid),
                    "name": None,
                    "version_pin": pin,
                }
            )
        return out

    async def _fetch_active_adoptions(self, capability_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
        """Return ``{consumer_tenant_id: {version_pin, adoption_id}}``."""
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT consumer_tenant_id, version_pin, adoption_id
                    FROM adoption_events
                    WHERE provider_capability_id = :cap
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"cap": capability_id},
            )
            return {
                row.consumer_tenant_id: {
                    "version_pin": row.version_pin,
                    "adoption_id": row.adoption_id,
                }
                for row in result.all()
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_semver(value: str) -> None:
    """Reject malformed proposed_version with the canonical T11 message."""
    if not isinstance(value, str):
        raise ValidationError(f"{value!r} is not valid semver 2.0.0. " "Example: '2.4.1', '3.0.0-alpha.1'.")
    try:
        semver.Version.parse(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"{value!r} is not valid semver 2.0.0. " "Example: '2.4.1', '3.0.0-alpha.1'.") from exc


_CONSUMER_IMPACTING_CHANGES: frozenset[str] = frozenset(
    {
        "operation_removed",
        "operation_input_narrowed",
        "operation_input_type_changed",
        "operation_output_type_changed",
        "field_removed",
        "field_added_required",
        "field_required_added",
        "field_type_changed",
        "event_removed",
        "event_payload_narrowed",
    }
)


def _has_consumer_impacting(changes: list[dict[str, Any]]) -> bool:
    return any(c["change_type"] in _CONSUMER_IMPACTING_CHANGES for c in changes)


def _adoption_in_scope(
    version_pin: str | None,
    proposed_version: str,
    classification: str,
    changes: list[dict[str, Any]],
) -> bool:
    """Decide whether a consumer adoption is in-scope for the advisory."""
    # Anything classified breaking always lists every consumer; the
    # advisor's job is to surface impact, not filter it.
    if classification == BREAKING:
        return True
    if _has_consumer_impacting(changes):
        return True
    # Deprecation only or no consumer-impacting change: only adoptions
    # whose pin fails the proposed version are still in scope.
    if version_pin is None:
        return False
    return not evaluate_version_predicate(proposed_version, version_pin)


def _opaque_hash(entity_id: uuid.UUID) -> str:
    """Stable, irreversible opaque identifier for a cross-tenant consumer entity."""
    h = hashlib.sha256(str(entity_id).encode("utf-8")).hexdigest()
    return f"opaque-{h[:16]}"


__all__ = [
    "BreakingChangeAdvisor",
    "INTERFACE_CANONICAL_KEY",
]
