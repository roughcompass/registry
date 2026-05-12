"""Unit tests for sync/webhook.py.

Coverage:
- HMAC-SHA256 signature verification (GitHub): valid, missing header, wrong sig.
- GitLab token verification: valid, missing header, wrong token.
- _record_delivery_and_trigger: duplicate delivery_id is no-op (200).
- _record_delivery_and_trigger: inactive/missing source_id raises 404.
- github_webhook endpoint: valid delivery enqueues job.
- gitlab_webhook endpoint: valid delivery enqueues job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from sync.webhook import _verify_github_signature, _verify_gitlab_token

# ---------------------------------------------------------------------------
# _verify_github_signature
# ---------------------------------------------------------------------------


def _make_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_github_signature_valid() -> None:
    body = b'{"ref": "refs/heads/main"}'
    secret = "supersecret"
    sig = _make_sig(secret, body)
    _verify_github_signature(body, secret, sig)  # must not raise


def test_verify_github_signature_missing_header() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _verify_github_signature(b"body", "secret", None)
    assert exc_info.value.status_code == 401


def test_verify_github_signature_wrong_sig() -> None:
    body = b'{"ref": "refs/heads/main"}'
    with pytest.raises(HTTPException) as exc_info:
        _verify_github_signature(body, "secret", "sha256=deadbeef")
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# _verify_gitlab_token
# ---------------------------------------------------------------------------


def test_verify_gitlab_token_valid() -> None:
    _verify_gitlab_token("mytoken", "mytoken")  # must not raise


def test_verify_gitlab_token_missing() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _verify_gitlab_token("mytoken", None)
    assert exc_info.value.status_code == 401


def test_verify_gitlab_token_wrong() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _verify_gitlab_token("mytoken", "wrongtoken")
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# _record_delivery_and_trigger — helpers
# ---------------------------------------------------------------------------


def _make_mock_source(
    source_id: uuid.UUID,
    tenant_id: uuid.UUID,
    is_active: bool = True,
) -> MagicMock:
    source = MagicMock()
    source.source_id = source_id
    source.tenant_id = tenant_id
    source.is_active = is_active
    return source


def _make_request(
    source: MagicMock | None,
    raise_integrity: bool = False,
) -> MagicMock:
    """Build a mock Request whose app.state has session_factory and scheduler."""
    from registry.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_use_memory_jobstore=True,
        webhook_secret_github="gh-secret",
        webhook_secret_gitlab="gl-secret",
    )

    # Build an async context manager that represents session.begin()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = source
    session.execute = AsyncMock(return_value=result_mock)

    if raise_integrity:
        session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, Exception()))
    else:
        session.flush = AsyncMock()

    session.commit = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session)

    scheduler = MagicMock()

    catalog = MagicMock()

    app_state = MagicMock()
    app_state.session_factory = session_factory
    app_state.scheduler = scheduler
    app_state.settings = settings
    app_state.catalog = catalog

    request = MagicMock()
    request.app.state = app_state

    return request


# ---------------------------------------------------------------------------
# _record_delivery_and_trigger tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_delivery_missing_source_raises_404() -> None:
    from sync.webhook import _record_delivery_and_trigger

    request = _make_request(source=None)
    with pytest.raises(HTTPException) as exc_info:
        await _record_delivery_and_trigger(request, uuid.uuid4(), "delivery-1", "github")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_record_delivery_inactive_source_raises_404() -> None:
    from sync.webhook import _record_delivery_and_trigger

    source_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    source = _make_mock_source(source_id, tenant_id, is_active=False)
    request = _make_request(source=source)

    with pytest.raises(HTTPException) as exc_info:
        await _record_delivery_and_trigger(request, source_id, "delivery-1", "github")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_record_delivery_enqueues_job() -> None:
    from sync.webhook import _record_delivery_and_trigger

    source_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    source = _make_mock_source(source_id, tenant_id, is_active=True)
    request = _make_request(source=source)

    with patch("sync.runner.run_sync_job"):
        await _record_delivery_and_trigger(request, source_id, "gh-delivery-xyz", "github")

    request.app.state.scheduler.add_job.assert_called_once()
    call_kwargs = request.app.state.scheduler.add_job.call_args
    assert call_kwargs.kwargs["id"] == "webhook:gh-delivery-xyz"


@pytest.mark.asyncio
async def test_record_delivery_duplicate_is_noop() -> None:
    """IntegrityError on flush → no job enqueued, function returns normally."""
    from sync.webhook import _record_delivery_and_trigger

    source_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    source = _make_mock_source(source_id, tenant_id, is_active=True)
    request = _make_request(source=source, raise_integrity=True)

    with patch("sync.runner.run_sync_job"):
        await _record_delivery_and_trigger(request, source_id, "gh-delivery-dup", "github")

    # No scheduler.add_job call — duplicate was swallowed.
    request.app.state.scheduler.add_job.assert_not_called()


# ---------------------------------------------------------------------------
# Endpoint smoke tests via TestClient
# ---------------------------------------------------------------------------


def _make_app_with_source(source: MagicMock | None) -> Any:
    """Return a FastAPI TestClient wrapping just the webhook router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from registry.config import Settings
    from sync.webhook import router

    settings = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_use_memory_jobstore=True,
        webhook_secret_github="gh-secret",
        webhook_secret_gitlab="gl-secret",
    )

    app = FastAPI()

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = source
    session.execute = AsyncMock(return_value=result_mock)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session)

    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.scheduler = MagicMock()
    app.state.catalog = MagicMock()

    app.include_router(router)

    return TestClient(app, raise_server_exceptions=True)


def test_github_endpoint_invalid_sig() -> None:
    source_id = uuid.uuid4()
    source = _make_mock_source(source_id, uuid.uuid4(), is_active=True)
    client = _make_app_with_source(source)
    body = json.dumps({"ref": "refs/heads/main"}).encode()

    resp = client.post(
        f"/webhooks/github?source_id={source_id}",
        content=body,
        headers={
            "x-hub-signature-256": "sha256=badhash",
            "x-github-delivery": "test-delivery-001",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_github_endpoint_valid_delivery() -> None:
    source_id = uuid.uuid4()
    source = _make_mock_source(source_id, uuid.uuid4(), is_active=True)
    client = _make_app_with_source(source)
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    sig = _make_sig("gh-secret", body)

    with patch("sync.runner.run_sync_job"):
        resp = client.post(
            f"/webhooks/github?source_id={source_id}",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-delivery": "test-delivery-001",
                "content-type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["delivery_id"] == "test-delivery-001"


def test_gitlab_endpoint_invalid_token() -> None:
    source_id = uuid.uuid4()
    source = _make_mock_source(source_id, uuid.uuid4(), is_active=True)
    client = _make_app_with_source(source)

    resp = client.post(
        f"/webhooks/gitlab?source_id={source_id}",
        content=b"{}",
        headers={
            "x-gitlab-token": "wrongtoken",
            "x-gitlab-event-uuid": "gl-uuid-001",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_gitlab_endpoint_valid_delivery() -> None:
    source_id = uuid.uuid4()
    source = _make_mock_source(source_id, uuid.uuid4(), is_active=True)
    client = _make_app_with_source(source)

    with patch("sync.runner.run_sync_job"):
        resp = client.post(
            f"/webhooks/gitlab?source_id={source_id}",
            content=b"{}",
            headers={
                "x-gitlab-token": "gl-secret",
                "x-gitlab-event-uuid": "gl-uuid-001",
                "content-type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["delivery_id"] == "gl-uuid-001"
