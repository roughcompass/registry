"""Unit tests for the in-process token-bucket rate limiter.

Covers:
- _TokenBucket: refill, consume, retry_after.
- _BucketStore: per-tenant isolation, separate read/write pools.
- RateLimitMiddleware: 60 writes allowed, 61st returns 429.
- RateLimitMiddleware: tenant A exhausted does not throttle tenant B.
- RateLimitMiddleware: separate read and write budgets.
- RateLimitMiddleware: rate_limit_enabled=False disables enforcement.
- RateLimitMiddleware: public paths bypass rate limiting.
- RateLimitMiddleware: requests without Bearer token bypass rate limiting.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.api.middleware.ratelimit import RateLimitMiddleware, _BucketStore, _TokenBucket

# ---------------------------------------------------------------------------
# _TokenBucket
# ---------------------------------------------------------------------------


def test_token_bucket_starts_full() -> None:
    bucket = _TokenBucket(per_minute=10)
    # First 10 consumes should succeed.
    for _ in range(10):
        assert bucket.consume() is True


def test_token_bucket_exhausts_at_limit() -> None:
    bucket = _TokenBucket(per_minute=5)
    for _ in range(5):
        bucket.consume()
    # 6th should fail.
    assert bucket.consume() is False


def test_token_bucket_zero_budget_always_throttled() -> None:
    bucket = _TokenBucket(per_minute=0)
    assert bucket.consume() is False


def test_token_bucket_retry_after_positive_when_empty() -> None:
    bucket = _TokenBucket(per_minute=60)
    # Drain the bucket.
    while bucket.consume():
        pass
    assert bucket.retry_after_seconds >= 1


def test_token_bucket_retry_after_zero_budget() -> None:
    bucket = _TokenBucket(per_minute=0)
    assert bucket.retry_after_seconds == 60


# ---------------------------------------------------------------------------
# _BucketStore
# ---------------------------------------------------------------------------


def test_bucket_store_per_tenant_isolation() -> None:
    """Tenant A exhausting writes does not throttle tenant B."""
    store = _BucketStore(read_per_minute=600, write_per_minute=2)
    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()

    # Exhaust tenant A's write bucket.
    store.consume_write(tid_a)
    store.consume_write(tid_a)
    allowed_a, _ = store.consume_write(tid_a)
    assert allowed_a is False

    # Tenant B is untouched.
    allowed_b, _ = store.consume_write(tid_b)
    assert allowed_b is True


def test_bucket_store_separate_read_write_budgets() -> None:
    """Write exhaustion does not affect the read bucket for the same tenant."""
    store = _BucketStore(read_per_minute=100, write_per_minute=2)
    tid = uuid.uuid4()

    # Exhaust write budget.
    store.consume_write(tid)
    store.consume_write(tid)
    write_allowed, _ = store.consume_write(tid)
    assert write_allowed is False

    # Read budget is unaffected.
    read_allowed, _ = store.consume_read(tid)
    assert read_allowed is True


def test_bucket_store_write_limit_at_60() -> None:
    """Exactly 60 writes succeed; the 61st is rejected (default write_per_minute=60)."""
    store = _BucketStore(read_per_minute=600, write_per_minute=60)
    tid = uuid.uuid4()

    successes = 0
    for _ in range(60):
        allowed, _ = store.consume_write(tid)
        if allowed:
            successes += 1

    assert successes == 60

    # 61st must be rejected.
    allowed_61, retry_after = store.consume_write(tid)
    assert allowed_61 is False
    assert retry_after >= 1


# ---------------------------------------------------------------------------
# Helpers for middleware ASGI testing
# ---------------------------------------------------------------------------


def _make_settings(
    *,
    enabled: bool = True,
    write_per_minute: int = 60,
    read_per_minute: int = 600,
) -> Any:
    s = MagicMock()
    s.rate_limit_enabled = enabled
    s.rate_limit_write_per_minute = write_per_minute
    s.rate_limit_read_per_minute = read_per_minute
    return s


def _make_scope(
    method: str = "POST",
    path: str = "/v1/capabilities",
    token: str = "test-token",
) -> dict[str, Any]:
    auth_value = f"Bearer {token}".encode()
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"authorization", auth_value)],
    }


async def _collect_response(middleware: RateLimitMiddleware, scope: dict) -> dict[str, Any]:
    """Run the middleware and return {status, headers, body}."""
    received: list[dict] = []

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(event: dict) -> None:
        received.append(event)

    inner_called = False

    async def _inner_app(s: Any, r: Any, snd: Any) -> None:
        nonlocal inner_called
        inner_called = True
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware._app = _inner_app
    await middleware(scope, _receive, _send)

    status_event = next((e for e in received if e["type"] == "http.response.start"), None)
    body_event = next((e for e in received if e["type"] == "http.response.body"), None)
    body_bytes = body_event["body"] if body_event else b""

    headers_dict = {}
    if status_event:
        for k, v in status_event.get("headers", []):
            headers_dict[k.decode()] = v.decode()

    return {
        "status": status_event["status"] if status_event else None,
        "headers": headers_dict,
        "body": body_bytes,
        "inner_called": inner_called,
    }


def _middleware_with_tenant(tenant_id: uuid.UUID, settings: Any) -> RateLimitMiddleware:
    """Build a RateLimitMiddleware whose tenant cache is pre-populated."""
    mw = RateLimitMiddleware(
        app=MagicMock(),
        settings=settings,
        session_factory=AsyncMock(),
    )
    token_hash = _token_hash("test-token")
    mw._tenant_cache[token_hash] = tenant_id
    return mw


def _token_hash(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# RateLimitMiddleware integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_60_writes_succeed_61st_rejected() -> None:
    """60 POST requests from one tenant succeed; the 61st returns 429."""
    settings = _make_settings(write_per_minute=60)
    tid = uuid.uuid4()
    mw = _middleware_with_tenant(tid, settings)

    scope = _make_scope("POST", "/v1/capabilities", "test-token")

    successes = 0
    for _ in range(60):
        resp = await _collect_response(mw, scope)
        if resp["status"] == 200:
            successes += 1

    assert successes == 60

    # 61st must be 429.
    resp_61 = await _collect_response(mw, scope)
    assert resp_61["status"] == 429
    body = json.loads(resp_61["body"])
    assert body["errors"][0]["code"] == "rate_limited"
    assert "retry-after" in resp_61["headers"]
    assert int(resp_61["headers"]["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_middleware_tenant_a_limit_does_not_throttle_tenant_b() -> None:
    """Tenant A exhausted write budget; tenant B's first request succeeds."""
    settings = _make_settings(write_per_minute=1)
    mw = RateLimitMiddleware(
        app=MagicMock(),
        settings=settings,
        session_factory=AsyncMock(),
    )

    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()

    hash_a = _token_hash("token-a")
    hash_b = _token_hash("token-b")
    mw._tenant_cache[hash_a] = tid_a
    mw._tenant_cache[hash_b] = tid_b

    scope_a = _make_scope("POST", "/v1/capabilities", "token-a")
    scope_b = _make_scope("POST", "/v1/capabilities", "token-b")

    # First request for tenant A succeeds.
    resp_a1 = await _collect_response(mw, scope_a)
    assert resp_a1["status"] == 200

    # Second request for tenant A is throttled (budget=1).
    resp_a2 = await _collect_response(mw, scope_a)
    assert resp_a2["status"] == 429

    # Tenant B is entirely unaffected.
    resp_b = await _collect_response(mw, scope_b)
    assert resp_b["status"] == 200


