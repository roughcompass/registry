"""Unit tests for the entitlement service HTTP client.

One test per row of the documented failure-mode table — covers the full
status-class dispatch surface using ``httpx.MockTransport``. Retry
policy and deadline-budget enforcement also have dedicated tests since
they are load-bearing for keeping the auth hot path bounded.

The tests use ``httpx.MockTransport`` rather than respx because respx
does not reliably intercept ``AsyncClient`` instances created in test
fixtures in this environment. MockTransport is the httpx-native
testing primitive and behaves identically across versions.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from registry.auth.entitlements.client import (
    EntitlementAuthError,
    EntitlementMalformedError,
    EntitlementNotFoundError,
    EntitlementRateLimitError,
    EntitlementServiceError,
    fetch_entitlements,
)
from registry.config import Settings

_TEST_BASE_URL = "https://entitlement.test.local"


def _settings(*, max_retries: int = 1) -> Settings:
    """Construct a Settings configured against the canonical test base URL."""
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/r",
        pgbouncer_url="postgresql+asyncpg://u:p@localhost/r",
        scheduler_jobstore_url="postgresql+asyncpg://u:p@localhost/r",
        entitlement_service_url=_TEST_BASE_URL,
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={"ADMIN": "admin"},
        entitlement_max_retries=max_retries,
        entitlement_connect_timeout_ms=250,
        entitlement_read_timeout_ms=1500,
    )


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response] | list[httpx.Response | Exception],
) -> httpx.AsyncClient:
    """Build an AsyncClient backed by a MockTransport.

    ``handler`` may be a callable (one response per request) or a list of
    responses/exceptions consumed in sequence (for retry tests). A list is
    handy for "5xx then 200" scenarios where the response varies per call.
    """
    if isinstance(handler, list):
        seq = iter(handler)

        def _from_seq(_request: httpx.Request) -> httpx.Response:
            try:
                item = next(seq)
            except StopIteration as exc:  # pragma: no cover — test bug
                raise AssertionError("MockTransport sequence exhausted") from exc
            if isinstance(item, BaseException):
                raise item
            return item

        transport = httpx.MockTransport(_from_seq)
    else:
        transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _capture(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """Returns a handler that records each request and replies 200/empty."""

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"entitlements": []})

    return _handler


@pytest.mark.asyncio
class TestSuccessfulPaths:
    async def test_200_with_entitlements_returns_list(self):
        async with _make_client(
            lambda _r: httpx.Response(
                200, json={"entitlements": ["111_REGISTRY_ADMIN", "222_REGISTRY_CONSUMER"]}
            )
        ) as client:
            result = await fetch_entitlements(
                client,
                resolved_identity="user-1",
                raw_jwt="dummy.jwt.value",
                settings=_settings(),
            )
        assert result == ["111_REGISTRY_ADMIN", "222_REGISTRY_CONSUMER"]

    async def test_200_with_empty_entitlements_returns_empty_list(self):
        async with _make_client(
            lambda _r: httpx.Response(200, json={"entitlements": []})
        ) as client:
            result = await fetch_entitlements(
                client,
                resolved_identity="user-1",
                raw_jwt="dummy.jwt.value",
                settings=_settings(),
            )
        assert result == []


@pytest.mark.asyncio
class TestAuthoritativeRejection:
    """401/403/404 from upstream propagate without cache consultation —
    these are authoritative answers the client cannot override."""

    async def test_401_raises_auth_error_with_status(self):
        async with _make_client(
            lambda _r: httpx.Response(401, json={"error": "invalid_token"})
        ) as client:
            with pytest.raises(EntitlementAuthError) as exc_info:
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="rejected.jwt",
                    settings=_settings(),
                )
        assert exc_info.value.status_code == 401

    async def test_403_raises_auth_error_with_status(self):
        async with _make_client(
            lambda _r: httpx.Response(403, json={"error": "forbidden"})
        ) as client:
            with pytest.raises(EntitlementAuthError) as exc_info:
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(),
                )
        assert exc_info.value.status_code == 403

    async def test_404_raises_not_found(self):
        async with _make_client(lambda _r: httpx.Response(404)) as client:
            with pytest.raises(EntitlementNotFoundError):
                await fetch_entitlements(
                    client,
                    resolved_identity="unknown-user",
                    raw_jwt="dummy.jwt",
                    settings=_settings(),
                )


@pytest.mark.asyncio
class TestRateLimit:
    async def test_429_after_retries_raises_rate_limit(self):
        # max_retries=1 means 2 total attempts. Both 429 → final raise.
        call_count = 0

        def _handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(429)

        async with _make_client(_handler) as client:
            with pytest.raises(EntitlementRateLimitError):
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(max_retries=1),
                )
        assert call_count == 2

    async def test_429_then_200_succeeds_via_retry(self):
        async with _make_client(
            [
                httpx.Response(429),
                httpx.Response(200, json={"entitlements": ["111_REGISTRY_ADMIN"]}),
            ]
        ) as client:
            result = await fetch_entitlements(
                client,
                resolved_identity="user-1",
                raw_jwt="dummy.jwt",
                settings=_settings(max_retries=1),
            )
        assert result == ["111_REGISTRY_ADMIN"]


@pytest.mark.asyncio
class TestServiceUnavailable:
    """5xx + timeout + network error all raise EntitlementServiceError
    with is_cacheable=True — the resolver is permitted to use stale cache."""

    async def test_500_after_retries_raises_service_error(self):
        call_count = 0

        def _handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500)

        async with _make_client(_handler) as client:
            with pytest.raises(EntitlementServiceError) as exc_info:
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(max_retries=1),
                )
        assert exc_info.value.is_cacheable is True
        assert call_count == 2

    async def test_503_then_200_succeeds_via_retry(self):
        async with _make_client(
            [
                httpx.Response(503),
                httpx.Response(200, json={"entitlements": []}),
            ]
        ) as client:
            result = await fetch_entitlements(
                client,
                resolved_identity="user-1",
                raw_jwt="dummy.jwt",
                settings=_settings(max_retries=1),
            )
        assert result == []

    async def test_network_error_raises_service_error(self):
        async with _make_client(
            [httpx.ConnectError("connection refused")]
        ) as client:
            with pytest.raises(EntitlementServiceError):
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(max_retries=0),
                )

    async def test_timeout_raises_service_error(self):
        async with _make_client([httpx.ReadTimeout("timed out")]) as client:
            with pytest.raises(EntitlementServiceError):
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(max_retries=0),
                )


@pytest.mark.asyncio
class TestMalformed:
    """200 with a body that doesn't match `{"entitlements": [...]}`
    raises EntitlementMalformedError — caller maps to 503; cache MUST NOT
    be consulted because the upstream is behaving outside contract."""

    async def test_non_json_body_raises_malformed(self):
        async with _make_client(
            lambda _r: httpx.Response(
                200, content=b"not-json", headers={"content-type": "application/json"}
            )
        ) as client:
            with pytest.raises(EntitlementMalformedError):
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(),
                )

    async def test_missing_entitlements_key_raises_malformed(self):
        async with _make_client(
            lambda _r: httpx.Response(200, json={"other": "field"})
        ) as client:
            with pytest.raises(EntitlementMalformedError):
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(),
                )

    async def test_entitlements_not_a_list_raises_malformed(self):
        async with _make_client(
            lambda _r: httpx.Response(200, json={"entitlements": "should-be-list"})
        ) as client:
            with pytest.raises(EntitlementMalformedError):
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(),
                )

    async def test_entitlements_with_non_string_item_raises_malformed(self):
        async with _make_client(
            lambda _r: httpx.Response(
                200, json={"entitlements": ["111_REGISTRY_ADMIN", 42]}
            )
        ) as client:
            with pytest.raises(EntitlementMalformedError):
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(),
                )


@pytest.mark.asyncio
class TestRequestShape:
    async def test_url_includes_user_id_and_env(self):
        seen: list[httpx.Request] = []
        async with _make_client(_capture(seen)) as client:
            await fetch_entitlements(
                client,
                resolved_identity="abc-123",
                raw_jwt="dummy.jwt",
                settings=_settings(),
            )
        assert len(seen) == 1
        url = str(seen[0].url)
        assert "userId=abc-123" in url
        assert "env=DEV" in url

    async def test_jwt_forwarded_as_bearer(self):
        seen: list[httpx.Request] = []
        async with _make_client(_capture(seen)) as client:
            await fetch_entitlements(
                client,
                resolved_identity="abc-123",
                raw_jwt="my.user.jwt",
                settings=_settings(),
            )
        assert seen[0].headers["Authorization"] == "Bearer my.user.jwt"

    async def test_request_id_forwarded_when_supplied(self):
        seen: list[httpx.Request] = []
        async with _make_client(_capture(seen)) as client:
            await fetch_entitlements(
                client,
                resolved_identity="abc-123",
                raw_jwt="my.user.jwt",
                settings=_settings(),
                request_id="req-xyz",
            )
        assert seen[0].headers["X-Request-ID"] == "req-xyz"

    async def test_request_id_omitted_when_not_supplied(self):
        seen: list[httpx.Request] = []
        async with _make_client(_capture(seen)) as client:
            await fetch_entitlements(
                client,
                resolved_identity="abc-123",
                raw_jwt="my.user.jwt",
                settings=_settings(),
            )
        assert "X-Request-ID" not in seen[0].headers


@pytest.mark.asyncio
class TestDeadline:
    async def test_deadline_already_passed_raises_service_error(self):
        # Pre-expired deadline — the first attempt aborts before issuing.
        async with _make_client(
            lambda _r: httpx.Response(200, json={"entitlements": []})
        ) as client:
            past_deadline = time.monotonic() - 1.0
            with pytest.raises(EntitlementServiceError) as exc_info:
                await fetch_entitlements(
                    client,
                    resolved_identity="user-1",
                    raw_jwt="dummy.jwt",
                    settings=_settings(),
                    deadline=past_deadline,
                )
        assert "deadline" in exc_info.value.reason.lower()
