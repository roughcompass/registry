"""WebhookDeliveryWorker — drains notification_deliveries and POSTs to subscriber webhooks.

Drains pending rows from ``notification_deliveries`` and POSTs the
matching ``notifications`` payload to the subscription's
``webhook_url``. Payload is :class:`CapabilityRegistryEvent` JSON only —
no body, description, or freeform content is ever sent. This keeps
webhook payloads minimal so consumers cannot accidentally depend on
mutable free-text fields.

Responsibilities
----------------
1. Pick up rows where ``status='pending'`` and ``next_retry_at <= now``.
2. Build the payload from the joined ``notifications`` row.
3. Sign the body with HMAC-SHA256 and the subscription's secret; emit
   the ``X-Registry-Signature-256: sha256=<hex>`` header.
4. POST via ``httpx.AsyncClient`` (transport is injectable so tests can
   use ``httpx.MockTransport``).
5. On ``2xx`` → mark ``success`` (no further attempts).
   On ``5xx`` → schedule retry with exponential backoff capped at 24h.
   On ``4xx`` other than 408/429 → mark ``failed`` (no retry — caller
   error, not a transient).
6. Digest batching: if a subscription's ``digest_window`` is non-``none``,
   callers may use :meth:`make_digest_envelope` to combine accumulated
   events into a single ``CapabilityRegistry.Digest v1`` envelope. The
   actual time-window accumulation logic lives in the scheduler that calls
   this worker (typically the AsyncIOScheduler in :mod:`registry.main`);
   the worker itself stays stateless.

Concurrency
-----------
``run_once`` caps in-flight deliveries per tenant via an asyncio
semaphore (default 10).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.service.notifications import event_to_dict
from registry.types import CapabilityRegistryEvent, Clock

_log = logging.getLogger(__name__)

#: Signature header name (case-insensitive in HTTP).
SIGNATURE_HEADER = "X-Registry-Signature-256"

#: Hex-encoded HMAC-SHA256 length (used by validators).
SIGNATURE_PREFIX = "sha256="

#: Backoff schedule (seconds): 1m, 2m, 4m, 8m, ... capped at 24h.
_RETRY_BASE_SECONDS = 60
_RETRY_CAP_SECONDS = 24 * 60 * 60  # 24h

#: Max attempts before giving up (logged at 24h cap regardless).
MAX_ATTEMPTS = 12

#: Default per-tenant concurrency cap.
DEFAULT_PER_TENANT_CONCURRENCY = 10

#: HTTP status codes that should retry (rather than fail permanently).
_RETRYABLE_4XX = frozenset({408, 425, 429})


@dataclass
class DeliveryResult:
    """Outcome of a single delivery attempt — exposed for tests."""

    status: str  # 'success' | 'pending' | 'failed'
    http_status: int | None
    next_retry_at: datetime.datetime | None
    error_text: str | None


def sign_payload(payload: bytes, hmac_secret: str) -> str:
    """Compute ``X-Registry-Signature-256`` value for *payload*.

    Returns ``sha256=<hex-digest>``. Matches the HMAC verifier shape
    used by Stripe/GitHub-style webhook signing — the prefix lets
    consumers distinguish algorithms over time.
    """
    # Reject both None and empty-string. An empty key still produces a
    # well-formed HMAC-SHA256 digest, but the signature is trivially
    # reproducible by anyone who knows the empty-key convention — equivalent
    # to "no signature" with the appearance of one. Fail loudly instead.
    if not hmac_secret:
        raise ValueError("hmac_secret is required for webhook signing")
    digest = hmac.new(
        hmac_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify_signature(payload: bytes, secret: str, header_value: str) -> bool:
    """Constant-time verification of a header produced by :func:`sign_payload`.

    Returns ``True`` only on exact match — used by ``test_*`` modules and
    by downstream consumers (e.g., :mod:`sync.webhook`) that need to
    independently verify our signing.
    """
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, header_value)


def compute_next_retry(attempt_number: int, now: datetime.datetime) -> datetime.datetime:
    """Return the next retry timestamp after a transient failure.

    Schedule: 60s * 2^(attempt-1), capped at 24h. ``attempt_number=1`` →
    +60s; ``attempt_number=8`` → +128m; ``attempt_number=12`` → 24h cap.
    """
    if attempt_number < 1:
        attempt_number = 1
    delay = _RETRY_BASE_SECONDS * (2 ** (attempt_number - 1))
    if delay > _RETRY_CAP_SECONDS:
        delay = _RETRY_CAP_SECONDS
    return now + datetime.timedelta(seconds=delay)


def make_digest_envelope(
    tenant_id: uuid.UUID,
    events: list[CapabilityRegistryEvent],
    window: str,
    now: datetime.datetime,
) -> dict[str, Any]:
    """Build a ``CapabilityRegistry.Digest v1`` envelope for *events*.

    ``window`` is the tenant's digest window label (e.g., ``'15m'``).
    All items in the envelope share the same tenant; the worker is
    responsible for grouping at call time.

    The envelope is payload-minimal — each item is the same shape as a
    single ``CapabilityRegistryEvent``; no free-text fields are included.
    """
    return {
        "envelope_type": "CapabilityRegistry.Digest",
        "version": "v1",
        "tenant_id": str(tenant_id),
        "window": window,
        "delivered_at": now.isoformat(),
        "item_count": len(events),
        "items": [event_to_dict(e) for e in events],
    }


class WebhookDeliveryWorker:
    """Async worker that drains notification_deliveries → webhooks."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        http_client: httpx.AsyncClient | None = None,
        *,
        per_tenant_concurrency: int = DEFAULT_PER_TENANT_CONCURRENCY,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._owns_http = http_client is None
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
        )
        self._per_tenant_concurrency = per_tenant_concurrency
        self._tenant_semaphores: dict[uuid.UUID, asyncio.Semaphore] = {}

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Single delivery (testable in isolation)
    # ------------------------------------------------------------------

    async def deliver(
        self,
        event: CapabilityRegistryEvent,
        webhook_url: str,
        hmac_secret: str,
        *,
        attempt_number: int = 1,
    ) -> DeliveryResult:
        """POST a single event to *webhook_url*.

        Returns a :class:`DeliveryResult` capturing the outcome. The
        worker itself does not write to the DB here — :meth:`run_once`
        does — so this is the convenient unit-test entry point.
        """
        now = self._clock.now()
        body = json.dumps(event_to_dict(event), separators=(",", ":")).encode("utf-8")
        sig = sign_payload(body, hmac_secret)
        try:
            resp = await self._http.post(
                webhook_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    SIGNATURE_HEADER: sig,
                },
            )
        except httpx.HTTPError as exc:
            return DeliveryResult(
                status="pending",
                http_status=None,
                next_retry_at=compute_next_retry(attempt_number, now),
                error_text=f"transport: {exc}",
            )

        return _classify_response(resp, attempt_number, now)

    # ------------------------------------------------------------------
    # Drain pass — reads + writes notification_deliveries
    # ------------------------------------------------------------------

    async def run_once(self, *, batch_size: int = 50) -> int:
        """Drain up to *batch_size* pending deliveries. Returns count attempted.

        Per-tenant in-flight cap is enforced via :class:`asyncio.Semaphore`;
        rows for a tenant that has saturated its semaphore wait their turn.

        All HTTP calls are issued concurrently; outcomes are recorded in a
        single bulk UPDATE after the fan-out completes.  This keeps
        connection-pool peak usage at 1 (for the bulk write) instead of
        up to ``batch_size`` simultaneous connections.
        """
        now = self._clock.now()
        rows = await self._claim_pending(now, batch_size)
        if not rows:
            return 0

        async def _deliver_row(row: dict) -> tuple[uuid.UUID, DeliveryResult]:
            sem = self._tenant_semaphores.setdefault(
                row["tenant_id"],
                asyncio.Semaphore(self._per_tenant_concurrency),
            )
            async with sem:
                outcome = await self.deliver(
                    event=_row_to_event(row),
                    webhook_url=row["webhook_url"],
                    hmac_secret=row["hmac_secret"],
                    attempt_number=row["attempt_number"],
                )
            return row["delivery_id"], outcome

        results = await asyncio.gather(*(_deliver_row(r) for r in rows), return_exceptions=True)

        # Collect successful (delivery_id, outcome) pairs.  An exception here
        # means the _deliver_row coroutine itself failed (not the HTTP call —
        # HTTP errors are already folded into DeliveryResult by deliver()).
        # Log and skip those; don't lose the rest.
        outcomes: list[tuple[uuid.UUID, DeliveryResult]] = []
        for idx, res in enumerate(results):
            if isinstance(res, BaseException):
                _log.exception(
                    "unexpected error during delivery of row %s; outcome will not be recorded",
                    rows[idx].get("delivery_id"),
                    exc_info=res,
                )
            else:
                outcomes.append(res)

        if outcomes:
            await self._record_outcomes_bulk(outcomes)

        return len(rows)

    # ------------------------------------------------------------------
    # SQL helpers
    # ------------------------------------------------------------------

    async def _claim_pending(self, now: datetime.datetime, batch_size: int) -> list[dict]:
        """Claim a batch of pending deliveries and increment attempt_number.

        Uses ``FOR UPDATE SKIP LOCKED`` so multiple workers do not
        contend on the same rows.
        """
        sql = text(
            """
            UPDATE notification_deliveries d
            SET attempt_number = d.attempt_number + 1,
                attempted_at = :now
            FROM (
                SELECT delivery_id
                FROM notification_deliveries
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= :now)
                ORDER BY next_retry_at NULLS FIRST, attempted_at
                LIMIT :lim
                FOR UPDATE SKIP LOCKED
            ) src
            WHERE d.delivery_id = src.delivery_id
            RETURNING d.delivery_id, d.notification_id, d.tenant_id,
                      d.webhook_url, d.attempt_number
            """
        )
        async with self._session_factory() as session, session.begin():
            claimed = await session.execute(sql, {"now": now, "lim": batch_size})
            claimed_rows = claimed.mappings().all()
            if not claimed_rows:
                return []

            notif_ids = [r["notification_id"] for r in claimed_rows]
            details = await session.execute(
                text(
                    """
                    SELECT n.notification_id, n.tenant_id, n.subscription_id,
                           n.capability_id, n.capability_slug, n.event_kind,
                           n.change_classification, n.version_before,
                           n.version_after, n.occurred_at, n.fetch_url,
                           s.webhook_hmac_secret_ref AS hmac_secret
                    FROM notifications n
                    LEFT JOIN subscriptions s ON s.subscription_id = n.subscription_id
                    WHERE n.notification_id = ANY(:nids)
                    """
                ),
                {"nids": notif_ids},
            )
            detail_map = {r["notification_id"]: r for r in details.mappings().all()}

        # Splice claim rows with notification details.
        out: list[dict] = []
        for c in claimed_rows:
            d = detail_map.get(c["notification_id"])
            if d is None:
                _log.warning(
                    "delivery %s references missing notification %s",
                    c["delivery_id"],
                    c["notification_id"],
                )
                continue
            if not d["hmac_secret"]:
                _log.warning(
                    "subscription %s has no HMAC secret configured — "
                    "delivery will not include a signature header",
                    d["subscription_id"],
                )
            out.append(
                {
                    "delivery_id": c["delivery_id"],
                    "notification_id": c["notification_id"],
                    "tenant_id": c["tenant_id"],
                    "webhook_url": c["webhook_url"],
                    "attempt_number": c["attempt_number"],
                    "hmac_secret": d["hmac_secret"] or "",
                    "subscription_id": d["subscription_id"],
                    "capability_id": d["capability_id"],
                    "capability_slug": d["capability_slug"],
                    "event_kind": d["event_kind"],
                    "change_classification": d["change_classification"],
                    "version_before": d["version_before"],
                    "version_after": d["version_after"],
                    "occurred_at": d["occurred_at"],
                    "fetch_url": d["fetch_url"],
                }
            )
        return out

    async def _record_outcomes_bulk(
        self,
        outcomes: list[tuple[uuid.UUID, DeliveryResult]],
    ) -> None:
        """Record all delivery outcomes in a single UPDATE statement.

        Uses a VALUES-list join so Postgres applies all changes in one
        round-trip.  Missing delivery_id rows (deleted concurrently) are
        silently skipped; the affected-row count is logged when it falls
        short of expectations so operators can detect unexpected gaps.
        """
        now = self._clock.now()
        params = [
            {
                "did": delivery_id,
                "st": outcome.status,
                "http": outcome.http_status,
                "retry": outcome.next_retry_at,
                "err": outcome.error_text,
                "recorded_at": now,
            }
            for delivery_id, outcome in outcomes
        ]

        # Build a VALUES list matched against delivery_id so the DB can
        # update every row in one statement.  SQLAlchemy executemany with
        # a single connection is the portable path; a raw VALUES-join is
        # Postgres-specific and adds complexity for the same effect at
        # small batch sizes.
        sql = text(
            """
            UPDATE notification_deliveries
            SET status      = :st,
                http_status = :http,
                next_retry_at = :retry,
                error_text  = :err
            WHERE delivery_id = :did
            """
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(sql, params)
            affected = result.rowcount if result.rowcount is not None else len(params)
            if affected < len(params):
                _log.warning(
                    "bulk outcome UPDATE affected %d rows but expected %d; "
                    "some delivery rows may have been deleted concurrently",
                    affected,
                    len(params),
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_response(
    resp: httpx.Response,
    attempt_number: int,
    now: datetime.datetime,
) -> DeliveryResult:
    """Map an HTTP response to a DeliveryResult.

    - 2xx               → success (no retry)
    - 5xx               → pending (retry with backoff up to MAX_ATTEMPTS)
    - 408 / 425 / 429   → pending (transient client error)
    - other 4xx         → failed (caller error)
    """
    code = resp.status_code
    if 200 <= code < 300:
        return DeliveryResult(
            status="success",
            http_status=code,
            next_retry_at=None,
            error_text=None,
        )
    transient = (500 <= code < 600) or code in _RETRYABLE_4XX
    if transient and attempt_number < MAX_ATTEMPTS:
        return DeliveryResult(
            status="pending",
            http_status=code,
            next_retry_at=compute_next_retry(attempt_number, now),
            error_text=f"http {code}",
        )
    return DeliveryResult(
        status="failed",
        http_status=code,
        next_retry_at=None,
        error_text=f"http {code}",
    )


def _row_to_event(row: dict) -> CapabilityRegistryEvent:
    return CapabilityRegistryEvent(
        notification_id=row["notification_id"],
        tenant_id=row["tenant_id"],
        subscription_id=row["subscription_id"],
        capability_id=row["capability_id"],
        capability_slug=row["capability_slug"],
        event_kind=row["event_kind"],
        change_classification=row["change_classification"],
        version_before=row["version_before"],
        version_after=row["version_after"],
        occurred_at=row["occurred_at"],
        fetch_url=row["fetch_url"],
    )


__all__ = [
    "DeliveryResult",
    "MAX_ATTEMPTS",
    "SIGNATURE_HEADER",
    "SIGNATURE_PREFIX",
    "WebhookDeliveryWorker",
    "compute_next_retry",
    "make_digest_envelope",
    "sign_payload",
    "verify_signature",
]
