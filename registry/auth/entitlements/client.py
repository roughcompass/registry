"""HTTP client for the enterprise entitlement service.

The registry calls the entitlement service on every request that misses
the per-JWT cache. This module isolates the HTTP shape, retry policy,
and status-class dispatch so failure-mode behavior is independently
testable without spinning up the full middleware.

The client never makes its own authorization decision — it only
classifies upstream responses into typed exceptions, and the resolver
decides what to do with each. This separation matters because some
failure modes are eligible for cache fallback (5xx, timeout, network
errors) and others are not (401/403 from upstream — meaning the JWT was
rejected — must propagate without consulting the cache, or stale data
could authorize a revoked token).

Status-class dispatch (matches the registry-side failure-mode contract):

| Upstream                       | Raised                          | Caller behavior                            |
|--------------------------------|---------------------------------|---------------------------------------------|
| 200 + valid body               | (returns list[str])             | proceed                                     |
| 200 + empty entitlements       | (returns [])                    | proceed (caller maps to 403)                |
| 401                            | EntitlementAuthError(401)       | propagate as 401; do NOT consult cache      |
| 403                            | EntitlementAuthError(403)       | propagate as 403; do NOT consult cache      |
| 404                            | EntitlementNotFoundError        | map to 403; do NOT consult cache            |
| 429 (after retry)              | EntitlementRateLimitError       | map to 503; do NOT consult cache            |
| 5xx / timeout / network        | EntitlementServiceError(cacheable=True) | cache fallback if available; else 503 |
| Malformed body / wrong shape   | EntitlementMalformedError       | map to 503; do NOT consult cache            |

Retry policy: at most ``settings.entitlement_max_retries`` retries on
network failure / 5xx / 429, with jittered backoff (50–150 ms). No
retry on any other 4xx. The total time spent (initial attempt + retries
+ backoffs) must respect the optional ``deadline`` argument so the
caller's overall request budget is never exceeded.

Lifespan: the ``httpx.AsyncClient`` instance is owned by the FastAPI app
(stored on ``app.state.entitlement_client``, closed on shutdown). This
module is a pure call-site helper; it does not own the client.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

import httpx
from prometheus_client import Counter, Histogram

if TYPE_CHECKING:
    from registry.config import Settings

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions — the resolver decides what each one means at the API layer.


class EntitlementClientError(Exception):
    """Base class for every error raised by the entitlement client."""


class EntitlementAuthError(EntitlementClientError):
    """Upstream rejected the forwarded JWT (HTTP 401 or 403).

    The caller MUST propagate the upstream status to the client without
    consulting the cache — using stale entitlements after upstream has
    explicitly de-authorized the caller would defeat the purpose of
    forwarding the JWT in the first place.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"entitlement service rejected upstream JWT (HTTP {status_code})")
        self.status_code = status_code


class EntitlementNotFoundError(EntitlementClientError):
    """Upstream returned 404 — no entitlement record exists for this user.

    Caller maps to 403 (no roles ⇒ access denied). Cache is not consulted
    because absence of a record is an authoritative answer.
    """


class EntitlementRateLimitError(EntitlementClientError):
    """Upstream returned 429 after the retry budget was exhausted.

    Caller maps to 503. Cache MUST NOT be consulted: 429 means the
    upstream is throttling, not that it is unavailable. Falling back to
    stale data on a rate-limit signal would mask back-pressure and could
    authorize a revoked token.
    """


class EntitlementServiceError(EntitlementClientError):
    """Upstream is unavailable: 5xx, timeout, or network failure.

    ``is_cacheable`` is always True for instances of this class — it is
    the marker the resolver uses to decide that stale-cache fallback is
    permitted. A separate field rather than ``isinstance`` keeps the
    contract explicit at call sites.
    """

    is_cacheable: bool = True

    def __init__(self, reason: str) -> None:
        super().__init__(f"entitlement service unavailable: {reason}")
        self.reason = reason


class EntitlementMalformedError(EntitlementClientError):
    """Upstream returned 200 but the body did not parse as the expected
    ``{"entitlements": [...]}`` shape.

    Caller maps to 503. Cache MUST NOT be consulted: the upstream is
    behaving outside contract and serving stale data on top of unknown
    new behavior risks compounding the bug.
    """


# ---------------------------------------------------------------------------
# Telemetry — counter labeled by HTTP status class plus a duration histogram.

_CALLS_TOTAL = Counter(
    "registry_entitlement_calls_total",
    "Entitlement service HTTP calls, labeled by outcome status class.",
    ["status_class"],
)

_CALL_DURATION = Histogram(
    "registry_entitlement_call_duration_seconds",
    "End-to-end latency of entitlement service HTTP calls (including retries).",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Labels used on _CALLS_TOTAL — bounded set keeps cardinality predictable.
_STATUS_2XX = "2xx"
_STATUS_4XX = "4xx_propagate"      # 401/403/404 — propagate, no cache
_STATUS_RATE = "4xx_rate_limited"  # 429 — map to 503, no cache
_STATUS_5XX = "5xx_cacheable"      # 5xx/timeout/network — cache fallback
_STATUS_MALFORMED = "malformed"    # 200 but wrong shape


def _backoff_seconds() -> float:
    """Jittered backoff between 50 ms and 150 ms."""
    return random.uniform(0.050, 0.150)


def _remaining_budget(deadline: float | None) -> float | None:
    """Seconds left before the optional ``deadline`` expires.

    ``deadline`` is an absolute ``time.monotonic()`` timestamp. Returns
    ``None`` when no deadline was supplied. Returns 0 when the deadline
    has already passed (caller treats as a timeout).
    """
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


async def _attempt_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    connect_timeout: float,
    read_timeout: float,
    budget_remaining: float | None,
) -> httpx.Response:
    """One HTTP attempt with timeout clamping. Raises on transport errors."""
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=read_timeout,
        pool=connect_timeout,
    )
    if budget_remaining is not None:
        # Clamp read timeout to the remaining request budget so a slow
        # upstream cannot consume more than the caller's deadline allows.
        timeout = httpx.Timeout(
            connect=min(connect_timeout, budget_remaining),
            read=min(read_timeout, budget_remaining),
            write=min(read_timeout, budget_remaining),
            pool=min(connect_timeout, budget_remaining),
        )
    return await client.get(url, headers=headers, timeout=timeout)


