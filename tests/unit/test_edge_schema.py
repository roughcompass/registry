"""Unit tests for edge vocabulary + edge-property schema service.

Tests are purely in-memory — no DB.  Session factories are mocked to return
pre-canned rows matching the SQL columns fetched by SchemaService.

Coverage:
  - validate_edge_rel: accepts current vocab values; rejects unknown/deprecated.
  - register_edge_schema: vocab guard, JSON Schema well-formedness, inserted row shape.
  - validate_edge_properties: no schema → (True, []); mandatory violation → (False, errors);
    advisory within window → (True, [warning]); advisory expired → (False, errors).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import VocabularyError
from registry.service.schema import SchemaService
from registry.service.vocabulary import VocabularyService
from registry.storage.models import VocabularyValue
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 5, 10, 12, 0, 0, tzinfo=datetime.UTC)
_ADVISORY_FUTURE = _NOW + datetime.timedelta(days=20)
_ADVISORY_PAST = _NOW - datetime.timedelta(days=5)
_VALID_FROM_RECENT = _NOW - datetime.timedelta(days=5)  # within 30-day window
_VALID_FROM_OLD = _NOW - datetime.timedelta(days=40)  # outside 30-day window


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"])


def _vocab_factory(row: VocabularyValue | None) -> MagicMock:
    """Mock session_factory whose session.execute returns scalar_one_or_none=row."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=_async_noop_ctx())

    return MagicMock(return_value=session)


