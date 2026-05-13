"""Unit tests — RBAC and OIDC ORM model column shapes.

Verifies that the Role, ActorRole, RateLimit, and Actor ORM models expose
the expected columns and accept the expected constructor arguments. These
tests guard against accidental column renames or removals that would break
service-layer code without a DB migration.
"""

from __future__ import annotations

import uuid


def test_role_model_columns() -> None:
    from registry.storage.models import Role

    r = Role(
        role_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="admin",
        permissions=["read", "write"],
        created_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
    )
    assert r.name == "admin"
    assert r.permissions == ["read", "write"]


def test_actor_role_model_columns() -> None:
    from registry.storage.models import ActorRole

    tid = uuid.uuid4()
    ar = ActorRole(
        tenant_id=tid,
        actor_id=uuid.uuid4(),
        role_id=uuid.uuid4(),
        granted_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        granted_by=None,
    )
    assert ar.tenant_id == tid
    assert ar.granted_by is None


def test_rate_limit_model_columns() -> None:
    from registry.storage.models import RateLimit

    rl = RateLimit(
        limit_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        actor_id=None,
        reads_per_second=100,
        writes_per_second=10,
        created_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
    )
    assert rl.actor_id is None
    assert rl.reads_per_second == 100


def test_actor_has_oidc_subject_column() -> None:
    from registry.storage.models import Actor

    a = Actor.__table__.c  # type: ignore[attr-defined]
    col_names = {c.name for c in a}
    assert "oidc_subject" in col_names


def test_role_actor_role_rate_limit_in_models_all() -> None:
    """Spot-check __all__ in models is not explicitly excluding new classes."""
    import registry.storage.models as m

    assert hasattr(m, "Role")
    assert hasattr(m, "ActorRole")
    assert hasattr(m, "RateLimit")
