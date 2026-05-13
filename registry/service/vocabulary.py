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

# Migrations seed system vocabulary (entity_type, fact_category, edge_rel,
# annotation_category, annotation_status, …) under this fixed UUID with
# is_system=TRUE. Every tenant inherits those rows transparently — without
# this, a freshly-provisioned tenant could not create a single capability.
_SYSTEM_TENANT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


class VocabularyService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def validate_value(self, ctx: TenantContext, kind: str, value: str) -> None:
        """Reject if the (kind, value) pair is unknown or has been deprecated.

        A value is acceptable when either:
        - the caller's own tenant has a row matching (kind, value), or
        - the system tenant has a row matching (kind, value) with
          ``is_system=TRUE`` (every tenant inherits the seeded vocabulary).

        Tenant-local rows take precedence over system rows so a tenant can
        deprecate a value for themselves without removing the system seed.
        Implemented as two sequential ``scalar_one_or_none`` queries (rather
        than one OR-joined query) so unit-test mocks that only stub
        ``scalar_one_or_none`` continue to work without each caller's mock
        having to wire ``scalars().all()``.
        """
        async with self._session_factory() as session:
            tenant_result = await session.execute(
                select(VocabularyValue).where(
                    VocabularyValue.tenant_id == ctx.tenant_id,
                    VocabularyValue.kind == kind,
                    VocabularyValue.value == value,
                )
            )
            row = tenant_result.scalar_one_or_none()
            if row is None:
                system_result = await session.execute(
                    select(VocabularyValue).where(
                        VocabularyValue.tenant_id == _SYSTEM_TENANT_UUID,
                        VocabularyValue.is_system.is_(True),
                        VocabularyValue.kind == kind,
                        VocabularyValue.value == value,
                    )
                )
                row = system_result.scalar_one_or_none()
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