def _async_noop_ctx() -> Any:
    """Async context manager that does nothing."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_vocab_row(*, value: str = "requires", deprecated: bool = False) -> VocabularyValue:
    row = VocabularyValue()
    row.vocab_id = uuid.uuid4()
    row.tenant_id = uuid.uuid4()
    row.kind = "edge_rel"
    row.value = value
    row.is_system = True
    row.deprecated_at = _NOW if deprecated else None
    return row


def _schema_factory_for_register(execute_results: list[Any]) -> MagicMock:
    """Factory for register_edge_schema: first call returns vocab row; second is the INSERT."""
    call_count = 0
    results = list(execute_results)

    async def _execute(*_a: Any, **_kw: Any) -> Any:
        nonlocal call_count
        r = results[call_count % len(results)]
        call_count += 1
        return r

    session = MagicMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=_async_noop_ctx())
    session.add = MagicMock()

    return MagicMock(return_value=session)


def _vocab_result(row: VocabularyValue | None) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=row)
    return r


def _insert_result() -> MagicMock:
    r = MagicMock()
    r.rowcount = 1
    return r


def _schema_edge_factory(
    edge_row: tuple[Any, ...] | None,
    vocab_row: VocabularyValue | None = None,
) -> MagicMock:
    """Factory for validate_edge_properties: returns an edge schema row (or None)."""
    first_result = MagicMock()
    first_result.first = MagicMock(return_value=edge_row)

    session = MagicMock()
    session.execute = AsyncMock(return_value=first_result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=_async_noop_ctx())

    return MagicMock(return_value=session)


_MIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"version": {"type": "string"}},
    "required": ["version"],
}


# ---------------------------------------------------------------------------
# validate_edge_rel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_edge_rel_accepts_requires() -> None:
    factory = _vocab_factory(_make_vocab_row(value="requires"))
    svc = VocabularyService(factory)
    await svc.validate_edge_rel(_ctx(), "requires")  # no raise


@pytest.mark.asyncio
async def test_validate_edge_rel_accepts_conflicts_with() -> None:
    factory = _vocab_factory(_make_vocab_row(value="conflicts_with"))
    svc = VocabularyService(factory)
    await svc.validate_edge_rel(_ctx(), "conflicts_with")


@pytest.mark.asyncio
async def test_validate_edge_rel_accepts_composes() -> None:
    factory = _vocab_factory(_make_vocab_row(value="composes"))
    svc = VocabularyService(factory)
    await svc.validate_edge_rel(_ctx(), "composes")


@pytest.mark.asyncio
async def test_validate_edge_rel_accepts_provides_to() -> None:
    factory = _vocab_factory(_make_vocab_row(value="provides_to"))
    svc = VocabularyService(factory)
    await svc.validate_edge_rel(_ctx(), "provides_to")


@pytest.mark.asyncio
async def test_validate_edge_rel_rejects_unknown() -> None:
    factory = _vocab_factory(None)
    svc = VocabularyService(factory)
    with pytest.raises(VocabularyError, match="unknown"):
        await svc.validate_edge_rel(_ctx(), "ghost_rel")


@pytest.mark.asyncio
async def test_validate_edge_rel_rejects_deprecated() -> None:
    factory = _vocab_factory(_make_vocab_row(value="requires", deprecated=True))
    svc = VocabularyService(factory)
    with pytest.raises(VocabularyError, match="deprecated"):
        await svc.validate_edge_rel(_ctx(), "requires")


# ---------------------------------------------------------------------------
# register_edge_schema tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_edge_schema_returns_row_dict() -> None:
    factory = _schema_factory_for_register(
        [
            _vocab_result(_make_vocab_row(value="requires")),
            _insert_result(),
        ]
    )
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)
    ctx = _ctx()

    result = await svc.register_edge_schema(ctx, "requires", _MIN_SCHEMA, is_advisory=False)

    assert result["edge_rel"] == "requires"
    assert result["tenant_id"] == ctx.tenant_id
    assert result["json_schema"] == _MIN_SCHEMA
    assert result["is_advisory"] is False
    assert result["t_valid_from"] == _NOW
    assert "schema_id" in result


@pytest.mark.asyncio
async def test_register_edge_schema_sets_advisory_until_when_advisory_and_no_date() -> None:
    factory = _schema_factory_for_register(
        [
            _vocab_result(_make_vocab_row(value="composes")),
            _insert_result(),
        ]
    )
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    result = await svc.register_edge_schema(_ctx(), "composes", _MIN_SCHEMA, is_advisory=True)

    expected = _NOW + datetime.timedelta(days=30)
    assert result["advisory_until"] == expected


@pytest.mark.asyncio
async def test_register_edge_schema_honours_explicit_advisory_until() -> None:
    factory = _schema_factory_for_register(
        [
            _vocab_result(_make_vocab_row(value="provides_to")),
            _insert_result(),
        ]
    )
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)
    custom_until = _NOW + datetime.timedelta(days=10)

    result = await svc.register_edge_schema(
        _ctx(), "provides_to", _MIN_SCHEMA, is_advisory=True, advisory_until=custom_until
    )

    assert result["advisory_until"] == custom_until


@pytest.mark.asyncio
async def test_register_edge_schema_rejects_unknown_rel() -> None:
    factory = _schema_factory_for_register([_vocab_result(None)])
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    with pytest.raises(VocabularyError, match="unknown"):
        await svc.register_edge_schema(_ctx(), "nonexistent_rel", _MIN_SCHEMA)


@pytest.mark.asyncio
async def test_register_edge_schema_rejects_empty_dict() -> None:
    factory = _schema_factory_for_register(
        [
            _vocab_result(_make_vocab_row(value="requires")),
        ]
    )
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    with pytest.raises(VocabularyError, match="non-empty dict"):
        await svc.register_edge_schema(_ctx(), "requires", {})


@pytest.mark.asyncio
async def test_register_edge_schema_rejects_non_dict() -> None:
    factory = _schema_factory_for_register(
        [
            _vocab_result(_make_vocab_row(value="requires")),
        ]
    )
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    with pytest.raises(VocabularyError, match="non-empty dict"):
        await svc.register_edge_schema(_ctx(), "requires", "not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_edge_properties tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_edge_properties_no_schema_returns_true_empty() -> None:
    factory = _schema_edge_factory(None)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    valid, errors = await svc.validate_edge_properties(_ctx(), "requires", {"any": "thing"}, _NOW)

    assert valid is True
    assert errors == []


@pytest.mark.asyncio
async def test_validate_edge_properties_mandatory_passes_on_valid() -> None:
    # is_advisory=False, advisory_until=None, t_valid_from=recent → mandatory
    edge_row = (_MIN_SCHEMA, False, None, _VALID_FROM_RECENT)
    factory = _schema_edge_factory(edge_row)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    valid, errors = await svc.validate_edge_properties(_ctx(), "requires", {"version": "1.0"}, _NOW)

    assert valid is True
    assert errors == []


@pytest.mark.asyncio
async def test_validate_edge_properties_mandatory_fails_on_invalid() -> None:
    edge_row = (_MIN_SCHEMA, False, None, _VALID_FROM_RECENT)
    factory = _schema_edge_factory(edge_row)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    # Missing required "version" field.
    valid, errors = await svc.validate_edge_properties(_ctx(), "requires", {}, _NOW)

    assert valid is False
    assert len(errors) >= 1


@pytest.mark.asyncio
async def test_validate_edge_properties_advisory_within_window_returns_warning() -> None:
    # is_advisory=True, advisory_until in the future → still advisory.
    edge_row = (_MIN_SCHEMA, True, _ADVISORY_FUTURE, _VALID_FROM_RECENT)
    factory = _schema_edge_factory(edge_row)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    valid, messages = await svc.validate_edge_properties(_ctx(), "requires", {}, _NOW)

    assert valid is True
    assert len(messages) == 1
    assert "advisory" in messages[0].lower()


@pytest.mark.asyncio
async def test_validate_edge_properties_advisory_expired_mandatory_enforcement() -> None:
    # is_advisory=True but advisory_until is in the past → mandatory.
    edge_row = (_MIN_SCHEMA, True, _ADVISORY_PAST, _VALID_FROM_OLD)
    factory = _schema_edge_factory(edge_row)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    valid, errors = await svc.validate_edge_properties(_ctx(), "requires", {}, _NOW)

    assert valid is False
    assert len(errors) >= 1


@pytest.mark.asyncio
async def test_validate_edge_properties_advisory_no_until_within_30d_is_advisory() -> None:
    # is_advisory=True, advisory_until=None, t_valid_from=5 days ago → within 30d window.
    edge_row = (_MIN_SCHEMA, True, None, _VALID_FROM_RECENT)
    factory = _schema_edge_factory(edge_row)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    valid, messages = await svc.validate_edge_properties(_ctx(), "composes", {}, _NOW)

    assert valid is True
    assert messages  # advisory warning


@pytest.mark.asyncio
async def test_validate_edge_properties_advisory_no_until_after_30d_is_mandatory() -> None:
    # is_advisory=True, advisory_until=None, t_valid_from=40 days ago → past 30d → mandatory.
    edge_row = (_MIN_SCHEMA, True, None, _VALID_FROM_OLD)
    factory = _schema_edge_factory(edge_row)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    valid, errors = await svc.validate_edge_properties(_ctx(), "composes", {}, _NOW)

    assert valid is False
    assert errors


@pytest.mark.asyncio
async def test_validate_edge_properties_passes_with_no_errors() -> None:
    # Valid properties even with advisory schema → (True, []).
    edge_row = (_MIN_SCHEMA, True, _ADVISORY_FUTURE, _VALID_FROM_RECENT)
    factory = _schema_edge_factory(edge_row)
    clock = FakeClock(_NOW)
    svc = SchemaService(factory, clock)

    valid, messages = await svc.validate_edge_properties(_ctx(), "requires", {"version": "2.0.0"}, _NOW)

    assert valid is True
    assert messages == []
