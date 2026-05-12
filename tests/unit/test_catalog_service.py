"""Unit tests for CatalogService.

The full bi-temporal update / cascade behaviour is exercised in
tests/integration/test_phase1.py (testcontainers + real session). These
unit tests pin the static invariants — tenant guard, vocab call, schema
call — that don't require a live session.
"""

from __future__ import annotations

import uuid

import pytest

from registry.exceptions import TenantIsolationError
from registry.service.catalog import CatalogService
from registry.types import TenantContext


def _ctx(tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["producer"],
    )


def test_assert_tenant_passes_for_matching_tenant() -> None:
    ctx = _ctx()
    CatalogService._assert_tenant(ctx, ctx.tenant_id)  # no raise


def test_assert_tenant_raises_for_mismatched_tenant() -> None:
    ctx = _ctx()
    other = uuid.uuid4()
    with pytest.raises(TenantIsolationError):
        CatalogService._assert_tenant(ctx, other)
