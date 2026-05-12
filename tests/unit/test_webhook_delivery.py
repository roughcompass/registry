"""Unit tests for WebhookDeliveryWorker.

HTTP is faked via ``httpx.MockTransport``; DB is mocked at the
``session.execute`` boundary so no live Postgres is required.

Coverage:
- HMAC signing is deterministic and constant-time-verifiable.
- Backoff schedule: 60s base, doubles each attempt, caps at 24h.
- 2xx → status='success'; no next_retry_at.
- 5xx → status='pending'; next_retry_at advances per backoff schedule.
- 408/425/429 → transient retry; other 4xx → permanent 'failed'.
- Transport-level error (httpx.HTTPError) → pending with retry.
- run_once: claims pending rows, dispatches, and records outcomes.
- make_digest_envelope: 3 events → single CapabilityRegistry.Digest v1 with item_count=3.
- Payload contract: the POSTed body contains only
  CapabilityRegistryEvent fields — no body/description/fact_body.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from registry.types import CapabilityRegistryEvent, FakeClock
from registry.workers.webhook_delivery import (
    MAX_ATTEMPTS,
    SIGNATURE_HEADER,
    SIGNATURE_PREFIX,
    WebhookDeliveryWorker,
    compute_next_retry,
    make_digest_envelope,
    sign_payload,
    verify_signature,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_SECRET = "shhh-this-is-a-test-secret"


def _event(**overrides: Any) -> CapabilityRegistryEvent:
    base: dict[str, Any] = dict(
        notification_id=uuid.uuid4(),
        tenant_id=_TENANT,
        subscription_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        capability_slug="payment-api",
        event_kind="version_published",
        change_classification="non-breaking",
        version_before="1.0.0",
        version_after="1.1.0",
        occurred_at=_NOW,
        fetch_url="https://example.com/cap/abc",
    )
    base.update(overrides)
    return CapabilityRegistryEvent(**base)


def _async_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _mock_factory() -> tuple[MagicMock, list[tuple[str, dict]]]:
    """Minimal session factory; tests that don't exercise run_once won't read it."""
    calls: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None):
        calls.append((str(stmt), params or {}))
        return MagicMock()

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    return factory, calls


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------


def test_sign_payload_format_and_determinism() -> None:
    sig1 = sign_payload(b"hello", _SECRET)
    sig2 = sign_payload(b"hello", _SECRET)
    assert sig1 == sig2
    assert sig1.startswith(SIGNATURE_PREFIX)
    # Hex digest is 64 chars → total length 7 + 64 = 71.
    assert len(sig1) == len(SIGNATURE_PREFIX) + 64


def test_sign_payload_matches_independent_hmac_sha256() -> None:
    sig = sign_payload(b"payload-bytes", _SECRET)
    expected_hex = hmac.new(_SECRET.encode("utf-8"), b"payload-bytes", hashlib.sha256).hexdigest()
    assert sig == SIGNATURE_PREFIX + expected_hex


def test_verify_signature_round_trip() -> None:
    sig = sign_payload(b"x", _SECRET)
    assert verify_signature(b"x", _SECRET, sig)
    assert not verify_signature(b"y", _SECRET, sig)
    assert not verify_signature(b"x", "wrong-secret", sig)


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_compute_next_retry_doubles_per_attempt() -> None:
    one = compute_next_retry(1, _NOW)
    two = compute_next_retry(2, _NOW)
    three = compute_next_retry(3, _NOW)
    assert (one - _NOW).total_seconds() == 60
    assert (two - _NOW).total_seconds() == 120
    assert (three - _NOW).total_seconds() == 240


def test_compute_next_retry_caps_at_24h() -> None:
    far = compute_next_retry(50, _NOW)
    # 24h cap means ≤ 24h, but >= 1m (positive).
    assert (far - _NOW).total_seconds() == 24 * 60 * 60


# ---------------------------------------------------------------------------
# deliver() — single attempt
# ---------------------------------------------------------------------------


def _make_worker(handler) -> WebhookDeliveryWorker:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    factory, _ = _mock_factory()
    return WebhookDeliveryWorker(factory, FakeClock(_NOW), http_client=client)


@pytest.mark.asyncio
async def test_deliver_2xx_returns_success() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["sig"] = request.headers.get(SIGNATURE_HEADER)
        return httpx.Response(200)

    w = _make_worker(handler)
    try:
        result = await w.deliver(_event(), "https://hook.example.com", _SECRET)
    finally:
        await w.close()

    assert result.status == "success"
    assert result.http_status == 200
    assert result.next_retry_at is None
    # Signature header verifies against what was sent.
    assert verify_signature(seen["body"], _SECRET, seen["sig"])
    # Payload minimality: no freeform description/body fields in the payload.
    body = json.loads(seen["body"])
    forbidden = {"body", "description", "fact_body", "content", "message"}
    assert not (set(body.keys()) & forbidden)


@pytest.mark.asyncio
async def test_deliver_5xx_returns_pending_with_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    w = _make_worker(handler)
    try:
        result = await w.deliver(_event(), "https://hook.example.com", _SECRET, attempt_number=2)
    finally:
        await w.close()

    assert result.status == "pending"
    assert result.http_status == 503
    assert result.next_retry_at == compute_next_retry(2, _NOW)
    assert "http 503" in (result.error_text or "")


@pytest.mark.asyncio
async def test_deliver_429_is_transient_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    w = _make_worker(handler)
    try:
        result = await w.deliver(_event(), "https://hook.example.com", _SECRET, attempt_number=1)
    finally:
        await w.close()

    assert result.status == "pending"
    assert result.http_status == 429


@pytest.mark.asyncio
async def test_deliver_400_is_permanent_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    w = _make_worker(handler)
    try:
        result = await w.deliver(_event(), "https://hook.example.com", _SECRET)
    finally:
        await w.close()

    assert result.status == "failed"
    assert result.http_status == 400
    assert result.next_retry_at is None


@pytest.mark.asyncio
async def test_deliver_max_attempts_marks_failed_even_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    w = _make_worker(handler)
    try:
        result = await w.deliver(
            _event(),
            "https://hook.example.com",
            _SECRET,
            attempt_number=MAX_ATTEMPTS,
        )
    finally:
        await w.close()

    # At MAX_ATTEMPTS we stop retrying.
    assert result.status == "failed"
    assert result.http_status == 502


@pytest.mark.asyncio
async def test_deliver_transport_error_returns_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    w = _make_worker(handler)
    try:
        result = await w.deliver(_event(), "https://hook.example.com", _SECRET, attempt_number=1)
    finally:
        await w.close()

    assert result.status == "pending"
    assert result.http_status is None
    assert "transport" in (result.error_text or "")


# ---------------------------------------------------------------------------
# Digest envelope
# ---------------------------------------------------------------------------


def test_make_digest_envelope_aggregates_events() -> None:
    events = [_event(), _event(), _event()]
    env = make_digest_envelope(_TENANT, events, window="15m", now=_NOW)
    assert env["envelope_type"] == "CapabilityRegistry.Digest"
    assert env["version"] == "v1"
    assert env["window"] == "15m"
    assert env["item_count"] == 3
    assert len(env["items"]) == 3
    assert env["tenant_id"] == str(_TENANT)
    # No body / description / freeform fields anywhere in items.
    forbidden = {"body", "description", "fact_body"}
    for item in env["items"]:
        assert not (set(item.keys()) & forbidden)


# ---------------------------------------------------------------------------
# run_once — claim → deliver → record
# ---------------------------------------------------------------------------


def _make_claim_session(
    delivery_ids: list[uuid.UUID],
    notification_ids: list[uuid.UUID],
    sql_calls: list[tuple[str, Any]],
) -> MagicMock:
    """Build a session mock that serves _claim_pending correctly for N deliveries."""
    assert len(delivery_ids) == len(notification_ids)

    async def _execute(stmt: Any, params: Any = None):
        sql_calls.append((str(stmt), params))
        result = MagicMock()
        sql = " ".join(str(stmt).split())
        if "UPDATE notification_deliveries d" in sql and "RETURNING" in sql:
            result.mappings.return_value.all.return_value = [
                {
                    "delivery_id": did,
                    "notification_id": nid,
                    "tenant_id": _TENANT,
                    "webhook_url": "https://hook.example.com",
                    "attempt_number": 1,
                }
                for did, nid in zip(delivery_ids, notification_ids, strict=True)
            ]
            return result
        if "FROM notifications n" in sql and "LEFT JOIN subscriptions s" in sql:
            result.mappings.return_value.all.return_value = [
                {
                    "notification_id": nid,
                    "tenant_id": _TENANT,
                    "subscription_id": uuid.uuid4(),
                    "capability_id": uuid.uuid4(),
                    "capability_slug": "payment-api",
                    "event_kind": "version_published",
                    "change_classification": "non-breaking",
                    "version_before": "1.0.0",
                    "version_after": "1.1.0",
                    "occurred_at": _NOW,
                    "fetch_url": "https://example.com/cap/abc",
                    "hmac_secret": _SECRET,
                }
                for nid in notification_ids
            ]
            return result
        # Bulk outcome UPDATE — return a rowcount matching the list length.
        result.rowcount = len(delivery_ids) if isinstance(params, list) else 1
        return result

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
async def test_run_once_dispatches_and_records_success() -> None:
    delivery_id = uuid.uuid4()
    notification_id = uuid.uuid4()

    sql_calls: list[tuple[str, Any]] = []
    session = _make_claim_session([delivery_id], [notification_id], sql_calls)
    factory = MagicMock(return_value=session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    worker = WebhookDeliveryWorker(factory, FakeClock(_NOW), http_client=http)
    try:
        attempted = await worker.run_once(batch_size=10)
    finally:
        await worker.close()

    assert attempted == 1
    # Bulk outcome UPDATE fired with a list containing status='success'.
    bulk_calls = [
        params
        for sql, params in sql_calls
        if "UPDATE notification_deliveries" in sql and "SET status" in sql and isinstance(params, list)
    ]
    assert bulk_calls, "expected a bulk outcome UPDATE with a list of params"
    assert bulk_calls[0][0]["st"] == "success"
    assert bulk_calls[0][0]["http"] == 202


# ---------------------------------------------------------------------------
# Batch outcome recording — new tests for CPR-T17
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_50_deliveries_uses_one_db_session_for_outcomes() -> None:
    """50 concurrent HTTP deliveries must record outcomes with exactly 1 DB session."""
    n = 50
    delivery_ids = [uuid.uuid4() for _ in range(n)]
    notification_ids = [uuid.uuid4() for _ in range(n)]

    sql_calls: list[tuple[str, Any]] = []
    session = _make_claim_session(delivery_ids, notification_ids, sql_calls)

    # Count how many times the factory is called (each call = one session acquired).
    factory_call_count = 0
    real_session = session

    def counting_factory():
        nonlocal factory_call_count
        factory_call_count += 1
        return real_session

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    worker = WebhookDeliveryWorker(counting_factory, FakeClock(_NOW), http_client=http)
    try:
        attempted = await worker.run_once(batch_size=n)
    finally:
        await worker.close()

    assert attempted == n
    # Session 1 = _claim_pending, session 2 = _record_outcomes_bulk.
    # Regardless of the HTTP fan-out width, bulk recording uses exactly 1 session.
    bulk_outcome_calls = [
        params
        for sql, params in sql_calls
        if "UPDATE notification_deliveries" in sql and "SET status" in sql and isinstance(params, list)
    ]
    assert len(bulk_outcome_calls) == 1, "outcomes must be recorded in a single bulk call"
    assert len(bulk_outcome_calls[0]) == n, f"expected {n} outcome params, got {len(bulk_outcome_calls[0])}"
    # Factory called at most 2 times: once for claim, once for bulk write.
    assert factory_call_count <= 2, f"expected ≤2 factory calls for a 50-delivery batch but got {factory_call_count}"


@pytest.mark.asyncio
async def test_run_once_one_http_error_does_not_lose_other_49_outcomes() -> None:
    """One delivery's HTTP call raising must not prevent the other 49 outcomes."""
    n = 50
    delivery_ids = [uuid.uuid4() for _ in range(n)]
    notification_ids = [uuid.uuid4() for _ in range(n)]
    failing_delivery_id = delivery_ids[0]

    sql_calls: list[tuple[str, Any]] = []
    session = _make_claim_session(delivery_ids, notification_ids, sql_calls)
    factory = MagicMock(return_value=session)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("simulated network failure")
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    worker = WebhookDeliveryWorker(factory, FakeClock(_NOW), http_client=http)
    try:
        attempted = await worker.run_once(batch_size=n)
    finally:
        await worker.close()

    assert attempted == n

    # All 50 outcomes must be in the bulk params list — the HTTP error is
    # converted to DeliveryResult(status='pending'), not dropped.
    bulk_outcome_calls = [
        params
        for sql, params in sql_calls
        if "UPDATE notification_deliveries" in sql and "SET status" in sql and isinstance(params, list)
    ]
    assert bulk_outcome_calls, "bulk outcome UPDATE must have fired"
    params_list = bulk_outcome_calls[0]
    assert len(params_list) == n, f"expected all {n} outcomes recorded, got {len(params_list)}"

    # The failing delivery must appear with status 'pending' (transport error → retry).
    failing_params = [p for p in params_list if p["did"] == failing_delivery_id]
    assert failing_params, "failing delivery must still be in outcome batch"
    assert failing_params[0]["st"] == "pending"


