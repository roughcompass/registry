"""VocabularyService — controlled-vocabulary enforcement.

`validate_edge_rel` validates an edge `rel` value against the `edge_rel`
vocabulary kind, including requires, conflicts_with, composes, and
provides_to, as well as any tenant-registered custom values.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import VocabularyError
from registry.storage.models import VocabularyValue
from registry.types import TenantContext


class VocabularyService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def validate_value(self, ctx: TenantContext, kind: str, value: str) -> None:
        """Reject if the (kind, value) pair is unknown to this tenant or has been deprecated."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(VocabularyValue).where(
                    VocabularyValue.tenant_id == ctx.tenant_id,
                    VocabularyValue.kind == kind,
                    VocabularyValue.value == value,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            msg = f"unknown vocabulary value: kind={kind!r} value={value!r}"
            raise VocabularyError(msg)
        if row.deprecated_at is not None:
            msg = f"deprecated vocabulary value: kind={kind!r} value={value!r}"
            raise VocabularyError(msg)

    async def validate_edge_rel(self, ctx: TenantContext, edge_rel: str) -> None:
        """Validate an edge `rel` value against the `edge_rel` vocabulary kind.

        Accepts requires, conflicts_with, composes, provides_to, as well as any
        tenant-local edge_rel values registered via :meth:`add_value`.
        Raises :class:`~registry.exceptions.VocabularyError` for unknown or
        deprecated values.
        """
        await self.validate_value(ctx, "edge_rel", edge_rel)

    async def add_value(self, ctx: TenantContext, kind: str, value: str) -> None:
        """Insert a non-system row. Idempotent on duplicate (no error if already present)."""
        async with self._session_factory() as session, session.begin():
            existing = await session.execute(
                select(VocabularyValue).where(
                    VocabularyValue.tenant_id == ctx.tenant_id,
                    VocabularyValue.kind == kind,
                    VocabularyValue.value == value,
                )
            )
            if existing.scalar_one_or_none() is not None:
                return
            session.add(
                VocabularyValue(
                    vocab_id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    kind=kind,
                    value=value,
                    is_system=False,
                    deprecated_at=None,
                    created_at=datetime.datetime.now(tz=datetime.UTC),
                )
            )


__all__ = ["VocabularyService"]
