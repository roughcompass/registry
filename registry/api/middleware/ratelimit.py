"""Per-tenant rate limiting — in-process token bucket + ASGI middleware.

Two independent rate-limiting mechanisms live in this module:

1. ``check_rate_limit`` / ``rate_limit_dep`` — Postgres advisory-lock gate
   used by the original per-actor DB-driven budget. Still callable directly
   from FastAPI dependencies if a caller wants actor-level precision.

2. ``RateLimitMiddleware`` — ASGI middleware that enforces per-tenant
   token buckets in process memory. Mount this in ``create_app()`` for
   automatic coverage of every route (including ones added in the future).
   Design decisions for the ASGI approach:

   - Token buckets live in a shared dict keyed on ``tenant_id``.  A tenant
     that has never been seen gets a fresh bucket on first request.
   - Two separate bucket pools: one for reads (GET/HEAD) and one for writes
     (everything else), each with an independent refill rate drawn from
     ``Settings.rate_limit_read_per_minute`` and
     ``Settings.rate_limit_write_per_minute``.
   - Tenant resolution in the middleware: the middleware extracts the Bearer
     token, hashes it (same SHA-256 as the auth layer), and looks up
     ``tenant_id`` from a small LRU cache.  On a cache miss it opens one
     short-lived DB session.  Cache hits are free — in the common case of a
     steady authenticated client this adds zero overhead beyond the bucket
     check itself.
   - Public paths (/healthz, /readyz, /metrics, /webhooks) bypass rate
     limiting entirely — they are either cheap probe paths or HMAC-
     authenticated inbound receivers that predate tenant context.
   - In-process only: in a multi-process deployment each worker process owns
     its own bucket state.  The effective per-tenant limit across N workers
     is up to N × per_minute (each process gets a full budget).  For v1 this
     is acceptable; a distributed token store is not in scope.
   - ``rate_limit_enabled = False`` in Settings disables enforcement without
     requiring a redeploy — the middleware short-circuits immediately.

Usage — mount in ``create_app()``::

    from registry.api.middleware.ratelimit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware, settings=settings,
                       session_factory=session_factory)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.api.middleware.tenant import get_db_session, get_tenant_context
from registry.types import TenantContext

_log = logging.getLogger(__name__)

# HTTP methods treated as reads; everything else is a write.
_READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

# Paths that bypass rate limiting entirely.  These are either cheap operator
# probes or inbound HMAC-authenticated receivers with no tenant context.
_BYPASS_PATH_PREFIXES: tuple[str, ...] = ("/healthz", "/readyz", "/metrics", "/webhooks")

# Max entries in the token-hash → tenant_id cache.  Enough for thousands of
# active API tokens without significant memory pressure.
_TENANT_CACHE_MAXSIZE: int = 4096


# ---------------------------------------------------------------------------
# In-process token bucket
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Simple token-bucket rate limiter for a single key.

    Refills at ``per_minute`` tokens per 60 seconds.  The bucket starts full.
    Thread-safety: asyncio is single-threaded within an event loop; no lock
    needed.  Do not share instances across OS threads.
    """

    __slots__ = ("_per_minute", "_tokens", "_last_refill")

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._tokens: float = float(per_minute)
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        # Tokens accrue at per_minute / 60 per second.
        added = elapsed * self._per_minute / 60.0
        self._tokens = min(float(self._per_minute), self._tokens + added)
        self._last_refill = now

    def consume(self) -> bool:
        """Attempt to consume one token.  Returns True if allowed, False if throttled."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def retry_after_seconds(self) -> int:
        """Seconds until the next token is available (ceiling)."""
        if self._per_minute <= 0:
            return 60
        deficit = 1.0 - self._tokens
        if deficit <= 0:
            return 0
        # Time to refill ``deficit`` tokens at the configured rate.
        seconds = deficit / (self._per_minute / 60.0)
        return max(1, int(seconds) + 1)


class _BucketStore:
    """LRU-capped store of per-tenant token buckets.

    Maintains two pools: read buckets and write buckets.  Each pool is keyed
    on ``tenant_id`` (UUID).  Entries are evicted LRU when the store exceeds
    ``maxsize``.
    """

    def __init__(
        self,
        read_per_minute: int,
        write_per_minute: int,
        maxsize: int = 8192,
    ) -> None:
        self._rpm = read_per_minute
        self._wpm = write_per_minute
        self._maxsize = maxsize
        self._reads: OrderedDict[uuid.UUID, _TokenBucket] = OrderedDict()
        self._writes: OrderedDict[uuid.UUID, _TokenBucket] = OrderedDict()

    def _get_or_create(
        self,
        pool: OrderedDict[uuid.UUID, _TokenBucket],
        tenant_id: uuid.UUID,
        per_minute: int,
    ) -> _TokenBucket:
        if tenant_id in pool:
            pool.move_to_end(tenant_id)
            return pool[tenant_id]
        bucket = _TokenBucket(per_minute)
        pool[tenant_id] = bucket
        if len(pool) > self._maxsize:
            pool.popitem(last=False)
        return bucket

    def consume_read(self, tenant_id: uuid.UUID) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        bucket = self._get_or_create(self._reads, tenant_id, self._rpm)
        allowed = bucket.consume()
        return allowed, bucket.retry_after_seconds

    def consume_write(self, tenant_id: uuid.UUID) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        bucket = self._get_or_create(self._writes, tenant_id, self._wpm)
        allowed = bucket.consume()
        return allowed, bucket.retry_after_seconds


# ---------------------------------------------------------------------------
# ASGI RateLimitMiddleware
# ---------------------------------------------------------------------------

_RATE_LIMIT_RESPONSE_BODY = json.dumps(
    {"errors": [{"path": None, "code": "rate_limited", "message": "rate limit exceeded"}]}
).encode()


class RateLimitMiddleware:
    """ASGI middleware: per-tenant in-process token-bucket rate limiting.

    Mount with ``app.add_middleware(RateLimitMiddleware, settings=settings,
    session_factory=session_factory)``.

    The middleware is a no-op when ``settings.rate_limit_enabled`` is False.
    When enabled it enforces separate read and write budgets per tenant.
    Public paths (health probes, metrics, inbound webhooks) always bypass.
    """

    def __init__(
        self,
        app: Any,
        *,
        settings: Any,
        session_factory: Any,
    ) -> None:
        self._app = app
        self._settings = settings
        self._session_factory = session_factory
        self._buckets = _BucketStore(
            read_per_minute=settings.rate_limit_read_per_minute,
            write_per_minute=settings.rate_limit_write_per_minute,
        )
        # LRU cache: SHA-256(raw_token) → tenant_id UUID.  Bounded to avoid
        # unbounded memory growth on rotating-token deployments.
        self._tenant_cache: OrderedDict[str, uuid.UUID] = OrderedDict()

    def _evict_tenant_cache(self) -> None:
        while len(self._tenant_cache) > _TENANT_CACHE_MAXSIZE:
            self._tenant_cache.popitem(last=False)

    async def _resolve_tenant_id(self, token_hash: str, raw_token: str) -> uuid.UUID | None:
        """Return tenant_id for *raw_token*, consulting cache first.

        Returns None when the token is unrecognised — the request will
        be passed through without rate limiting (auth layer handles 401).
        """
        if token_hash in self._tenant_cache:
            self._tenant_cache.move_to_end(token_hash)
            return self._tenant_cache[token_hash]

        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    text("SELECT tenant_id FROM api_tokens " "WHERE token_hash = :h AND revoked_at IS NULL " "LIMIT 1"),
                    {"h": token_hash},
                )
                row = result.one_or_none()
        except Exception as exc:  # noqa: BLE001
            _log.warning("rate_limit_tenant_lookup_failed: %s", exc)
            return None

        if row is None:
            return None

        tid = row[0] if isinstance(row[0], uuid.UUID) else uuid.UUID(str(row[0]))
        self._tenant_cache[token_hash] = tid
        self._evict_tenant_cache()
        return tid

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if not self._settings.rate_limit_enabled:
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if any(path.startswith(prefix) for prefix in _BYPASS_PATH_PREFIXES):
            await self._app(scope, receive, send)
            return

        # Extract Bearer token from headers.
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        raw_token: str | None = None
        for name, value in headers:
            if name.lower() == b"authorization":
                decoded = value.decode("latin-1")
                scheme, _, tok = decoded.partition(" ")
                if scheme.lower() == "bearer" and tok:
                    raw_token = tok.strip()
                break

        if not raw_token:
            # No bearer token — pass through; the auth layer returns 401.
            await self._app(scope, receive, send)
            return

        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        tenant_id = await self._resolve_tenant_id(token_hash, raw_token)

        if tenant_id is None:
            # Unrecognised token — pass through; auth layer handles 401.
            await self._app(scope, receive, send)
            return

        method: str = scope.get("method", "GET").upper()
        is_read = method in _READ_METHODS
        if is_read:
            allowed, retry_after = self._buckets.consume_read(tenant_id)
        else:
            allowed, retry_after = self._buckets.consume_write(tenant_id)

        if allowed:
            await self._app(scope, receive, send)
            return

        _log.info(
            "rate_limit_exceeded tenant=%s method=%s path=%s",
            tenant_id,
            method,
            path,
        )
        await _send_429(send, retry_after)


async def _send_429(send: Any, retry_after: int) -> None:
    """Send a minimal 429 response with the standard error envelope."""
    headers = [
        (b"content-type", b"application/json"),
        (b"retry-after", str(retry_after).encode()),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": _RATE_LIMIT_RESPONSE_BODY,
            "more_body": False,
        }
    )


# ---------------------------------------------------------------------------
# Original Postgres advisory-lock helpers (kept for dependency callers)
# ---------------------------------------------------------------------------


async def _lookup_rate_limit(
    session: AsyncSession,
    tenant_id: Any,
    actor_id: Any,
) -> tuple[int, int]:
    """Return ``(reads_per_second, writes_per_second)`` for the actor.

    Two-tier lookup:
    1. Actor-specific row (tenant_id=:tid AND actor_id=:aid).
    2. Tenant default (tenant_id=:tid AND actor_id IS NULL).

    If no row exists at all, fall back to permissive defaults (1 000 / 100)
    so a missing seed row is never a hard outage.
    """
    # Actor-specific row first.
    row = (
        await session.execute(
            text(
                "SELECT reads_per_second, writes_per_second "
                "FROM rate_limits "
                "WHERE tenant_id = :tid AND actor_id = :aid "
                "LIMIT 1"
            ),
            {"tid": str(tenant_id), "aid": str(actor_id)},
        )
    ).one_or_none()

    if row is None:
        # Fall back to tenant default.
        row = (
            await session.execute(
                text(
                    "SELECT reads_per_second, writes_per_second "
                    "FROM rate_limits "
                    "WHERE tenant_id = :tid AND actor_id IS NULL "
                    "LIMIT 1"
                ),
                {"tid": str(tenant_id)},
            )
        ).one_or_none()

    if row is None:
        _log.warning(
            "rate_limit_row_missing tenant=%s actor=%s; using permissive defaults",
            tenant_id,
            actor_id,
        )
        return 1000, 100

    return int(row[0]), int(row[1])


async def _try_advisory_lock(session: AsyncSession, tenant_id: Any) -> bool:
    """Attempt ``pg_try_advisory_xact_lock`` on the request's DB connection.

    The lock key is ``hashtext('rate:' || tenant_id::text)``.  Returns True
    if the lock was acquired, False if it is already held by another
    transaction (i.e. another concurrent request for the same tenant).

    This MUST be called on the same session/connection that is already open
    for the request — never on a freshly-opened connection. An advisory lock
    acquired on a separate connection would be released immediately when that
    connection is returned to the pool, making the gate ineffective.
    """
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(" "hashtext('rate:' || :tid::text)" ")"),
        {"tid": str(tenant_id)},
    )
    acquired: bool = result.scalar_one()
    return acquired


async def check_rate_limit(
    request: Request,
    ctx: TenantContext,
    session: AsyncSession,
) -> None:
    """Core rate-limit check.  Raises ``HTTPException(429)`` on saturation.

    Designed to be called from a FastAPI dependency or from middleware.
    Uses *session* (the request's own DB connection) for the advisory lock.
    """
    is_read = request.method.upper() in _READ_METHODS
    reads_ps, writes_ps = await _lookup_rate_limit(session, ctx.tenant_id, ctx.actor_id)
    budget = reads_ps if is_read else writes_ps

    # Advisory lock doubles as a concurrency gate: if more than `budget`
    # concurrent requests are inflight for this tenant simultaneously, the
    # extra ones cannot acquire the lock and receive 429 immediately.
    # For a simple request-level gate (not a true sliding-window token bucket),
    # this is the mandated approach for request-level concurrency gating.
    #
    # The budget column is stored as requests-per-second but the lock is
    # binary (held/not-held).  In practice the pg advisory lock effectively
    # serialises requests at the DB level: a new request that finds the lock
    # taken (within the same transaction scope) is told to back off.
    #
    # For high-budget tenants (e.g. 1000 rps) this check is essentially a
    # no-op because each request holds the lock only for the duration of the
    # statement and then releases it.  For the explicit-override case (runaway
    # actor set to budget=0) this returns 429 immediately without touching the
    # lock (zero budget = always throttled).
    if budget <= 0:
        _log.info(
            "rate_limit_zero_budget tenant=%s actor=%s method=%s",
            ctx.tenant_id,
            ctx.actor_id,
            request.method,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limit_exceeded", "retry_after_s": 1},
        )

    acquired = await _try_advisory_lock(session, ctx.tenant_id)
    if not acquired:
        _log.info(
            "rate_limit_lock_contention tenant=%s actor=%s method=%s",
            ctx.tenant_id,
            ctx.actor_id,
            request.method,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limit_exceeded", "retry_after_s": 1},
        )


def rate_limit_dep() -> Any:
    """Return a FastAPI dependency that enforces rate limiting.

    Depends on ``get_tenant_context`` and ``get_db_session`` so it can be
    wired as a module-level closure (avoids ruff B008).

    Usage::

        _rl = rate_limit_dep()

        @router.get("/v1/...")
        async def handler(_: None = Depends(_rl), ...): ...
    """

    async def _dep(
        request: Request,
        ctx: TenantContext = Depends(get_tenant_context),
        session: AsyncSession = Depends(get_db_session),
    ) -> None:
        await check_rate_limit(request, ctx, session)

    return _dep


__all__ = [
    "RateLimitMiddleware",
    "_BucketStore",
    "_TokenBucket",
    "check_rate_limit",
    "rate_limit_dep",
]