@pytest.mark.asyncio
async def test_middleware_separate_read_write_budgets() -> None:
    """60 writes + reads from same tenant both succeed with default config."""
    settings = _make_settings(write_per_minute=60, read_per_minute=600)
    tid = uuid.uuid4()
    mw = _middleware_with_tenant(tid, settings)

    scope_write = _make_scope("POST", "/v1/capabilities", "test-token")
    scope_read = _make_scope("GET", "/v1/capabilities", "test-token")

    write_successes = 0
    for _ in range(60):
        resp = await _collect_response(mw, scope_write)
        if resp["status"] == 200:
            write_successes += 1

    assert write_successes == 60

    # 100 reads from the same tenant should also succeed (read budget=600).
    read_successes = 0
    for _ in range(100):
        resp = await _collect_response(mw, scope_read)
        if resp["status"] == 200:
            read_successes += 1

    assert read_successes == 100


@pytest.mark.asyncio
async def test_middleware_disabled_passes_all_requests() -> None:
    """rate_limit_enabled=False bypasses all rate limiting."""
    settings = _make_settings(enabled=False, write_per_minute=1)
    tid = uuid.uuid4()
    mw = _middleware_with_tenant(tid, settings)

    scope = _make_scope("POST", "/v1/capabilities", "test-token")

    # 100 requests should all pass (budget=1 but disabled).
    for _ in range(100):
        resp = await _collect_response(mw, scope)
        assert resp["status"] == 200


