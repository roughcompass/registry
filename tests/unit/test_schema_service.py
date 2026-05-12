"""Unit tests for SchemaService — advisory vs mandatory; mocked session."""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import ValidationError
from registry.service.schema import SchemaService
from registry.types import FakeClock, TenantContext


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["producer"])


def _factory_returning_row(json_schema: dict[str, Any] | None, is_advisory: bool) -> MagicMock:
    """Mock factory whose session.execute returns a Result with `.first()` matching the contract."""
    result = MagicMock()
    if json_schema is None:
        result.first = MagicMock(return_value=None)
    else:
        result.first = MagicMock(return_value=(json_schema, is_advisory))

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=session)


_MIN_PROPS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"version": {"type": "string"}},
    "required": ["version"],
}


@pytest.mark.asyncio
async def test_validate_capability_passes_when_no_schema_registered() -> None:
    factory = _factory_returning_row(None, is_advisory=False)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))
    service = SchemaService(factory, clock)
    result = await service.validate_capability(_ctx(), "untyped", {"any": "thing"})
    assert result.valid is True
    assert result.warnings == []


@pytest.mark.asyncio
async def test_validate_capability_advisory_returns_warning_on_violation() -> None:
    factory = _factory_returning_row(_MIN_PROPS_SCHEMA, is_advisory=True)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))
    service = SchemaService(factory, clock)
    result = await service.validate_capability(_ctx(), "api_service", {})
    assert result.valid is True
    assert result.warnings  # at least one warning


@pytest.mark.asyncio
async def test_validate_capability_mandatory_raises_on_violation() -> None:
    factory = _factory_returning_row(_MIN_PROPS_SCHEMA, is_advisory=False)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))
    service = SchemaService(factory, clock)
    with pytest.raises(ValidationError):
        await service.validate_capability(_ctx(), "api_service", {})


@pytest.mark.asyncio
async def test_validate_capability_mandatory_passes_on_valid_attributes() -> None:
    factory = _factory_returning_row(_MIN_PROPS_SCHEMA, is_advisory=False)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))
    service = SchemaService(factory, clock)
    result = await service.validate_capability(_ctx(), "api_service", {"version": "1.0.0"})
    assert result.valid is True
    assert result.warnings == []
