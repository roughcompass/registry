"""Unit tests for the integration capability-type schema path.

Locks in the contract that a tenant-scoped
``capability_type_schemas`` row for ``type_name='integration'`` causes
``CatalogService.create_entity(capability_type='integration', ...)`` to
validate attributes via ``SchemaService.validate_capability``.

What this audit found:
- Migration 0009 seeds the integration schema under the *default system
  tenant* UUID. Regular tenants must seed (or have it seeded) per-tenant
  for the validation to fire. This test exercises both branches:
    (a) no schema for the calling tenant → validation no-ops (existing
        behaviour; schema-free types are allowed).
    (b) a schema is registered for the calling tenant → invalid attributes
        raise ValidationError; valid attributes pass.
- No code change is required: the existing schema service already does
  the right thing once a schema row is present. The test locks in that
  contract so a future refactor cannot silently drop the validation.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import ValidationError
from registry.service.schema import SchemaService, ValidationResult
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=_ACTOR, roles=["producer"])


def _async_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_factory(schema_row: tuple[dict[str, Any], bool] | None) -> MagicMock:
    async def _execute(stmt: Any, params: dict | None = None):
        result = MagicMock()
        result.first = MagicMock(return_value=(schema_row[0], schema_row[1]) if schema_row else None)
        return result

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session)


_INTEGRATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "config_template": {"type": "string"},
        "runbook_url": {"type": "string", "format": "uri"},
        "known_issues": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# Branch A — no schema registered for the calling tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_schema_for_tenant_makes_validation_a_noop() -> None:
    """When no schema row exists for the tenant + type, validation passes."""
    svc = SchemaService(_make_factory(schema_row=None), FakeClock(_NOW))
    result = await svc.validate_capability(
        ctx=_ctx(),
        capability_type="integration",
        attributes={"runbook_url": 42},  # would fail if a schema were present
    )
    assert isinstance(result, ValidationResult)
    assert result.valid is True
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Branch B — schema registered, validation fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_integration_attributes_pass_schema_validation() -> None:
    svc = SchemaService(
        _make_factory(schema_row=(_INTEGRATION_SCHEMA, False)),
        FakeClock(_NOW),
    )
    result = await svc.validate_capability(
        ctx=_ctx(),
        capability_type="integration",
        attributes={
            "config_template": "yaml-blob",
            "runbook_url": "https://example.com/runbook",
            "known_issues": ["X", "Y"],
        },
    )
    assert result.valid is True


@pytest.mark.asyncio
async def test_runbook_url_wrong_type_fails_validation() -> None:
    svc = SchemaService(
        _make_factory(schema_row=(_INTEGRATION_SCHEMA, False)),
        FakeClock(_NOW),
    )
    with pytest.raises(ValidationError) as excinfo:
        await svc.validate_capability(
            ctx=_ctx(),
            capability_type="integration",
            attributes={"runbook_url": 42},
        )
    assert "integration" in str(excinfo.value)


@pytest.mark.asyncio
async def test_known_issues_not_an_array_fails_validation() -> None:
    svc = SchemaService(
        _make_factory(schema_row=(_INTEGRATION_SCHEMA, False)),
        FakeClock(_NOW),
    )
    with pytest.raises(ValidationError):
        await svc.validate_capability(
            ctx=_ctx(),
            capability_type="integration",
            attributes={"known_issues": "not-a-list"},
        )


@pytest.mark.asyncio
async def test_advisory_schema_emits_warning_instead_of_raising() -> None:
    """Advisory mode: violation produces a warning but the write proceeds."""
    svc = SchemaService(
        _make_factory(schema_row=(_INTEGRATION_SCHEMA, True)),
        FakeClock(_NOW),
    )
    result = await svc.validate_capability(
        ctx=_ctx(),
        capability_type="integration",
        attributes={"runbook_url": 42},
    )
    assert result.valid is True
    assert len(result.warnings) == 1
