"""X-Idempotency-Key handling for safe retry of POST writes.

How it works:
1. Caller sends ``X-Idempotency-Key: <opaque-string>`` on a POST.
2. Server hashes the request body and checks
   ``idempotency_keys (tenant_id, key, method, path)``.
3. Three cases:
   a. Row absent → proceed with the write. After the response is
      built, persist (status, body, request_hash) so a retry replays.
   b. Row present AND request_hash matches → return the persisted
      response unchanged.
   c. Row present AND request_hash differs → 409 with
      ``code: "idempotency_key_conflict"`` (the caller reused the key
      with a different payload — likely a bug).

The header is advisory: absent header → no idempotency tracking, write
proceeds normally.

Route handlers use the ``get_idempotency_context`` FastAPI dependency
which returns an ``IdempotencyContext``. The context exposes ``lookup``
and ``persist`` so each handler is a 3-line addition rather than
repeating the 10-line dance inline.

TTL is 24h; expired keys cycle. A periodic job sweeps them (out of
scope here — for now expired rows just sit until the next collision).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Header, HTTPException, Request, status
from sqlalchemy import text

if TYPE_CHECKING:
    from registry.types import TenantContext

_log = logging.getLogger(__name__)

_TTL = datetime.timedelta(hours=24)


def hash_request_body(body: Any) -> str:
    """Stable sha256 hex of the request body. Empty/None body → hash of '{}'."""
    canonical = json.dumps(body or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def lookup_idempotency_key(
    session: object,
    *,
    tenant_id: uuid.UUID,
    key: str,
    method: str,
    path: str,
    request_hash: str,
) -> tuple[int, dict[str, Any]] | None:
    """Look up the persisted response for *key*.

    Returns ``(status_code, body)`` on hit. None on miss (proceed with
    the write). Raises HTTPException(409) on body-hash mismatch.
    """
    row = (
        await session.execute(  # type: ignore[attr-defined]
            text(
                "SELECT request_hash, response_status, response_body, expires_at "
                "FROM idempotency_keys "
                "WHERE tenant_id = :tid AND key = :key "
                "AND method = :method AND path = :path "
                "LIMIT 1"
            ),
            {"tid": tenant_id, "key": key, "method": method, "path": path},
        )
    ).first()

    if row is None:
        return None

    persisted_hash, persisted_status, persisted_body, expires_at = row
    if expires_at is not None and expires_at < datetime.datetime.now(tz=datetime.UTC):
        # Stale row; treat as miss. Next persist call will overwrite.
        return None

    if persisted_hash != request_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_conflict",
                "message": (
                    "X-Idempotency-Key was reused with a different request body. " "Use a fresh key for a new request."
                ),
                "path": None,
            },
        )

    return int(persisted_status), persisted_body or {}


async def persist_idempotency_response(
    session: object,
    *,
    tenant_id: uuid.UUID,
    key: str,
    method: str,
    path: str,
    request_hash: str,
    response_status: int,
    response_body: Any,
) -> None:
    """Persist the (status, body) so a retry with the same key replays it.

    Upserts on the composite PK — the same key + body within a 24h
    window always returns the same response.
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    expires_at = now + _TTL
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO idempotency_keys "
            "(tenant_id, key, method, path, request_hash, response_status, "
            " response_body, created_at, expires_at) "
            "VALUES (:tid, :key, :method, :path, :hash, :rstatus, "
            "        CAST(:rbody AS JSONB), :now, :exp) "
            "ON CONFLICT (tenant_id, key, method, path) DO UPDATE SET "
            "  request_hash = EXCLUDED.request_hash, "
            "  response_status = EXCLUDED.response_status, "
            "  response_body = EXCLUDED.response_body, "
            "  expires_at = EXCLUDED.expires_at"
        ),
        {
            "tid": tenant_id,
            "key": key,
            "method": method,
            "path": path,
            "hash": request_hash,
            "rstatus": response_status,
            "rbody": json.dumps(response_body) if response_body is not None else None,
            "now": now,
            "exp": expires_at,
        },
    )


@dataclass
class IdempotencyContext:
    """Carries idempotency state for a single request.

    Constructed by ``get_idempotency_context``; injected into POST
    handlers via ``Depends``.  When ``key`` is ``None`` (header absent)
    both ``lookup`` and ``persist`` are no-ops so handlers don't need
    conditionals.

    The caller's ``TenantContext`` is passed explicitly to ``lookup`` and
    ``persist`` because the tenant dependency and the idempotency
    dependency are resolved independently by FastAPI.
    """

    key: str | None
    body_hash: str | None
    _method: str
    _path: str
    _session_factory: Any

    async def lookup(self, ctx: TenantContext) -> tuple[int, dict[str, Any]] | None:
        """Check the idempotency store.

        Returns ``(status_code, body)`` on cache hit so the handler can
        replay it immediately. Returns ``None`` when the key is absent or
        no prior response exists. Raises ``HTTPException(409)`` when the
        same key was used with a different body.
        """
        if self.key is None or self.body_hash is None:
            return None
        async with self._session_factory() as session:
            return await lookup_idempotency_key(
                session,
                tenant_id=ctx.tenant_id,
                key=self.key,
                method=self._method,
                path=self._path,
                request_hash=self.body_hash,
            )

    async def persist(self, ctx: TenantContext, response_status: int, response_body: Any) -> None:
        """Persist the response so future retries with the same key replay it."""
        if self.key is None or self.body_hash is None:
            return
        async with self._session_factory() as session, session.begin():
            await persist_idempotency_response(
                session,
                tenant_id=ctx.tenant_id,
                key=self.key,
                method=self._method,
                path=self._path,
                request_hash=self.body_hash,
                response_status=response_status,
                response_body=response_body,
            )


async def get_idempotency_context(
    request: Request,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> IdempotencyContext:
    """FastAPI dependency that resolves ``X-Idempotency-Key`` for a POST handler.

    When the header is absent the returned context is inert (both
    ``lookup`` and ``persist`` are no-ops).  When present the request body
    is hashed eagerly so the ``lookup`` call is cheap.

    The ``TenantContext`` is passed explicitly to ``lookup`` and ``persist``
    rather than being resolved here, because FastAPI resolves both
    dependencies independently in the same handler signature.
    """
    if x_idempotency_key is None:
        return IdempotencyContext(
            key=None,
            body_hash=None,
            _method=request.method,
            _path=request.url.path,
            _session_factory=getattr(request.app.state, "session_factory", None),
        )

    # Hash the raw body bytes so the hash is stable regardless of JSON
    # serialisation order differences introduced by Pydantic model parsing.
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        body_json = {}
    body_hash = hash_request_body(body_json)

    return IdempotencyContext(
        key=x_idempotency_key,
        body_hash=body_hash,
        _method=request.method,
        _path=request.url.path,
        _session_factory=request.app.state.session_factory,
    )


__all__ = [
    "hash_request_body",
    "lookup_idempotency_key",
    "persist_idempotency_response",
    "IdempotencyContext",
    "get_idempotency_context",
]