@pytest.mark.asyncio
async def test_run_once_affected_rows_shortfall_logs_warning(caplog: Any) -> None:
    """When affected rows < expected, a warning is logged and no exception raised."""
    import logging

    delivery_ids = [uuid.uuid4(), uuid.uuid4()]
    notification_ids = [uuid.uuid4(), uuid.uuid4()]

    async def _execute(stmt: Any, params: Any = None):
        result = MagicMock()
        sql = " ".join(str(stmt).split())
        if "UPDATE notification_deliveries d" in sql and "RETURNING" in sql:
            result.mappings.return_value.all.return_value = [
                {
                    "delivery_id": did,
                    "notification_id": nid,
                    "tenant_id": _TENANT,
                    "webhook_url": "https://hook.example.com",
                    "attempt_number": 1,
                }
                for did, nid in zip(delivery_ids, notification_ids, strict=True)
            ]
            return result
        if "FROM notifications n" in sql and "LEFT JOIN subscriptions s" in sql:
            result.mappings.return_value.all.return_value = [
                {
                    "notification_id": nid,
                    "tenant_id": _TENANT,
                    "subscription_id": uuid.uuid4(),
                    "capability_id": uuid.uuid4(),
                    "capability_slug": "payment-api",
                    "event_kind": "version_published",
                    "change_classification": "non-breaking",
                    "version_before": "1.0.0",
                    "version_after": "1.1.0",
                    "occurred_at": _NOW,
                    "fetch_url": "https://example.com/cap/abc",
                    "hmac_secret": _SECRET,
                }
                for nid in notification_ids
            ]
            return result
        # Simulate that only 1 of the 2 rows was updated (one was deleted concurrently).
        result.rowcount = 1
        return result

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    worker = WebhookDeliveryWorker(factory, FakeClock(_NOW), http_client=http)

    with caplog.at_level(logging.WARNING, logger="catalog.workers.webhook_delivery"):
        try:
            attempted = await worker.run_once(batch_size=10)
        finally:
            await worker.close()

    assert attempted == 2
    assert any(
        "some delivery rows may have been deleted concurrently" in record.message for record in caplog.records
    ), "expected a warning about affected row shortfall"


@pytest.mark.asyncio
async def test_run_once_returns_zero_when_no_pending() -> None:
    async def _execute(stmt: Any, params: dict | None = None):
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        return result

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP should not be called when nothing is pending")

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    worker = WebhookDeliveryWorker(factory, FakeClock(_NOW), http_client=http)
    try:
        assert await worker.run_once() == 0
    finally:
        await worker.close()
