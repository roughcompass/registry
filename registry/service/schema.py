"""SchemaService — JSON Schema validation against capability_type and edge_rel registries.

Schema records are bi-temporal; this service fetches the current row at write
time (no caching) so an admin schema change is visible without a process restart.

This service reaches into `capability_type_schemas` and `edge_property_schemas`
via raw SQL so it has no compile-time dependency on ORM models for those tables.

Methods:
  `validate_capability`    — validate capability attributes against a registered type schema.
  `register_edge_schema`   — insert a bi-temporal row into edge_property_schemas.
  `validate_edge_properties` — fetch current schema for an edge_rel and validate.

Advisory enforcement: schemas with `is_advisory=True` produce warnings on
violation (write proceeds); mandatory schemas produce errors (422 on violation).
Auto-transition: if `advisory_until` is None the schema is mandatory after
30 days; if `advisory_until` is set, the schema is advisory until that timestamp.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

import jsonschema
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import ValidationError, VocabularyError
from registry.service.vocabulary import VocabularyService
from registry.types import Clock, TenantContext

# Advisory window applied when advisory_until is not set on an advisory schema.
_DEFAULT_ADVISORY_DAYS = 30


@dataclass
class ValidationResult:
    valid: bool
    warnings: list[str]


class SchemaService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def validate_capability(
        self,
        ctx: TenantContext,
        capability_type: str,
        attributes: dict[str, Any],
    ) -> ValidationResult:
        """Look up the current schema for `capability_type` and validate `attributes`.

        Returns a ValidationResult with `valid=True, warnings=[...]` if the
        schema is advisory; raises ValidationError if mandatory and invalid.
        Returns `valid=True, warnings=[]` if no schema is registered for the
        type — schema-free types are allowed.
        """
        async with self._session_factory() as session:
            row = await self._fetch_current_schema(session, ctx, capability_type)

        if row is None:
            return ValidationResult(valid=True, warnings=[])

        json_schema, is_advisory = row

        try:
            jsonschema.validate(instance=attributes, schema=json_schema)
        except jsonschema.ValidationError as exc:
            if is_advisory:
                return ValidationResult(valid=True, warnings=[str(exc.message)])
            msg = f"capability attributes failed schema validation for type " f"{capability_type!r}: {exc.message}"
            raise ValidationError(msg) from exc

        return ValidationResult(valid=True, warnings=[])

    async def _fetch_current_schema(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        capability_type: str,
    ) -> tuple[dict[str, Any], bool] | None:
        """Bi-temporal current-row fetch via raw SQL."""
        result = await session.execute(
            text(
                "SELECT json_schema, is_advisory "
                "FROM capability_type_schemas "
                "WHERE tenant_id = :tid "
                "  AND type_name = :type_name "
                "  AND t_invalidated_at IS NULL "
                "  AND (t_valid_to IS NULL OR t_valid_to > :now) "
                "ORDER BY t_valid_from DESC "
                "LIMIT 1"
            ),
            {"tid": ctx.tenant_id, "type_name": capability_type, "now": self._clock.now()},
        )
        row = result.first()
        if row is None:
            return None
        json_schema = row[0]
        is_advisory = bool(row[1])
        return json_schema, is_advisory

    # ------------------------------------------------------------------
    # Edge property schema registry
    # ------------------------------------------------------------------

    async def register_edge_schema(
        self,
        ctx: TenantContext,
        edge_rel: str,
        json_schema: dict[str, Any],
        is_advisory: bool = True,
        advisory_until: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """Insert a new bi-temporal row into ``edge_property_schemas``.

        Validates ``edge_rel`` against the ``edge_rel`` vocabulary kind.
        Validates ``json_schema`` is a well-formed JSON Schema object (must be
        a ``dict`` with a ``"type"`` or ``"$schema"`` or ``"properties"`` key;
        empty dicts are rejected).

        Supersession of an existing schema for the same ``edge_rel`` must be
        done by invalidating the previous row and inserting a new one — this
        method always inserts a fresh row (idempotent from the caller's
        perspective: two identical inserts produce two valid rows with the same
        ``t_valid_from``; deduplication is out of scope for T03).

        Returns the inserted row as a ``dict``.
        """
        # 1. Vocabulary guard — raises VocabularyError on unknown/deprecated rel.
        vocab = VocabularyService(self._session_factory)
        await vocab.validate_edge_rel(ctx, edge_rel)

        # 2. Basic JSON Schema well-formedness check (structural, not semantic).
        _validate_json_schema_shape(json_schema)

        # 3. Compute effective advisory_until when not supplied.
        now = self._clock.now()
        effective_advisory_until: datetime.datetime | None = advisory_until
        if is_advisory and advisory_until is None:
            effective_advisory_until = now + datetime.timedelta(days=_DEFAULT_ADVISORY_DAYS)

        schema_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO edge_property_schemas "
                    "(schema_id, tenant_id, edge_rel, json_schema, is_advisory, "
                    " advisory_until, t_valid_from, t_valid_to, t_ingested_at, "
                    " t_invalidated_at, created_by) "
                    "VALUES (:schema_id, :tenant_id, :edge_rel, :json_schema::jsonb, "
                    "        :is_advisory, :advisory_until, :t_valid_from, NULL, "
                    "        :t_ingested_at, NULL, :created_by)"
                ),
                {
                    "schema_id": schema_id,
                    "tenant_id": ctx.tenant_id,
                    "edge_rel": edge_rel,
                    "json_schema": _json_dumps(json_schema),
                    "is_advisory": is_advisory,
                    "advisory_until": effective_advisory_until,
                    "t_valid_from": now,
                    "t_ingested_at": now,
                    "created_by": ctx.actor_id,
                },
            )

        return {
            "schema_id": schema_id,
            "tenant_id": ctx.tenant_id,
            "edge_rel": edge_rel,
            "json_schema": json_schema,
            "is_advisory": is_advisory,
            "advisory_until": effective_advisory_until,
            "t_valid_from": now,
            "t_valid_to": None,
            "t_ingested_at": now,
            "t_invalidated_at": None,
            "created_by": ctx.actor_id,
        }

    async def validate_edge_properties(
        self,
        ctx: TenantContext,
        edge_rel: str,
        properties: dict[str, Any],
        now: datetime.datetime,
    ) -> tuple[bool, list[str]]:
        """Validate ``properties`` against the current schema for ``edge_rel``.

        Fetches the latest non-invalidated, temporally-valid schema row.

        Advisory resolution:
          - No schema registered → ``(True, [])`` — write proceeds.
          - Schema is mandatory (``is_advisory=False``) and validation fails →
            ``(False, [error_str, ...])`` — caller should raise 422.
          - Schema is advisory and still within the advisory window
            (``advisory_until > now``, or ``advisory_until`` computed as
            ``t_valid_from + 30 days`` when the field is NULL and
            ``is_advisory=True``) → ``(True, [warning_str])`` on violation.
          - Advisory period has expired → treated as mandatory.

        Returns ``(True, [])`` when validation passes regardless of advisory
        status.
        """
        async with self._session_factory() as session:
            schema_row = await self._fetch_edge_schema(session, ctx, edge_rel, now)

        if schema_row is None:
            return (True, [])

        json_schema, is_advisory, advisory_until, t_valid_from = schema_row

        # Determine effective enforcement mode.
        in_advisory_window = _in_advisory_window(is_advisory, advisory_until, t_valid_from, now)

        errors: list[str] = []
        try:
            jsonschema.validate(instance=properties, schema=json_schema)
        except jsonschema.ValidationError as exc:
            errors.append(exc.message)

        if not errors:
            return (True, [])

        if in_advisory_window:
            return (True, [f"advisory schema warning for edge_rel={edge_rel!r}: {errors[0]}"])

        return (False, errors)

    async def _fetch_edge_schema(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        edge_rel: str,
        now: datetime.datetime,
    ) -> tuple[dict[str, Any], bool, datetime.datetime | None, datetime.datetime] | None:
        """Bi-temporal current-row fetch for ``edge_property_schemas``."""
        result = await session.execute(
            text(
                "SELECT json_schema, is_advisory, advisory_until, t_valid_from "
                "FROM edge_property_schemas "
                "WHERE tenant_id = :tid "
                "  AND edge_rel = :edge_rel "
                "  AND t_invalidated_at IS NULL "
                "  AND (t_valid_to IS NULL OR t_valid_to > :now) "
                "ORDER BY t_valid_from DESC "
                "LIMIT 1"
            ),
            {"tid": ctx.tenant_id, "edge_rel": edge_rel, "now": now},
        )
        row = result.first()
        if row is None:
            return None
        return (row[0], bool(row[1]), row[2], row[3])


# ---------------------------------------------------------------------------
# Helpers (module-private)
# ---------------------------------------------------------------------------


def _validate_json_schema_shape(schema: Any) -> None:
    """Raise VocabularyError if ``schema`` is not a non-empty dict."""
    if not isinstance(schema, dict) or not schema:
        msg = "json_schema must be a non-empty dict (JSON Schema object)"
        raise VocabularyError(msg)


def _in_advisory_window(
    is_advisory: bool,
    advisory_until: datetime.datetime | None,
    t_valid_from: datetime.datetime,
    now: datetime.datetime,
) -> bool:
    """Return True iff the schema is currently in advisory (non-mandatory) mode."""
    if not is_advisory:
        return False
    if advisory_until is not None:
        return now < advisory_until
    # Fallback: is_advisory=True with no advisory_until → mandatory after 30 days.
    return now < (t_valid_from + datetime.timedelta(days=_DEFAULT_ADVISORY_DAYS))


def _json_dumps(value: Any) -> str:
    """Serialize a dict to JSON string for raw SQL JSONB cast."""
    import json

    return json.dumps(value)


__all__ = ["SchemaService", "ValidationResult"]