async def fetch_entitlements(
    client: httpx.AsyncClient,
    *,
    resolved_identity: str,
    raw_jwt: str,
    settings: Settings,
    deadline: float | None = None,
    request_id: str | None = None,
) -> list[str]:
    """Fetch raw entitlement strings from the upstream service.

    Parameters are keyword-only after ``client`` to keep call sites
    self-documenting and to make boundary tests (deadline-exhausted,
    missing JWT) read naturally.

    Returns the raw ``entitlements`` list from the upstream response.
    Empty list is a valid return — the caller decides what an empty list
    means (currently: 403 via the parser dropping all tuples).

    Raises one of the typed exceptions above for every non-success path.
    Never returns None.
    """
    url = (
        f"{settings.entitlement_service_url.rstrip('/')}"
        f"/api/v1/ldap-entitlements"
        f"?userId={resolved_identity}"
        f"&env={settings.entitlement_service_env}"
    )
    headers = {
        "Authorization": f"Bearer {raw_jwt}",
        "Accept": "application/json",
    }
    if request_id:
        headers["X-Request-ID"] = request_id

    connect_timeout = settings.entitlement_connect_timeout_ms / 1000.0
    read_timeout = settings.entitlement_read_timeout_ms / 1000.0
    max_retries = max(0, settings.entitlement_max_retries)

    attempts = 0
    last_retryable: Exception | None = None

    with _CALL_DURATION.time():
        while attempts <= max_retries:
            attempts += 1
            budget = _remaining_budget(deadline)
            if budget is not None and budget <= 0:
                # Budget exhausted before this attempt could even start —
                # treat identically to a read timeout per the failure-mode
                # contract.
                _CALLS_TOTAL.labels(status_class=_STATUS_5XX).inc()
                raise EntitlementServiceError("deadline exceeded before attempt")

            try:
                response = await _attempt_request(
                    client,
                    url,
                    headers,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    budget_remaining=budget,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_retryable = exc
                if attempts <= max_retries:
                    await asyncio.sleep(_backoff_seconds())
                    continue
                _CALLS_TOTAL.labels(status_class=_STATUS_5XX).inc()
                raise EntitlementServiceError(f"transport error: {type(exc).__name__}") from exc

            status = response.status_code

            if status == 200:
                try:
                    body = response.json()
                except (ValueError, httpx.DecodingError) as exc:
                    _CALLS_TOTAL.labels(status_class=_STATUS_MALFORMED).inc()
                    raise EntitlementMalformedError(
                        "entitlement service returned non-JSON body"
                    ) from exc
                if not isinstance(body, dict) or "entitlements" not in body:
                    _CALLS_TOTAL.labels(status_class=_STATUS_MALFORMED).inc()
                    raise EntitlementMalformedError(
                        "entitlement service response missing 'entitlements' key"
                    )
                entitlements = body["entitlements"]
                if not isinstance(entitlements, list) or not all(
                    isinstance(item, str) for item in entitlements
                ):
                    _CALLS_TOTAL.labels(status_class=_STATUS_MALFORMED).inc()
                    raise EntitlementMalformedError(
                        "entitlement service 'entitlements' field is not a list of strings"
                    )
                _CALLS_TOTAL.labels(status_class=_STATUS_2XX).inc()
                return entitlements

            if status in (401, 403):
                # Authoritative authorization signal from upstream — must
                # propagate, never fall back to cache.
                _CALLS_TOTAL.labels(status_class=_STATUS_4XX).inc()
                raise EntitlementAuthError(status)

            if status == 404:
                _CALLS_TOTAL.labels(status_class=_STATUS_4XX).inc()
                raise EntitlementNotFoundError()

            if status == 429:
                last_retryable = EntitlementRateLimitError()
                if attempts <= max_retries:
                    await asyncio.sleep(_backoff_seconds())
                    continue
                _CALLS_TOTAL.labels(status_class=_STATUS_RATE).inc()
                raise EntitlementRateLimitError()

            if 500 <= status < 600:
                last_retryable = EntitlementServiceError(f"upstream {status}")
                if attempts <= max_retries:
                    await asyncio.sleep(_backoff_seconds())
                    continue
                _CALLS_TOTAL.labels(status_class=_STATUS_5XX).inc()
                raise EntitlementServiceError(f"upstream {status}")

            # Unmapped status code — treat as malformed since it violates
            # the documented contract.
            _CALLS_TOTAL.labels(status_class=_STATUS_MALFORMED).inc()
            raise EntitlementMalformedError(f"unmapped upstream status {status}")

        # Loop exited without success or terminal raise — should never
        # happen because every branch either continues or raises, but
        # keep a defensive raise so mypy can prove the function returns
        # list[str] on success and raises otherwise.
        if isinstance(last_retryable, EntitlementClientError):
            raise last_retryable
        raise EntitlementServiceError("retries exhausted")


__all__ = [
    "EntitlementAuthError",
    "EntitlementClientError",
    "EntitlementMalformedError",
    "EntitlementNotFoundError",
    "EntitlementRateLimitError",
    "EntitlementServiceError",
    "fetch_entitlements",
]
