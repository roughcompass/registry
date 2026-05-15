"""Unit tests for shared types — TenantContext + TenantMembership.

Verifies the additive shape: legacy three-field constructors keep
working, the new entitlement-service-resolved fields are present with
safe defaults, and ``selected_tenant_id`` aliases ``tenant_id``.
"""

from __future__ import annotations

import uuid

import pytest

from registry.types import TenantContext, TenantMembership


class TestLegacyConstructor:
    """Existing call sites construct TenantContext with the original
    three fields. That pattern must keep working."""

    def test_three_field_constructor(self):
        tid = uuid.uuid4()
        aid = uuid.uuid4()
        ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"])
        assert ctx.tenant_id == tid
        assert ctx.actor_id == aid
        assert ctx.roles == ["admin"]

    def test_legacy_defaults_for_new_fields(self):
        ctx = TenantContext(
            tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=[]
        )
        assert ctx.oidc_subject == ""
        assert ctx.tenant_memberships == []

    def test_in_check_on_roles(self):
        """RBAC guards do `if "admin" in ctx.roles` — must work with list."""
        ctx = TenantContext(
            tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=["admin", "producer"],
        )
        assert "admin" in ctx.roles
        assert "auditor" not in ctx.roles


class TestNewFields:
    def test_with_oidc_subject(self):
        ctx = TenantContext(
            tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=[],
            oidc_subject="user-1",
        )
        assert ctx.oidc_subject == "user-1"

    def test_with_tenant_memberships(self):
        memberships = [
            TenantMembership(
                tenant_id=uuid.uuid4(),
                tenant_slug="111",
                roles=frozenset({"admin"}),
            ),
            TenantMembership(
                tenant_id=uuid.uuid4(),
                tenant_slug="222",
                roles=frozenset({"consumer"}),
            ),
        ]
        ctx = TenantContext(
            tenant_id=memberships[0].tenant_id,
            actor_id=uuid.uuid4(),
            roles=["admin"],
            tenant_memberships=memberships,
        )
        assert len(ctx.tenant_memberships) == 2
        assert ctx.tenant_memberships[0].tenant_slug == "111"


class TestSelectedTenantIdAlias:
    def test_returns_tenant_id(self):
        tid = uuid.uuid4()
        ctx = TenantContext(tenant_id=tid, actor_id=uuid.uuid4(), roles=[])
        assert ctx.selected_tenant_id == tid

    def test_alias_returns_same_value_for_every_access(self):
        tid = uuid.uuid4()
        ctx = TenantContext(tenant_id=tid, actor_id=uuid.uuid4(), roles=[])
        assert ctx.selected_tenant_id == ctx.tenant_id


class TestImmutability:
    def test_frozen_blocks_mutation(self):
        ctx = TenantContext(
            tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=[]
        )
        with pytest.raises(Exception):
            ctx.tenant_id = uuid.uuid4()  # type: ignore[misc]


class TestTenantMembershipShape:
    def test_frozen_dataclass(self):
        m = TenantMembership(
            tenant_id=uuid.uuid4(),
            tenant_slug="111",
            roles=frozenset({"admin"}),
        )
        with pytest.raises(Exception):
            m.tenant_slug = "222"  # type: ignore[misc]

    def test_equality(self):
        tid = uuid.uuid4()
        a = TenantMembership(
            tenant_id=tid, tenant_slug="111", roles=frozenset({"admin"})
        )
        b = TenantMembership(
            tenant_id=tid, tenant_slug="111", roles=frozenset({"admin"})
        )
        assert a == b

    def test_hashable(self):
        m = TenantMembership(
            tenant_id=uuid.uuid4(),
            tenant_slug="111",
            roles=frozenset({"admin"}),
        )
        # Frozen + frozenset roles → hashable; usable as dict key / set member.
        assert {m, m} == {m}
