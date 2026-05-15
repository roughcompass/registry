"""Compose-stack smoke test for the entitlement auth path.

This test exercises the full production-equivalent call chain by
hitting the live mock-oauth2-server and mock-entitlement-service that
``docker compose up`` brings up. The unit + non-compose integration
tests use ``make_jwt`` to bypass OIDC discovery + JWKS fetch; this
test is the only one that exercises the real discovery → JWKS → token
→ registry → entitlement-service round trip.

Skipped by default — runs only when ``COMPOSE_STACK_UP=1`` is set in
the environment, so CI without the compose stack does not fail. To
run locally::

    docker compose up -d
    python scripts/bootstrap_dev_tenant.py
    COMPOSE_STACK_UP=1 pytest tests/integration/test_auth_compose_smoke.py -m compose -q
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.compose


_MOCK_OIDC_URL = os.environ.get("MOCK_OIDC_URL", "http://localhost:8090")
_MOCK_ENTITLEMENT_URL = os.environ.get(
    "MOCK_ENTITLEMENT_URL", "http://localhost:8091"
)
_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:8000")
_DEFAULT_USER_ID = os.environ.get("DEV_USER_ID", "dev-admin")
_DEFAULT_TENANT_SLUG = os.environ.get("DEV_TENANT_SLUG", "111205")


def _compose_stack_up() -> bool:
    """Detect whether the compose stack is running and reachable.

    Skips the test cleanly when COMPOSE_STACK_UP is unset OR when the
    mock OIDC discovery doc is unreachable. Either condition means the
    operator hasn't set up the stack and the smoke test would fail for
    environmental reasons rather than a real defect.
    """
    if not os.environ.get("COMPOSE_STACK_UP"):
        return False
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{_MOCK_OIDC_URL}/default/.well-known/openid-configuration")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(
    not _compose_stack_up(),
    reason="COMPOSE_STACK_UP not set or mock-oauth2-server not reachable",
)
def test_real_jwt_flows_through_to_whoami() -> None:
    """End-to-end: fetch JWT from mock IDP → seed entitlements →
    call /v1/whoami → 200 with the expected tenant slug.

    Single sanity-check scenario — failure scenarios are covered by
    unit + non-compose integration tests. This test exists to confirm
    the wire-level pieces (discovery doc, JWKS fetch, JWT signature
    verification, entitlement service call) all interconnect.
    """
    # Step 1: register canned entitlements for our test user in the
    # mock entitlement service.
    with httpx.Client(timeout=10.0) as client:
        seed_resp = client.put(
            f"{_MOCK_ENTITLEMENT_URL}/admin/entitlements/{_DEFAULT_USER_ID}",
            json={
                "scenario": "success_one_tenant",
                "entitlements": [f"{_DEFAULT_TENANT_SLUG}_REGISTRY_ADMIN"],
            },
        )
        assert seed_resp.status_code in (200, 204), (
            f"entitlement seed failed: {seed_resp.status_code} {seed_resp.text}"
        )

        # Step 2: obtain a JWT from mock-oauth2-server using
        # client_credentials. The default mock-oauth2-server config
        # accepts any client_id / client_secret pair and signs a JWT
        # against the issuer at {url}/default.
        token_resp = client.post(
            f"{_MOCK_OIDC_URL}/default/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "registry-dev",
                "client_secret": "dev-secret",
                "scope": "openid",
                "audience": "registry",
            },
        )
        assert token_resp.status_code == 200, (
            f"mock IDP token endpoint failed: {token_resp.status_code} {token_resp.text}"
        )
        access_token = token_resp.json()["access_token"]

        # Step 3: call the registry's /v1/whoami with the JWT.
        api_resp = client.get(
            f"{_REGISTRY_URL}/v1/whoami",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert api_resp.status_code == 200, (
        f"registry rejected real JWT: {api_resp.status_code} {api_resp.text}"
    )
    body = api_resp.json()
    # Spot-check that the resolved tenant slug round-trips.
    assert body.get("tenant_slug") == _DEFAULT_TENANT_SLUG
