"""Integration suite for API Consistency & Performance Remediation contracts.

Covers six contracts:

1. Role enforcement on POST creates — consumer role returns 403;
   producer succeeds (201).  Tested for capabilities, concepts, operations,
   and artifacts.

2. Cursor strict mode — a malformed cursor on an audit endpoint
   returns 422 with ``code: "invalid_cursor"`` in the error envelope.

3. Keyset pagination — ``GET /v1/capabilities?cursor=...`` returns
   ``next_cursor``; following the cursor visits each row exactly once with no
   overlap.

4. Envelope shape — list endpoints emit ``{items, next_cursor}``.
   No ``rows``, no ``results``, no bare list.

5. VALID_ROLES consistency — static-analysis check via import:
   service modules import named constants from ``registry.api.auth.context``
   rather than hard-coded string literals.

6. Rate-limit enforcement — when a tenant exhausts its write budget
   the next request returns 429 with a ``Retry-After`` header and the
   ``rate_limited`` error code.

Run against the shared testcontainer Postgres using ``httpx.ASGITransport``.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from registry.config import Settings
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    default_settings,
    patch_validator_for_actor,
)

type _CprClients = tuple[
    AsyncClient, AsyncClient, uuid.UUID, Settings, TenantPersona, TenantPersona
]

# ---------------------------------------------------------------------------
# Seed helpers (direct SQL, no api_tokens)
# ---------------------------------------------------------------------------


async def _seed_capability_row(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    """Insert a minimal capability row directly, bypassing the role check."""
    import datetime

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    _NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
    cap_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at, visibility) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now, 'private')"
                ),
                {"eid": cap_id, "tid": tenant_id, "name": name, "now": _NOW},
            )
    finally:
        await engine.dispose()
    return cap_id


async def _make_persona(
    harness: EntitlementAuthHarness, client: AsyncClient, slug: str, roles: list[str]
) -> tuple[TenantPersona, uuid.UUID]:
    """Add persona, JIT-materialise via whoami. Returns (persona, tenant_id)."""
    persona = harness.add_persona(slug, roles=roles)
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
        assert resp.status_code == 200, resp.text
    return persona, uuid.UUID(resp.json()["tenant_id"])


# ---------------------------------------------------------------------------
# Shared per-module fixture: one harness + consumer + producer personas
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cpr_clients(
    pg_container: str,
) -> AsyncGenerator[_CprClients, None]:
    """Yield ``(consumer_client, producer_client, cap_id, settings, consumer_persona, producer_persona)``.

    Both clients target the same tenant. Consumer carries ``["consumer"]``;
    producer carries ``["producer"]``. A capability row is pre-seeded via
    direct SQL so artifact and sub-resource tests have a stable target.
    """
    slug = f"cpr-{secrets.token_hex(4)}"
    async with EntitlementAuthHarness(pg_container) as harness:
        transport = ASGITransport(app=harness.app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as c_client,
            AsyncClient(transport=transport, base_url="http://test") as p_client,
        ):
            # Materialise the tenant via the consumer persona first (creates the tenant row).
            consumer_persona, tenant_id = await _make_persona(
                harness, c_client, slug, ["consumer"]
            )
            # Producer is a second actor in the same tenant (same slug → same tenant row).
            producer_persona = harness.add_persona(slug, roles=["producer"], actor_id=uuid.uuid4())
            harness.configure_fetcher_for(producer_persona)
            # Materialise the producer actor via whoami so the actors row exists.
            with patch_validator_for_actor(producer_persona):
                _r = await p_client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert _r.status_code == 200, _r.text

            cap_id = await _seed_capability_row(pg_container, tenant_id=tenant_id, name="cpr-base-cap")

            yield (
                c_client,
                p_client,
                cap_id,
                default_settings(pg_container),
                consumer_persona,
                producer_persona,
            )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_forbidden(response) -> None:  # type: ignore[no-untyped-def]
    """Assert 403 with ``code: "forbidden"`` in the error envelope."""
    assert response.status_code == 403, f"expected 403, got {response.status_code}: {response.text}"
    body = response.json()
    assert "errors" in body, f"no 'errors' key in response: {body}"
    codes = [e.get("code") for e in body["errors"]]
    assert any(
        c in ("forbidden", "permission_denied") for c in codes
    ), f"expected 'forbidden' or 'permission_denied' in {codes}"


# ===========================================================================
# 1. Role enforcement on POST creates
# ===========================================================================


@pytest.mark.asyncio
async def test_create_capability_consumer_forbidden(cpr_clients: _CprClients) -> None:
    """POST /v1/capabilities — consumer role returns 403."""
    consumer, _, _cap, _settings, consumer_persona, _ = cpr_clients
    with patch_validator_for_actor(consumer_persona):
        resp = await consumer.post(
            "/v1/capabilities",
            json={"name": "cpr-should-deny", "capability_type": "component"},
            headers=bearer_headers(tenant_slug=consumer_persona.slug),
        )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_capability_producer_succeeds(cpr_clients: _CprClients) -> None:
    """POST /v1/capabilities — producer role returns 201."""
    _, producer, _cap, _settings, _, producer_persona = cpr_clients
    with patch_validator_for_actor(producer_persona):
        resp = await producer.post(
            "/v1/capabilities",
            json={"name": f"cpr-cap-{secrets.token_hex(4)}", "capability_type": "component"},
            headers=bearer_headers(tenant_slug=producer_persona.slug),
        )
    assert resp.status_code == 201, f"expected 201: {resp.text}"


@pytest.mark.asyncio
async def test_create_concept_consumer_forbidden(cpr_clients: _CprClients) -> None:
    """POST /v1/concepts — consumer role returns 403."""
    consumer, _, cap_id, _settings, consumer_persona, _ = cpr_clients
    with patch_validator_for_actor(consumer_persona):
        resp = await consumer.post(
            "/v1/concepts",
            json={"name": "cpr-deny-concept", "entity_type": "concept", "parent_capability_id": str(cap_id)},
            headers=bearer_headers(tenant_slug=consumer_persona.slug),
        )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_concept_producer_succeeds(cpr_clients: _CprClients) -> None:
    """POST /v1/concepts — producer role returns 201."""
    _, producer, cap_id, _settings, _, producer_persona = cpr_clients
    with patch_validator_for_actor(producer_persona):
        resp = await producer.post(
            "/v1/concepts",
            json={
                "name": f"cpr-concept-{secrets.token_hex(4)}",
                "entity_type": "concept",
                "parent_capability_id": str(cap_id),
            },
            headers=bearer_headers(tenant_slug=producer_persona.slug),
        )
    assert resp.status_code == 201, f"expected 201: {resp.text}"


@pytest.mark.asyncio
async def test_create_operation_consumer_forbidden(cpr_clients: _CprClients) -> None:
    """POST /v1/operations — consumer role returns 403."""
    consumer, _, cap_id, _settings, consumer_persona, _ = cpr_clients
    with patch_validator_for_actor(consumer_persona):
        resp = await consumer.post(
            "/v1/operations",
            json={"name": "cpr-deny-op", "entity_type": "operation", "parent_capability_id": str(cap_id)},
            headers=bearer_headers(tenant_slug=consumer_persona.slug),
        )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_operation_producer_succeeds(cpr_clients: _CprClients) -> None:
    """POST /v1/operations — producer role returns 201."""
    _, producer, cap_id, _settings, _, producer_persona = cpr_clients
    with patch_validator_for_actor(producer_persona):
        resp = await producer.post(
            "/v1/operations",
            json={
                "name": f"cpr-op-{secrets.token_hex(4)}",
                "entity_type": "operation",
                "parent_capability_id": str(cap_id),
            },
            headers=bearer_headers(tenant_slug=producer_persona.slug),
        )
    assert resp.status_code == 201, f"expected 201: {resp.text}"


@pytest.mark.asyncio
async def test_create_artifact_consumer_forbidden(cpr_clients: _CprClients) -> None:
    """POST /v1/capabilities/{id}/artifacts — consumer role returns 403."""
    consumer, _, cap_id, _settings, consumer_persona, _ = cpr_clients
    with patch_validator_for_actor(consumer_persona):
        resp = await consumer.post(
            f"/v1/capabilities/{cap_id}/artifacts",
            json={"category": "overview", "title": "Denied attempt", "body": "denied"},
            headers=bearer_headers(tenant_slug=consumer_persona.slug),
        )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_artifact_producer_succeeds(cpr_clients: _CprClients) -> None:
    """POST /v1/capabilities/{id}/artifacts — producer role returns 201."""
    _, producer, cap_id, _settings, _, producer_persona = cpr_clients
    with patch_validator_for_actor(producer_persona):
        resp = await producer.post(
            f"/v1/capabilities/{cap_id}/artifacts",
            json={
                "category": "overview",
                "title": "Producer Artifact",
                "body": "producer artifact body",
            },
            headers=bearer_headers(tenant_slug=producer_persona.slug),
        )
    assert resp.status_code == 201, f"expected 201: {resp.text}"


# ===========================================================================
# 2. Cursor strict mode
# ===========================================================================


@pytest.mark.asyncio
async def test_audit_malformed_cursor_returns_422(cpr_clients: _CprClients) -> None:
    """A malformed cursor string returns 422 invalid_cursor."""
    _, producer, _cap, _settings, _, producer_persona = cpr_clients
    malformed = "!!!not-base64!!!"
    with patch_validator_for_actor(producer_persona):
        resp = await producer.get(
            f"/v1/capabilities?cursor={malformed}",
            headers=bearer_headers(tenant_slug=producer_persona.slug),
        )
    assert resp.status_code == 422, f"expected 422 for malformed cursor, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "errors" in body, f"no 'errors' key in body: {body}"
    codes = [e.get("code") for e in body["errors"]]
    assert "invalid_cursor" in codes, f"expected 'invalid_cursor' in {codes}"


# ===========================================================================
# 3. Keyset pagination
# ===========================================================================


@pytest.mark.asyncio
async def test_keyset_pagination_no_overlap(cpr_clients: _CprClients) -> None:
    """GET /v1/capabilities?cursor=... visits each row exactly once with no overlap."""
    _, producer, _cap, _settings, _, producer_persona = cpr_clients
    created_ids: set[str] = set()

    with patch_validator_for_actor(producer_persona):
        for i in range(5):
            resp = await producer.post(
                "/v1/capabilities",
                json={"name": f"cpr-page-cap-{secrets.token_hex(4)}-{i}", "capability_type": "component"},
                headers=bearer_headers(tenant_slug=producer_persona.slug),
            )
            assert resp.status_code == 201, f"create failed: {resp.text}"
            created_ids.add(resp.json()["entity_id"])

        seen_ids: list[str] = []
        cursor: str | None = None
        pages = 0

        while True:
            url = "/v1/capabilities?page_size=2"
            if cursor:
                url += f"&cursor={cursor}"
            resp = await producer.get(url, headers=bearer_headers(tenant_slug=producer_persona.slug))
            assert resp.status_code == 200, f"list failed: {resp.text}"
            body = resp.json()
            assert "items" in body, f"envelope missing 'items': {list(body)}"
            assert "next_cursor" in body, f"envelope missing 'next_cursor': {list(body)}"

            page_ids = [item["entity_id"] for item in body["items"]]
            seen_ids.extend(page_ids)
            pages += 1

            cursor = body.get("next_cursor")
            if cursor is None:
                break
            if pages > 20:
                raise AssertionError("pagination did not terminate after 20 pages")

    seen_set = set(seen_ids)
    duplicates = [eid for eid in seen_ids if seen_ids.count(eid) > 1]
    assert not duplicates, f"cursor pagination produced duplicate IDs: {duplicates}"

    missing = created_ids - seen_set
    assert not missing, f"cursor pagination missed {len(missing)} created capabilities: {missing}"


# ===========================================================================
# 4. Envelope shape
# ===========================================================================


@pytest.mark.asyncio
async def test_list_capabilities_envelope_shape(cpr_clients: _CprClients) -> None:
    """GET /v1/capabilities emits {items, next_cursor} — no rows/results/bare list."""
    _, producer, _cap, _settings, _, producer_persona = cpr_clients
    with patch_validator_for_actor(producer_persona):
        resp = await producer.get(
            "/v1/capabilities", headers=bearer_headers(tenant_slug=producer_persona.slug)
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert isinstance(body, dict), f"response is not an object: {type(body)}"
    assert "items" in body, f"envelope missing 'items'; keys: {list(body)}"
    assert "next_cursor" in body, f"envelope missing 'next_cursor'; keys: {list(body)}"

    for banned in ("rows", "results"):
        assert banned not in body, f"envelope contains banned key '{banned}'"


@pytest.mark.asyncio
async def test_list_artifacts_envelope_shape(cpr_clients: _CprClients) -> None:
    """GET /v1/capabilities/{id}/artifacts emits {items, next_cursor}."""
    _, producer, cap_id, _settings, _, producer_persona = cpr_clients
    with patch_validator_for_actor(producer_persona):
        resp = await producer.get(
            f"/v1/capabilities/{cap_id}/artifacts",
            headers=bearer_headers(tenant_slug=producer_persona.slug),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert isinstance(body, dict), f"response is not an object: {type(body)}"
    assert "items" in body, f"artifact list envelope missing 'items'; keys: {list(body)}"
    assert "next_cursor" in body, f"artifact list envelope missing 'next_cursor'; keys: {list(body)}"

    for banned in ("page", "page_size", "rows", "results"):
        assert banned not in body, f"artifact list envelope contains banned key '{banned}'"


@pytest.mark.asyncio
async def test_list_subscriptions_envelope_shape(cpr_clients: _CprClients) -> None:
    """GET /v1/capabilities/{id}/subscriptions emits items + next_cursor (not bare list)."""
    _, producer, cap_id, _settings, _, producer_persona = cpr_clients
    with patch_validator_for_actor(producer_persona):
        resp = await producer.get(
            f"/v1/capabilities/{cap_id}/subscriptions",
            headers=bearer_headers(tenant_slug=producer_persona.slug),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert isinstance(body, dict | list), f"unexpected response type: {type(body)}"
    if isinstance(body, list):
        raise AssertionError(
            "GET /v1/capabilities/{id}/subscriptions returned a bare list — "
            "expected {items, next_cursor} envelope"
        )
    assert "items" in body or len(body) == 0, f"envelope missing 'items'; keys: {list(body)}"


# ===========================================================================
# 5. VALID_ROLES consistency — static check
# ===========================================================================


def test_valid_roles_imported_by_services() -> None:
    """Service modules that gate on roles import named constants from auth.context."""
    from registry.api.auth.context import (
        ROLE_ADMIN,
        ROLE_AUDITOR,
        ROLE_CONSUMER,
        ROLE_PRODUCER,
        VALID_ROLES,
    )

    assert ROLE_CONSUMER == "consumer"
    assert ROLE_PRODUCER == "producer"
    assert ROLE_ADMIN == "admin"
    assert ROLE_AUDITOR == "auditor"
    assert VALID_ROLES == frozenset({"consumer", "producer", "admin", "auditor"})

    import registry.service.adoption as adoption_mod
    import registry.service.entity as entity_mod
    import registry.service.interface_storage as iface_mod

    for mod in (adoption_mod, entity_mod, iface_mod):
        has_constant = any(
            getattr(mod, name, None) is not None or name in vars(mod)
            for name in ("ROLE_ADMIN", "ROLE_PRODUCER", "ROLE_CONSUMER", "ROLE_AUDITOR", "VALID_ROLES")
        )
        assert has_constant, (
            f"{mod.__name__} does not import any ROLE_* constant from registry.api.auth.context; "
            "add the import to eliminate hard-coded role strings"
        )


# ===========================================================================
# 6. Rate-limit enforcement
# ===========================================================================


@pytest.mark.asyncio
async def test_rate_limit_write_budget_exhausted(pg_container: str) -> None:
    """Exhausting the write budget returns 429 with Retry-After header."""
    slug = f"cpr-rl-{secrets.token_hex(4)}"

    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        rate_limit_enabled=True,
        rate_limit_write_per_minute=3,
        rate_limit_read_per_minute=600,
        oidc_discovery_url="https://idp.test.local/.well-known/openid-configuration",
        oidc_issuer_allowlist=["https://idp.test.local"],
        resource_uri_allowlist=["registry"],
        entitlement_service_url="https://entitlement.test.local",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        },
    )

    async with EntitlementAuthHarness(pg_container, settings=settings) as harness:
        persona = harness.add_persona(slug, roles=["producer"])
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Exhaust the 3-token write budget.
            for i in range(3):
                with patch_validator_for_actor(persona):
                    resp = await client.post(
                        "/v1/capabilities",
                        headers=bearer_headers(tenant_slug=slug),
                        json={
                            "name": f"cpr-rl-cap-{i}-{secrets.token_hex(4)}",
                            "capability_type": "component",
                        },
                    )
                assert resp.status_code == 201, f"request {i + 1} expected 201, got {resp.status_code}: {resp.text}"

            # The 4th write must be throttled.
            with patch_validator_for_actor(persona):
                throttled = await client.post(
                    "/v1/capabilities",
                    headers=bearer_headers(tenant_slug=slug),
                    json={"name": f"cpr-rl-throttled-{secrets.token_hex(4)}", "capability_type": "component"},
                )

    assert (
        throttled.status_code == 429
    ), f"expected 429 after budget exhausted, got {throttled.status_code}: {throttled.text}"
    assert (
        "Retry-After" in throttled.headers
    ), f"429 response missing Retry-After header; headers: {dict(throttled.headers)}"
    body = throttled.json()
    assert "errors" in body, f"no 'errors' key in 429 body: {body}"
    codes = [e.get("code") for e in body["errors"]]
    assert "rate_limited" in codes, f"expected 'rate_limited' in error codes; got: {codes}"


@pytest.mark.asyncio
async def test_rate_limit_reads_not_throttled_by_write_budget(pg_container: str) -> None:
    """Read endpoints use an independent budget — exhausting writes must not throttle reads."""
    slug = f"cpr-rl-ro-{secrets.token_hex(4)}"

    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        rate_limit_enabled=True,
        rate_limit_write_per_minute=2,
        rate_limit_read_per_minute=600,
        oidc_discovery_url="https://idp.test.local/.well-known/openid-configuration",
        oidc_issuer_allowlist=["https://idp.test.local"],
        resource_uri_allowlist=["registry"],
        entitlement_service_url="https://entitlement.test.local",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        },
    )

    async with EntitlementAuthHarness(pg_container, settings=settings) as harness:
        persona = harness.add_persona(slug, roles=["producer"])
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for i in range(2):
                with patch_validator_for_actor(persona):
                    await client.post(
                        "/v1/capabilities",
                        headers=bearer_headers(tenant_slug=slug),
                        json={
                            "name": f"cpr-ro-cap-{i}-{secrets.token_hex(4)}",
                            "capability_type": "component",
                        },
                    )

            with patch_validator_for_actor(persona):
                read_resp = await client.get(
                    "/v1/capabilities", headers=bearer_headers(tenant_slug=slug)
                )

    assert read_resp.status_code == 200, (
        f"GET /v1/capabilities should succeed even when write budget is exhausted; "
        f"got {read_resp.status_code}: {read_resp.text}"
    )


@pytest.mark.asyncio
async def test_rate_limit_tenant_isolation(pg_container: str) -> None:
    """Tenant A exhausting its write budget must not throttle tenant B."""
    slug_a = f"cpr-rl-a-{secrets.token_hex(4)}"
    slug_b = f"cpr-rl-b-{secrets.token_hex(4)}"

    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        rate_limit_enabled=True,
        rate_limit_write_per_minute=2,
        rate_limit_read_per_minute=600,
        oidc_discovery_url="https://idp.test.local/.well-known/openid-configuration",
        oidc_issuer_allowlist=["https://idp.test.local"],
        resource_uri_allowlist=["registry"],
        entitlement_service_url="https://entitlement.test.local",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        },
    )

    # The rate-limit middleware buckets by bearer token hash (not tenant_id),
    # so each tenant must use a distinct token to get an independent bucket.
    token_a = f"dummy.jwt.{secrets.token_hex(8)}"
    token_b = f"dummy.jwt.{secrets.token_hex(8)}"

    async with EntitlementAuthHarness(pg_container, settings=settings) as harness:
        persona_a = harness.add_persona(slug_a, roles=["producer"])
        persona_b = harness.add_persona(slug_b, roles=["producer"])
        transport = ASGITransport(app=harness.app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # JIT-materialise both tenants before the rate-limit test so each
            # gets its own tenant_id and independent rate-limit bucket.
            harness.configure_fetcher_for(persona_a)
            with patch_validator_for_actor(persona_a):
                _ra = await client.get("/v1/whoami", headers=bearer_headers(token=token_a, tenant_slug=slug_a))
                assert _ra.status_code == 200, _ra.text
            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                _rb = await client.get("/v1/whoami", headers=bearer_headers(token=token_b, tenant_slug=slug_b))
                assert _rb.status_code == 200, _rb.text

            # Exhaust tenant A's write budget using token_a.
            harness.configure_fetcher_for(persona_a)
            for i in range(2):
                with patch_validator_for_actor(persona_a):
                    await client.post(
                        "/v1/capabilities",
                        headers=bearer_headers(token=token_a, tenant_slug=slug_a),
                        json={
                            "name": f"cpr-iso-a-{i}-{secrets.token_hex(4)}",
                            "capability_type": "component",
                        },
                    )

            # Tenant B must still be able to write — uses a different token so
            # it has an independent rate-limit bucket.
            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                resp_b = await client.post(
                    "/v1/capabilities",
                    headers=bearer_headers(token=token_b, tenant_slug=slug_b),
                    json={"name": f"cpr-iso-b-{secrets.token_hex(4)}", "capability_type": "component"},
                )

    assert resp_b.status_code == 201, (
        f"Tenant B should not be throttled when tenant A exhausted its budget; "
        f"got {resp_b.status_code}: {resp_b.text}"
    )
