"""Unit tests for RsamClaimSource — 15 scenarios.

All scenarios inject `fetch_authorities` at construction time (lambda or
AsyncMock). The database session and `upsert_rsam_tenant` are mocked so no
DB is required. Tests do not patch module-level names at collection time.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.auth.rsam.claim_source import RsamClaimSource
from registry.config import Settings

# ---------------------------------------------------------------------------
# Helpers


def _settings(auth_mode: str = "rsam") -> Settings:
    """Minimal Settings with required fields satisfied."""
    return Settings(
        database_url="postgresql+asyncpg://test/test",
        pgbouncer_url="postgresql+asyncpg://test/test",
        scheduler_jobstore_url="postgresql+asyncpg://test/test",
        auth_mode=auth_mode,
        auth_claim_source_url="https://rsam.example.com" if auth_mode == "rsam" else None,
    )


def _make_session_factory(
    actor_display_name: str | None = None,
    actor_email: str | None = None,
) -> MagicMock:
    """Build a mock session_factory.

    Satisfies both:
      `async with factory() as session, session.begin():`   (upsert loop)
      `async with factory() as session: session.execute(...)`  (AuditIdentity SELECT)

    `upsert_rsam_tenant` and `upsert_rsam_actor` are patched at call-site in each
    test. `session.execute` returns a result whose `.first()` yields
    `(actor_display_name, actor_email)` so the AuditIdentity SELECT after the loop
    sees a non-empty row by default.
    """
    session = AsyncMock()

    # session.begin() is used as a context manager alongside the outer factory()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    # session.execute(...) returns an object whose .first() yields the actor row.
    # Default is `(None, None)` — actor row exists (post-upsert it always does),
    # but display_name/email are NULL → AuditIdentity uses fallbacks.
    actor_row = (actor_display_name, actor_email)
    execute_result = MagicMock()
    execute_result.first = MagicMock(return_value=actor_row)
    session.execute = AsyncMock(return_value=execute_result)

    # factory() is used as an async context manager
    outer_cm = AsyncMock()
    outer_cm.__aenter__ = AsyncMock(return_value=session)
    outer_cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=outer_cm)
    return factory


def _claims(subject: str = "F731821") -> dict:
    return {"sub": subject}


# ---------------------------------------------------------------------------
# Scenario 1: Zero authorities → empty tenant_grants


@pytest.mark.asyncio
async def test_zero_authorities_returns_empty_grants() -> None:
    """Zero RSAM authorities → ResolvedIdentity with no tenant grants."""
    settings = _settings()
    factory = _make_session_factory()
    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=[]),
    )

    with patch("registry.auth.rsam.claim_source.upsert_rsam_tenant", AsyncMock()) as mock_upsert:
        result = await source.resolve(_claims())

    assert result.user_id == "F731821"
    assert result.tenant_grants == []
    mock_upsert.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 2: Single SEAL, one authority → one grant with admin role


@pytest.mark.asyncio
async def test_single_seal_owner_produces_admin_grant() -> None:
    """Single Owner authority → one TenantGrant with catalog_role='admin'."""
    settings = _settings()
    factory = _make_session_factory()
    tenant_uuid = uuid.uuid4()

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=["112025_DP_CHANNEL_Owner"]),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
    ):
        result = await source.resolve(_claims())

    assert len(result.tenant_grants) == 1
    grant = result.tenant_grants[0]
    assert grant.tenant_external_id == "112025"
    assert grant.catalog_role == "admin"
    assert grant.tenant_id == tenant_uuid


# ---------------------------------------------------------------------------
# Scenario 3: Multi-SEAL, multiple authorities per SEAL → highest role wins


@pytest.mark.asyncio
async def test_multi_seal_highest_role_per_seal() -> None:
    """Multi-SEAL: 112025 gets producer (highest of Manager+Operate); 34612 gets producer (RU)."""
    settings = _settings()
    factory = _make_session_factory()

    uuid_112025 = uuid.uuid4()
    uuid_34612 = uuid.uuid4()

    def _side_effect(session: object, seal_id: str) -> uuid.UUID:
        return uuid_112025 if seal_id == "112025" else uuid_34612

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(
            return_value=[
                "112025_DP_CHANNEL_Manager",
                "112025_DP_MODULE_Operate",
                "34612_DP_MODULE_RU",
            ]
        ),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(side_effect=_side_effect),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
    ):
        result = await source.resolve(_claims())

    assert len(result.tenant_grants) == 2

    by_seal = {g.tenant_external_id: g for g in result.tenant_grants}

    assert "112025" in by_seal
    assert "34612" in by_seal

    # Manager=producer; Operate=auditor → highest is producer
    assert by_seal["112025"].catalog_role == "producer"
    # RU → producer
    assert by_seal["34612"].catalog_role == "producer"


# ---------------------------------------------------------------------------
# Scenario 4: Mixed valid and invalid authorities → invalid silently dropped


@pytest.mark.asyncio
async def test_invalid_authority_silently_dropped() -> None:
    """Non-matching authority strings are discarded; valid ones are resolved normally."""
    settings = _settings()
    factory = _make_session_factory()
    tenant_uuid = uuid.uuid4()

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(
            return_value=[
                "112025_DP_CHANNEL_Owner",
                "invalid_garbage",
            ]
        ),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
    ):
        result = await source.resolve(_claims())

    # Only the valid authority produces a grant
    assert len(result.tenant_grants) == 1
    assert result.tenant_grants[0].tenant_external_id == "112025"
    assert result.tenant_grants[0].catalog_role == "admin"


# ---------------------------------------------------------------------------
# Scenario 5: RSAM 5xx fail-closed → exception propagates


@pytest.mark.asyncio
async def test_rsam_5xx_fail_closed_propagates_exception() -> None:
    """When fetch_authorities raises, the exception propagates — never swallowed."""
    settings = _settings()
    factory = _make_session_factory()

    async def _failing_fetch(subject: str) -> list[str]:
        raise RuntimeError("simulated RSAM 503")

    source = RsamClaimSource(settings, factory, fetch_authorities=_failing_fetch)

    with pytest.raises(RuntimeError, match="simulated RSAM 503"):
        await source.resolve(_claims())


# ---------------------------------------------------------------------------
# Scenario 6: RSAM 5xx stale-serve opt-in — T06 still propagates without cache


@pytest.mark.asyncio
async def test_rsam_5xx_stale_serve_setting_does_not_suppress_exception() -> None:
    """With auth_serve_stale_on_failure=True but an empty cache, the resolver
    fails-closed — raising HTTP 503 rather than silently returning empty grants.

    The stale-on-failure path only serves cached data when a previous successful
    resolve populated the cache within the stale ceiling. On a cold cache there is
    nothing to serve, so the resolver raises 503 with a Retry-After header.
    """
    from fastapi import HTTPException

    settings = _settings()
    settings_with_stale = Settings(
        database_url=settings.database_url,
        pgbouncer_url=settings.pgbouncer_url,
        scheduler_jobstore_url=settings.scheduler_jobstore_url,
        auth_mode="rsam",
        auth_claim_source_url="https://rsam.example.com",
        auth_serve_stale_on_failure=True,
    )
    factory = _make_session_factory()

    async def _failing_fetch(subject: str) -> list[str]:
        raise RuntimeError("simulated RSAM 503 with stale-serve enabled")

    source = RsamClaimSource(
        settings_with_stale,
        factory,
        fetch_authorities=_failing_fetch,
    )

    with pytest.raises(HTTPException) as exc_info:
        await source.resolve(_claims())

    assert exc_info.value.status_code == 503
    assert "Retry-After" in exc_info.value.headers


# ---------------------------------------------------------------------------
# Scenario 7: audit_identity.email is None (actors-table lookup is a later task)


@pytest.mark.asyncio
async def test_audit_identity_email_is_none() -> None:
    """audit_identity.email is None — actors-table lookup is handled by a later task."""
    settings = _settings()
    factory = _make_session_factory()
    tenant_uuid = uuid.uuid4()

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=["112025_DP_CHANNEL_Owner"]),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
    ):
        result = await source.resolve(_claims(subject="F731821"))

    assert result.audit_identity is not None
    assert result.audit_identity.email is None


# ---------------------------------------------------------------------------
# Scenario 8: audit_identity.preferred_username falls back to subject


@pytest.mark.asyncio
async def test_audit_identity_preferred_username_falls_back_to_subject() -> None:
    """preferred_username equals the subject string when no actors-table lookup runs."""
    settings = _settings()
    factory = _make_session_factory()
    tenant_uuid = uuid.uuid4()
    subject = "F731821"

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=["112025_DP_CHANNEL_Owner"]),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
    ):
        result = await source.resolve(_claims(subject=subject))

    assert result.audit_identity is not None
    assert result.audit_identity.preferred_username == subject
    assert result.audit_identity.sub == subject


# ---------------------------------------------------------------------------
# Scenario 9: First-time RSAM user creates both tenants AND actors row


@pytest.mark.asyncio
async def test_first_time_user_creates_tenant_and_actor() -> None:
    """First login: upsert_rsam_tenant and upsert_rsam_actor both called exactly once."""
    settings = _settings()
    factory = _make_session_factory(actor_display_name=None, actor_email=None)
    tenant_uuid = uuid.uuid4()

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=["112025_DP_CHANNEL_Owner"]),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ) as mock_tenant,
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ) as mock_actor,
    ):
        result = await source.resolve(_claims(subject="F731821"))

    mock_tenant.assert_awaited_once()
    mock_actor.assert_awaited_once()
    # Actor upsert called with the resolved tenant_id + the JWT subject
    _, kwargs = mock_actor.call_args
    args, _ = mock_actor.call_args
    # Positional args: (session, tenant_id, oidc_subject)
    assert tenant_uuid in args
    assert "F731821" in args
    assert result.audit_identity.sub == "F731821"


# ---------------------------------------------------------------------------
# Scenario 10: Multi-SEAL user creates N actor rows, one per tenant


@pytest.mark.asyncio
async def test_multi_seal_creates_actor_per_tenant() -> None:
    """Two SEALs → upsert_rsam_actor called twice (once per tenant)."""
    settings = _settings()
    factory = _make_session_factory()

    uuid_a = uuid.uuid4()
    uuid_b = uuid.uuid4()

    def _tenant_se(session: object, seal_id: str) -> uuid.UUID:
        return uuid_a if seal_id == "112025" else uuid_b

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(
            return_value=[
                "112025_DP_CHANNEL_Owner",
                "34612_DP_MODULE_RU",
            ]
        ),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(side_effect=_tenant_se),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ) as mock_actor,
    ):
        result = await source.resolve(_claims())

    assert len(result.tenant_grants) == 2
    assert mock_actor.await_count == 2


# ---------------------------------------------------------------------------
# Scenario 11: AuditIdentity populated from actors-table row (real values)


@pytest.mark.asyncio
async def test_audit_identity_populated_from_actor_row() -> None:
    """When the actors row has non-NULL display_name + email, AuditIdentity reflects them."""
    settings = _settings()
    factory = _make_session_factory(
        actor_display_name="Real Name",
        actor_email="user@example.com",
    )
    tenant_uuid = uuid.uuid4()

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=["112025_DP_CHANNEL_Owner"]),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
    ):
        result = await source.resolve(_claims(subject="F731821"))

    assert result.audit_identity.sub == "F731821"
    assert result.audit_identity.email == "user@example.com"
    assert result.audit_identity.preferred_username == "Real Name"


# ---------------------------------------------------------------------------
# Scenario 12: Missing actor row after upsert is a programming error


@pytest.mark.asyncio
async def test_missing_actor_row_raises_runtime_error() -> None:
    """A None result.first() from the actors SELECT is a programming-error path."""
    settings = _settings()
    factory = _make_session_factory()
    # Override execute_result.first to return None — simulates an impossible state
    session_outer_cm = factory.return_value
    session = session_outer_cm.__aenter__.return_value
    session.execute.return_value.first = MagicMock(return_value=None)

    tenant_uuid = uuid.uuid4()
    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=["112025_DP_CHANNEL_Owner"]),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
    ):
        with pytest.raises(RuntimeError, match="actor row missing"):
            await source.resolve(_claims())


# ---------------------------------------------------------------------------
# Audit tests (prefix "test_audit_" so pytest -k "audit" selects them)


def _extract_audit_call_params(session: AsyncMock, action: str) -> dict | None:
    """Scan all session.execute calls for one whose SQL text contains action.

    The action string is embedded directly in the SQL text (not a bind param)
    for the auth.claim_source.invoked event. The `after_jsonb` field is a JSON
    string — parse it into a dict for assertions. Returns None if not found.
    """
    for c in session.execute.call_args_list:
        positional = c[0]
        if not positional:
            continue
        sql_text = str(positional[0])  # the text() object or string SQL
        if action not in sql_text:
            continue
        params = positional[1] if len(positional) > 1 else {}
        if not isinstance(params, dict):
            continue
        after_raw = params.get("after_jsonb", "{}")
        params_copy = dict(params)
        params_copy["after_jsonb"] = json.loads(after_raw)
        return params_copy
    return None


@pytest.mark.asyncio
async def test_audit_claim_source_invoked_emitted_with_payload() -> None:
    """resolve() emits an auth.claim_source.invoked structured log with correct fields.

    The audit_log schema requires non-null target_type / target_id which cannot
    be populated before tenant resolution completes. Structured logging is the
    observability surface for this event; no DB audit row is written.
    """
    settings = _settings()
    factory = _make_session_factory()
    tenant_uuid = uuid.uuid4()
    subject = "F731821"
    raw_authorities = ["112025_DP_CHANNEL_Owner", "34612_DP_MODULE_RU"]

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AsyncMock(return_value=raw_authorities),
    )

    with (
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_tenant",
            AsyncMock(return_value=tenant_uuid),
        ),
        patch(
            "registry.auth.rsam.claim_source.upsert_rsam_actor",
            AsyncMock(),
        ),
        patch("registry.auth.rsam.claim_source._log") as mock_log,
    ):
        await source.resolve(_claims(subject=subject))

    # Verify the structured log call contains the expected fields.
    mock_log.info.assert_called_once()
    call_args = mock_log.info.call_args
    log_fmt = call_args[0][0]  # format string
    log_positional = call_args[0][1:]  # positional args
    assert "auth.claim_source.invoked" in log_fmt
    # Positional args: subject, latency_ms, authority_count
    assert log_positional[0] == subject
    assert isinstance(log_positional[1], int)
    assert log_positional[1] >= 0
    assert log_positional[2] == len(raw_authorities)


@pytest.mark.asyncio
async def test_audit_tenant_jit_created_payload_complete() -> None:
    """tenant.jit_created payload includes provider='jit' and source='rsam'.

    The event is emitted inside upsert_rsam_tenant (tenant_store.py). This
    test calls resolve() with a brand-new SEAL and inspects the SQL params
    passed to the session mock to confirm all four required keys are present.
    """
    settings = _settings()
    tenant_uuid = uuid.uuid4()
    seal_id = "112025"

    # Build a session factory and capture the session used by upsert_rsam_tenant.
    # We need the REAL upsert_rsam_tenant to run so we can inspect its audit write,
    # so we do NOT patch it. Instead we make the session mock satisfy both the
    # INSERT RETURNING and the audit INSERT calls.
    from unittest.mock import AsyncMock as AM
    from unittest.mock import MagicMock as MM

    # Session that returns our fixed tenant_id on the first INSERT RETURNING call.
    def _row():
        r = MM()
        r.__getitem__ = MM(side_effect=lambda i: tenant_uuid if i == 0 else None)
        return r

    insert_result = MM()
    insert_result.fetchone = MM(return_value=_row())

    audit_result = MM()
    audit_result.fetchone = MM(return_value=None)

    # actor upsert: INSERT RETURNING (no row — actor already exists) + SELECT
    actor_no_row = MM()
    actor_no_row.fetchone = MM(return_value=None)

    # The AuditIdentity SELECT needs a row too.
    actor_row = (seal_id, None)
    actor_select_result = MM()
    actor_select_result.fetchone = MM(return_value=actor_row)
    actor_select_result.first = MM(return_value=actor_row)

    # Audit-claim-source INSERT (auth.claim_source.invoked).
    claim_audit_result = MM()
    claim_audit_result.fetchone = MM(return_value=None)

    session = AM()
    session.execute = AM(
        side_effect=[
            insert_result,  # upsert_rsam_tenant: INSERT ... RETURNING
            audit_result,  # upsert_rsam_tenant: INSERT INTO audit_log (tenant.jit_created)
            actor_no_row,  # upsert_rsam_actor: INSERT ... RETURNING (DO NOTHING)
            actor_select_result,  # upsert_rsam_actor: SELECT to find existing actor
            actor_select_result,  # _build_audit_identity SELECT
            claim_audit_result,  # auth.claim_source.invoked audit INSERT
        ]
    )
    begin_cm = AM()
    begin_cm.__aenter__ = AM(return_value=None)
    begin_cm.__aexit__ = AM(return_value=False)
    session.begin = MM(return_value=begin_cm)

    outer_cm = AM()
    outer_cm.__aenter__ = AM(return_value=session)
    outer_cm.__aexit__ = AM(return_value=False)
    factory = MM(return_value=outer_cm)

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AM(return_value=[f"{seal_id}_DP_CHANNEL_Owner"]),
    )
    await source.resolve(_claims(subject="F731821"))

    # Inspect the audit INSERT for tenant.jit_created (second execute call).
    audit_call = session.execute.call_args_list[1]
    params = audit_call[0][1]
    assert params["tenant_id"] == tenant_uuid
    after = json.loads(params["after_jsonb"])
    assert after["tenant_id"] == str(tenant_uuid)
    assert after["external_tenant_id"] == seal_id
    assert after["provider"] == "jit"
    assert after["source"] == "rsam"


@pytest.mark.asyncio
async def test_audit_actor_jit_created_payload_complete() -> None:
    """actor.jit_created payload includes oidc_subject and source='rsam'.

    Calls resolve() with a brand-new user so upsert_rsam_actor takes the
    INSERT path, and inspects the resulting audit_log INSERT params.
    """
    settings = _settings()
    tenant_uuid = uuid.uuid4()
    actor_uuid = uuid.uuid4()
    seal_id = "112025"
    subject = "F731821"

    from unittest.mock import AsyncMock as AM
    from unittest.mock import MagicMock as MM

    def _tenant_row():
        r = MM()
        r.__getitem__ = MM(side_effect=lambda i: tenant_uuid if i == 0 else None)
        return r

    def _actor_row_obj():
        r = MM()
        r.__getitem__ = MM(side_effect=lambda i: actor_uuid if i == 0 else None)
        return r

    tenant_insert_result = MM()
    tenant_insert_result.fetchone = MM(return_value=_tenant_row())

    tenant_audit_result = MM()
    tenant_audit_result.fetchone = MM(return_value=None)

    actor_insert_result = MM()
    actor_insert_result.fetchone = MM(return_value=_actor_row_obj())

    actor_audit_result = MM()
    actor_audit_result.fetchone = MM(return_value=None)

    # AuditIdentity SELECT.
    audit_identity_row = (subject, None)
    audit_identity_result = MM()
    audit_identity_result.first = MM(return_value=audit_identity_row)

    claim_audit_result = MM()
    claim_audit_result.fetchone = MM(return_value=None)

    session = AM()
    session.execute = AM(
        side_effect=[
            tenant_insert_result,  # upsert_rsam_tenant INSERT RETURNING
            tenant_audit_result,  # tenant.jit_created audit INSERT
            actor_insert_result,  # upsert_rsam_actor INSERT RETURNING
            actor_audit_result,  # actor.jit_created audit INSERT
            audit_identity_result,  # _build_audit_identity SELECT
            claim_audit_result,  # auth.claim_source.invoked audit INSERT
        ]
    )
    begin_cm = AM()
    begin_cm.__aenter__ = AM(return_value=None)
    begin_cm.__aexit__ = AM(return_value=False)
    session.begin = MM(return_value=begin_cm)

    outer_cm = AM()
    outer_cm.__aenter__ = AM(return_value=session)
    outer_cm.__aexit__ = AM(return_value=False)
    factory = MM(return_value=outer_cm)

    source = RsamClaimSource(
        settings,
        factory,
        fetch_authorities=AM(return_value=[f"{seal_id}_DP_CHANNEL_Owner"]),
    )
    await source.resolve(_claims(subject=subject))

    # actor.jit_created is the 4th execute call (index 3).
    actor_audit_call = session.execute.call_args_list[3]
    params = actor_audit_call[0][1]
    assert params["tenant_id"] == tenant_uuid
    after = json.loads(params["after_jsonb"])
    assert after["actor_id"] == str(actor_uuid)
    assert after["tenant_id"] == str(tenant_uuid)
    assert after["oidc_subject"] == subject
    assert after["source"] == "rsam"
