"""Unit tests for VocabularyService — mocked session; no DB."""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import VocabularyError
from registry.service.vocabulary import VocabularyService
from registry.storage.models import VocabularyValue
from registry.types import TenantContext


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["producer"])


def _make_session_returning(row: VocabularyValue | None) -> AsyncMock:
    """Build an AsyncMock that mimics `async with factory() as session: session.execute(...)`."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=session)
    return factory


def _make_vocab_row(*, deprecated: bool = False) -> VocabularyValue:
    row = VocabularyValue()
    row.vocab_id = uuid.uuid4()
    row.tenant_id = uuid.uuid4()
    row.kind = "entity_type"
    row.value = "capability"
    row.is_system = True
    row.deprecated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC) if deprecated else None
    return row


@pytest.mark.asyncio
async def test_validate_value_passes_for_active_row() -> None:
    factory = _make_session_returning(_make_vocab_row())
    service = VocabularyService(factory)
    await service.validate_value(_ctx(), "entity_type", "capability")  # no raise


@pytest.mark.asyncio
async def test_validate_value_rejects_unknown_kind() -> None:
    factory = _make_session_returning(None)
    service = VocabularyService(factory)
    with pytest.raises(VocabularyError, match="unknown"):
        await service.validate_value(_ctx(), "entity_type", "ghost")


@pytest.mark.asyncio
async def test_validate_value_rejects_deprecated_row() -> None:
    factory = _make_session_returning(_make_vocab_row(deprecated=True))
    service = VocabularyService(factory)
    with pytest.raises(VocabularyError, match="deprecated"):
        await service.validate_value(_ctx(), "entity_type", "capability")
