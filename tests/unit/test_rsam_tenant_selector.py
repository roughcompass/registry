"""Unit tests — RSAM X-Tenant-ID / X-SEAL-ID selector middleware.

Covers the six required scenarios:
1. Zero grants → 403 no_tenant_grants.
2. Single grant, no header → auto-select; TenantContext set correctly.
3. Multiple grants, no header → 400 tenant_context_required; body lists both SEAL IDs.
4. Multiple grants, X-Tenant-ID header → matching grant selected.
5. Multiple grants, X-SEAL-ID alias header → matching grant selected.
6. Multiple grants, unrecognised header value → 403 tenant_not_authorized.

All tests call `_select_rsam_tenant` directly with a fabricated Request-like
object and a `ResolvedIdentity`.  No DB, no live HTTP server — pure unit tests.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from registry.api.middleware.tenant import _select_rsam_tenant
from registry.auth.resolver import AuditIdentity, ResolvedIdentity, TenantGrant
from registry.config import Settings

# ---------------------------------------------------------------------------
# Helpers

_SEAL_A = "112025"
_SEAL_B = "34612"

_UUID_A = uuid.uuid4()
_UUID_B = uuid.uuid4()


def _make_settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://x/y",
        pgbouncer_url="postgresql+asyncpg://x/y",
        scheduler_jobstore_url="postgresql+asyncpg://x/y",
        auth_mode="rsam",
        auth_claim_source_url="https://rsam.example.com",
        auth_tenant_id_header="X-Tenant-ID",
        auth_seal_id_header_alias="X-SEAL-ID",
    )
    base.update(overrides)
    return Settings(**base)


def _make_request(headers: dict[str, str] | None = None, settings: Settings | None = None) -> MagicMock:
    """Build a minimal mock of a FastAPI `Request` with the given headers and settings."""
    req = MagicMock()
    raw_headers = headers or {}
    # Request.headers.get(name) — case-insensitive in real Starlette, exact-match here is fine
    # for test purposes since we control the test inputs.
    req.headers.get = lambda key, default=None: raw_headers.get(key, default)
    req.app.state.settings = settings or _make_settings()
    return req


def _make_grant(seal_id: str, tenant_id: uuid.UUID, role: str = "viewer") -> TenantGrant:
    return TenantGrant(
        tenant_id=tenant_id,
        tenant_external_id=seal_id,
        catalog_role=role,
    )


def _make_identity(grants: list[TenantGrant]) -> ResolvedIdentity:
    return ResolvedIdentity(
        user_id="ida-user-1",
        tenant_grants=grants,
        audit_identity=AuditIdentity(sub="ida-user-1", email=None, preferred_username="ida-user-1"),
    )


# ---------------------------------------------------------------------------
# Scenario 1: zero grants → 403 no_tenant_grants


def test_zero_grants_returns_403_no_tenant_grants():
    request = _make_request()
    identity = _make_identity([])

    with pytest.raises(HTTPException) as exc_info:
        _select_rsam_tenant(request, identity)

    assert exc_info.value.status_code == 403
    detail = exc_info.value.detail
    assert detail["error"] == "no_tenant_grants"


# ---------------------------------------------------------------------------
# Scenario 2: single grant, no header → auto-select


def test_single_grant_no_header_auto_selects():
    request = _make_request(headers={})
    grant = _make_grant(_SEAL_A, _UUID_A, role="producer")
    identity = _make_identity([grant])

    ctx = _select_rsam_tenant(request, identity)

    assert ctx.tenant_id == _UUID_A
    assert "producer" in ctx.roles


# ---------------------------------------------------------------------------
# Scenario 3: multiple grants, no header → 400 tenant_context_required with both SEAL IDs


def test_multiple_grants_no_header_returns_400_with_seal_ids():
    request = _make_request(headers={})
    grants = [
        _make_grant(_SEAL_A, _UUID_A),
        _make_grant(_SEAL_B, _UUID_B),
    ]
    identity = _make_identity(grants)

    with pytest.raises(HTTPException) as exc_info:
        _select_rsam_tenant(request, identity)

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["error"] == "tenant_context_required"
    available = detail["available_tenant_ids"]
    assert _SEAL_A in available
    assert _SEAL_B in available


# ---------------------------------------------------------------------------
# Scenario 4: multiple grants, X-Tenant-ID header → matching grant selected


def test_multiple_grants_primary_header_selects_correct_grant():
    request = _make_request(headers={"X-Tenant-ID": _SEAL_A})
    grants = [
        _make_grant(_SEAL_A, _UUID_A, role="admin"),
        _make_grant(_SEAL_B, _UUID_B, role="viewer"),
    ]
    identity = _make_identity(grants)

    ctx = _select_rsam_tenant(request, identity)

    assert ctx.tenant_id == _UUID_A
    assert "admin" in ctx.roles


# ---------------------------------------------------------------------------
# Scenario 5: multiple grants, X-SEAL-ID alias → matching grant selected


def test_multiple_grants_alias_header_selects_correct_grant():
    request = _make_request(headers={"X-SEAL-ID": _SEAL_B})
    grants = [
        _make_grant(_SEAL_A, _UUID_A, role="viewer"),
        _make_grant(_SEAL_B, _UUID_B, role="auditor"),
    ]
    identity = _make_identity(grants)

    ctx = _select_rsam_tenant(request, identity)

    assert ctx.tenant_id == _UUID_B
    assert "auditor" in ctx.roles


# ---------------------------------------------------------------------------
# Scenario 6: multiple grants, unrecognised header value → 403 tenant_not_authorized


def test_multiple_grants_unknown_header_value_returns_403():
    request = _make_request(headers={"X-Tenant-ID": "99999"})
    grants = [
        _make_grant(_SEAL_A, _UUID_A),
        _make_grant(_SEAL_B, _UUID_B),
    ]
    identity = _make_identity(grants)

    with pytest.raises(HTTPException) as exc_info:
        _select_rsam_tenant(request, identity)

    assert exc_info.value.status_code == 403
    detail = exc_info.value.detail
    assert detail["error"] == "tenant_not_authorized"


# ---------------------------------------------------------------------------
# Bonus: primary header wins when both headers are present


def test_primary_header_wins_over_alias():
    # Both headers present: primary should win
    request = _make_request(headers={"X-Tenant-ID": _SEAL_A, "X-SEAL-ID": _SEAL_B})
    grants = [
        _make_grant(_SEAL_A, _UUID_A, role="admin"),
        _make_grant(_SEAL_B, _UUID_B, role="viewer"),
    ]
    identity = _make_identity(grants)

    ctx = _select_rsam_tenant(request, identity)

    # Primary header (_SEAL_A) should win
    assert ctx.tenant_id == _UUID_A


# ---------------------------------------------------------------------------
# Alias disabled (auth_seal_id_header_alias=None) → alias header ignored


def test_alias_disabled_alias_header_ignored():
    settings = _make_settings(auth_seal_id_header_alias=None)
    request = _make_request(headers={"X-SEAL-ID": _SEAL_A}, settings=settings)
    grants = [
        _make_grant(_SEAL_A, _UUID_A),
        _make_grant(_SEAL_B, _UUID_B),
    ]
    identity = _make_identity(grants)

    # With alias disabled, sending only X-SEAL-ID is equivalent to no header → 400
    with pytest.raises(HTTPException) as exc_info:
        _select_rsam_tenant(request, identity)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "tenant_context_required"
