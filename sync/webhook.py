"""GitHub/GitLab webhook receiver.

POST /webhooks/github?source_id={uuid}
  - Verifies HMAC-SHA256 signature via ``X-Hub-Signature-256`` header using
    the secret resolved from ``settings.webhook_secret_github`` (or the
    per-source env var named by ``GITHUB_WEBHOOK_SECRET``).
  - Extracts ``X-GitHub-Delivery`` → delivery_id.
  - Inserts ``webhook_deliveries`` row; duplicate PK is swallowed (idempotent).
  - Adds a one-shot APScheduler job to run the sync immediately.
  - Returns 200 immediately (non-blocking).

POST /webhooks/gitlab?source_id={uuid}
  - Same pattern with ``X-Gitlab-Token`` (HMAC-SHA256 constant-time compare)
    or ``X-Gitlab-Event-UUID`` → delivery_id.

Both endpoints are intentionally *public* (no Bearer auth) but HMAC-verified.
An invalid or missing signature returns 401.

The router is mounted by ``registry/main.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError

from registry.storage.models import WebhookDelivery

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verify_github_signature(body: bytes, secret: str, sig_header: str | None) -> None:
    """Raise 401 if ``X-Hub-Signature-256`` does not match HMAC-SHA256(body)."""
    if not sig_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Hub-Signature-256",
        )
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid GitHub webhook signature",
        )


def _verify_gitlab_token(secret: str, token_header: str | None) -> None:
    """Raise 401 if ``X-Gitlab-Token`` does not match the configured secret."""
    if not token_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Gitlab-Token",
        )
    if not hmac.compare_digest(secret, token_header):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid GitLab webhook token",
        )


async def _record_delivery_and_trigger(
    request: Request,
    source_id: uuid.UUID,
    delivery_id: str,
    trigger_label: str,
) -> None:
    """Idempotently record the delivery and enqueue a one-shot sync job.

    Duplicate delivery_ids (same ``(tenant_id, delivery_id)`` PK) are silently
    ignored — the response is still 200.  A ``SyncSource`` lookup is performed
    to resolve ``tenant_id``; a missing or inactive source raises 404.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from registry.storage.models import SyncSource  # noqa: PLC0415

    factory = request.app.state.session_factory
    scheduler = request.app.state.scheduler

    async with factory() as session:
        result = await session.execute(select(SyncSource).where(SyncSource.source_id == source_id))
        source: SyncSource | None = result.scalar_one_or_none()

    if source is None or not source.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sync_source {source_id} not found or inactive",
        )

    tenant_id = source.tenant_id
    now = datetime.now(tz=UTC)

    # Insert delivery record — ignore duplicate PK to satisfy idempotency.
    async with factory() as session, session.begin():
        try:
            session.add(
                WebhookDelivery(
                    tenant_id=tenant_id,
                    delivery_id=delivery_id,
                    source_id=source_id,
                    received_at=now,
                )
            )
            await session.flush()
        except IntegrityError:
            _log.info(
                "webhook: duplicate delivery_id=%s tenant=%s; skipping enqueue",
                delivery_id,
                tenant_id,
            )
            return  # 200 no-op

    # Enqueue a one-shot job that runs immediately (date trigger = now).
    from sync.runner import run_sync_job  # noqa: PLC0415

    job_id = f"webhook:{delivery_id}"
    settings = request.app.state.settings
    catalog = request.app.state.catalog

    scheduler.add_job(
        run_sync_job,
        trigger="date",
        run_date=now,
        kwargs={
            "source_id": str(source_id),
            "session_factory": factory,
            "catalog": catalog,
            "settings": settings,
            "trigger": "webhook",
            "delivery_id": delivery_id,
        },
        id=job_id,
        replace_existing=True,
        name=f"webhook:{trigger_label}:{source_id}",
    )
    _log.info(
        "webhook: enqueued one-shot job id=%s source=%s delivery=%s",
        job_id,
        source_id,
        delivery_id,
    )


# ---------------------------------------------------------------------------
# GitHub endpoint
# ---------------------------------------------------------------------------


@router.post("/github", status_code=status.HTTP_200_OK)
async def github_webhook(
    request: Request,
    source_id: uuid.UUID = Query(...),
    x_hub_signature_256: str | None = Header(None, alias="x-hub-signature-256"),
    x_github_delivery: str | None = Header(None, alias="x-github-delivery"),
) -> dict[str, str]:
    """Receive a GitHub push (or any event) webhook delivery."""
    settings = request.app.state.settings
    secret: str | None = settings.webhook_secret_github
    if not secret:
        # Per-instance secret override without restart — read from env.
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET")  # config: intentional
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GITHUB_WEBHOOK_SECRET not configured",
        )

    body = await request.body()
    _verify_github_signature(body, secret, x_hub_signature_256)

    delivery_id = x_github_delivery or f"gh-{uuid.uuid4()}"

    await _record_delivery_and_trigger(request, source_id, delivery_id, "github")
    return {"status": "accepted", "delivery_id": delivery_id}


# ---------------------------------------------------------------------------
# GitLab endpoint
# ---------------------------------------------------------------------------


@router.post("/gitlab", status_code=status.HTTP_200_OK)
async def gitlab_webhook(
    request: Request,
    source_id: uuid.UUID = Query(...),
    x_gitlab_token: str | None = Header(None, alias="x-gitlab-token"),
    x_gitlab_event_uuid: str | None = Header(None, alias="x-gitlab-event-uuid"),
) -> dict[str, str]:
    """Receive a GitLab push (or any event) webhook delivery."""
    settings = request.app.state.settings
    secret: str | None = settings.webhook_secret_gitlab
    if not secret:
        # Per-instance secret override without restart — read from env.
        secret = os.environ.get("GITLAB_WEBHOOK_SECRET")  # config: intentional
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GITLAB_WEBHOOK_SECRET not configured",
        )

    _verify_gitlab_token(secret, x_gitlab_token)

    delivery_id = x_gitlab_event_uuid or f"gl-{uuid.uuid4()}"

    await _record_delivery_and_trigger(request, source_id, delivery_id, "gitlab")
    return {"status": "accepted", "delivery_id": delivery_id}


__all__ = ["router", "_verify_github_signature", "_verify_gitlab_token"]