@pytest.mark.asyncio
async def test_middleware_bypasses_public_paths() -> None:
    """Requests to /healthz, /readyz, /metrics, /webhooks bypass rate limiting."""
    settings = _make_settings(write_per_minute=0)  # budget=0 → always throttled if active
    tid = uuid.uuid4()

    for path in ("/healthz", "/readyz", "/metrics", "/webhooks/github"):
        mw = _middleware_with_tenant(tid, settings)
        scope = _make_scope("GET", path, "test-token")
        resp = await _collect_response(mw, scope)
        assert resp["status"] == 200, f"Expected bypass for {path}"


@pytest.mark.asyncio
async def test_middleware_bypasses_missing_bearer_token() -> None:
    """Requests without a Bearer token are passed through (auth layer handles 401)."""
    settings = _make_settings(write_per_minute=1)
    mw = RateLimitMiddleware(
        app=MagicMock(),
        settings=settings,
        session_factory=AsyncMock(),
    )

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/capabilities",
        "headers": [],  # No Authorization header.
    }

    resp = await _collect_response(mw, scope)
    # Should pass through (200 from inner app, not 429).
    assert resp["status"] == 200


@pytest.mark.asyncio
async def test_middleware_passes_through_unrecognised_token() -> None:
    """Unknown token (not in cache, not in DB) is passed through; auth layer 401s it."""
    settings = _make_settings(write_per_minute=1)

    # Session factory returns no row for the token.
    session_mock = AsyncMock()
    result_mock = MagicMock()
    result_mock.one_or_none.return_value = None
    session_mock.execute = AsyncMock(return_value=result_mock)

    factory_mock = MagicMock()
    factory_mock.return_value.__aenter__ = AsyncMock(return_value=session_mock)
    factory_mock.return_value.__aexit__ = AsyncMock(return_value=False)

    mw = RateLimitMiddleware(
        app=MagicMock(),
        settings=settings,
        session_factory=factory_mock,
    )

    scope = _make_scope("POST", "/v1/capabilities", "unknown-token")
    resp = await _collect_response(mw, scope)
    # Should pass through.
    assert resp["status"] == 200


@pytest.mark.asyncio
async def test_middleware_tenant_cache_populated_from_db() -> None:
    """First request resolves tenant_id from DB; subsequent calls use cache."""
    settings = _make_settings(write_per_minute=10)
    tid = uuid.uuid4()

    session_mock = AsyncMock()
    result_mock = MagicMock()
    result_mock.one_or_none.return_value = (tid,)
    session_mock.execute = AsyncMock(return_value=result_mock)

    factory_mock = MagicMock()
    factory_mock.return_value.__aenter__ = AsyncMock(return_value=session_mock)
    factory_mock.return_value.__aexit__ = AsyncMock(return_value=False)

    mw = RateLimitMiddleware(
        app=MagicMock(),
        settings=settings,
        session_factory=factory_mock,
    )

    scope = _make_scope("POST", "/v1/capabilities", "known-token")

    resp1 = await _collect_response(mw, scope)
    assert resp1["status"] == 200
    # DB was queried once.
    assert session_mock.execute.call_count == 1

    resp2 = await _collect_response(mw, scope)
    assert resp2["status"] == 200
    # DB still queried only once (cache hit).
    assert session_mock.execute.call_count == 1


@pytest.mark.asyncio
async def test_middleware_non_http_scope_passthrough() -> None:
    """WebSocket and lifespan scopes are passed through without rate limiting."""
    settings = _make_settings(write_per_minute=0)
    mw = RateLimitMiddleware(
        app=MagicMock(),
        settings=settings,
        session_factory=AsyncMock(),
    )

    inner_called = False

    async def _inner(s: Any, r: Any, snd: Any) -> None:
        nonlocal inner_called
        inner_called = True

    mw._app = _inner

    scope = {"type": "websocket", "path": "/ws"}
    await mw(scope, AsyncMock(), AsyncMock())
    assert inner_called
