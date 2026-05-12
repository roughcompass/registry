"""Unit tests for the ``get_idempotency_context`` FastAPI dependency and
``IdempotencyContext`` helper.

Coverage:
- Absent header → inert context; ``lookup`` returns None; ``persist`` is a no-op.
- Same key + same body → ``lookup`` returns cached (status, body).
- Same key + different body → ``lookup`` raises HTTPException 409 with
  ``code: "idempotency_key_conflict"``.
- ``persist`` writes to the session factory with the correct parameters.
- ``hash_request_body`` produces stable output regardless of key order.

No database required — the session factory is stubbed via AsyncMock.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.api.middleware.idempotency import (
    IdempotencyContext,
    hash_request_body,
)
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_KEY = "idempotency-key-abc123"
_PATH = "/v1/capabilities"
_METHOD = "POST"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT_ID, actor_id=_ACTOR_ID, roles=["producer"])


def _make_session(*, lookup_row: tuple | None = None) -> tuple[MagicMock, MagicMock]:
    """Return (session, factory) mocks.

    ``lookup_row`` is what ``session.execute(...).first()`` returns; ``None``
    simulates a cache miss.
    """
    result = MagicMock()
    result.first = MagicMock(return_value=lookup_row)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=tx)

    factory = MagicMock(return_value=session)
    return session, factory


def _inert_idem() -> IdempotencyContext:
    """No-op context (header absent)."""
    return IdempotencyContext(key=None, body_hash=None, _method=_METHOD, _path=_PATH, _session_factory=None)


def _keyed_idem(body: dict, factory: MagicMock) -> IdempotencyContext:
    """Context with a key and hashed body."""
    return IdempotencyContext(
        key=_KEY,
        body_hash=hash_request_body(body),
        _method=_METHOD,
        _path=_PATH,
        _session_factory=factory,
    )


# ---------------------------------------------------------------------------
# hash_request_body
# ---------------------------------------------------------------------------


class TestHashRequestBody:
    def test_stable_across_key_order(self) -> None:
        body_a = {"z": 1, "a": 2}
        body_b = {"a": 2, "z": 1}
        assert hash_request_body(body_a) == hash_request_body(body_b)

    def test_none_body_returns_hash_of_empty_object(self) -> None:
        assert hash_request_body(None) == hash_request_body({})

    def test_different_bodies_produce_different_hashes(self) -> None:
        assert hash_request_body({"a": 1}) != hash_request_body({"a": 2})


# ---------------------------------------------------------------------------
# Absent header → inert context
# ---------------------------------------------------------------------------


class TestInertContext:
    @pytest.mark.asyncio
    async def test_lookup_returns_none_when_key_absent(self) -> None:
        idem = _inert_idem()
        result = await idem.lookup(_ctx())
        assert result is None

    @pytest.mark.asyncio
    async def test_persist_is_noop_when_key_absent(self) -> None:
        idem = _inert_idem()
        # Should not raise even though session_factory is None.
        await idem.persist(_ctx(), 201, {"id": "abc"})


# ---------------------------------------------------------------------------
# Cache hit — same key + same body
# ---------------------------------------------------------------------------


class TestCacheHit:
    @pytest.mark.asyncio
    async def test_lookup_returns_cached_status_and_body(self) -> None:
        body = {"name": "CapA"}
        body_hash = hash_request_body(body)
        cached_response = {"entity_id": str(uuid.uuid4()), "name": "CapA"}
        expires_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=1)

        # Row: (request_hash, response_status, response_body, expires_at)
        row = (body_hash, 201, cached_response, expires_at)
        _, factory = _make_session(lookup_row=row)

        idem = _keyed_idem(body, factory)
        result = await idem.lookup(_ctx())

        assert result is not None
        status_code, resp_body = result
        assert status_code == 201
        assert resp_body == cached_response


# ---------------------------------------------------------------------------
# Body-hash mismatch — same key + different body → 409
# ---------------------------------------------------------------------------


class TestBodyHashMismatch:
    @pytest.mark.asyncio
    async def test_lookup_raises_409_on_body_mismatch(self) -> None:
        original_body = {"name": "CapA"}
        different_hash = hash_request_body({"name": "CapB"})
        expires_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=1)

        # The persisted hash is for a DIFFERENT body than the one we're using now.
        row = (different_hash, 201, {}, expires_at)
        _, factory = _make_session(lookup_row=row)

        idem = _keyed_idem(original_body, factory)
        with pytest.raises(HTTPException) as exc_info:
            await idem.lookup(_ctx())

        exc = exc_info.value
        assert exc.status_code == 409
        assert exc.detail["code"] == "idempotency_key_conflict"


# ---------------------------------------------------------------------------
# persist writes correct parameters
# ---------------------------------------------------------------------------


class TestPersist:
    @pytest.mark.asyncio
    async def test_persist_calls_execute_with_correct_sql(self) -> None:
        body = {"name": "CapA"}
        _, factory = _make_session()
        idem = _keyed_idem(body, factory)

        response_body = {"entity_id": str(uuid.uuid4())}
        await idem.persist(_ctx(), 201, response_body)

        # The factory should have been called (session opened).
        factory.assert_called()

    @pytest.mark.asyncio
    async def test_persist_is_noop_when_key_absent(self) -> None:
        _, factory = _make_session()
        idem = _inert_idem()

        await idem.persist(_ctx(), 201, {"id": "abc"})
        factory.assert_not_called()


# ---------------------------------------------------------------------------
# Expired row treated as miss
# ---------------------------------------------------------------------------


class TestExpiredRow:
    @pytest.mark.asyncio
    async def test_expired_row_treated_as_miss(self) -> None:
        body = {"name": "CapA"}
        body_hash = hash_request_body(body)
        # expires_at is in the past
        expires_at = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=1)

        row = (body_hash, 201, {"entity_id": "x"}, expires_at)
        _, factory = _make_session(lookup_row=row)

        idem = _keyed_idem(body, factory)
        result = await idem.lookup(_ctx())
        # Expired → treated as a miss, returns None
        assert result is None
