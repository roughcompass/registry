"""Unit tests for registry.api.auth.tokens.

Covers hash equality, expired-token rejection, revoked-token rejection,
and not-found rejection. The DB session is mocked — testcontainers
integration is exercised in tests/integration/test_phase0.py.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.api.auth.tokens import hash_token, validate_token
from registry.exceptions import CatalogError
from registry.storage.models import ApiToken
from registry.types import FakeClock


def _make_token(
    *,
    token_hash: str,
    revoked_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
    roles: list[str] | None = None,
) -> ApiToken:
    token = ApiToken()
    token.token_id = uuid.uuid4()
    token.tenant_id = uuid.uuid4()
    token.actor_id = uuid.uuid4()
    token.token_hash = token_hash
    token.roles = roles if roles is not None else ["producer"]
    token.description = "test"
    token.expires_at = expires_at
    token.revoked_at = revoked_at
    token.created_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    return token


def _session_returning(token: ApiToken | None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=token)
    session.execute = AsyncMock(return_value=result)
    return session


def test_hash_token_is_sha256_hex() -> None:
    # Stable known value: sha256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
    assert hash_token("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_hash_token_distinguishes_inputs() -> None:
    assert hash_token("a") != hash_token("b")


@pytest.mark.asyncio
async def test_validate_token_returns_tenant_context_on_match() -> None:
    raw = "secret-token"
    token = _make_token(token_hash=hash_token(raw))
    session = _session_returning(token)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    ctx = await validate_token(session, raw, clock)

    assert ctx.tenant_id == token.tenant_id
    assert ctx.actor_id == token.actor_id
    assert ctx.roles == ["producer"]


@pytest.mark.asyncio
async def test_validate_token_rejects_missing_token() -> None:
    session = _session_returning(None)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    with pytest.raises(CatalogError):
        await validate_token(session, "", clock)


@pytest.mark.asyncio
async def test_validate_token_rejects_unknown_token() -> None:
    session = _session_returning(None)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    with pytest.raises(CatalogError):
        await validate_token(session, "unknown-token", clock)


@pytest.mark.asyncio
async def test_validate_token_rejects_revoked_token() -> None:
    raw = "revoked-token"
    token = _make_token(
        token_hash=hash_token(raw),
        revoked_at=datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC),
    )
    session = _session_returning(token)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    with pytest.raises(CatalogError):
        await validate_token(session, raw, clock)


@pytest.mark.asyncio
async def test_validate_token_rejects_expired_token() -> None:
    raw = "expired-token"
    token = _make_token(
        token_hash=hash_token(raw),
        expires_at=datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC),
    )
    session = _session_returning(token)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    with pytest.raises(CatalogError):
        await validate_token(session, raw, clock)


@pytest.mark.asyncio
async def test_validate_token_accepts_token_with_future_expiry() -> None:
    raw = "future-token"
    token = _make_token(
        token_hash=hash_token(raw),
        expires_at=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
    )
    session = _session_returning(token)
    clock = FakeClock(datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC))

    ctx = await validate_token(session, raw, clock)
    assert ctx.tenant_id == token.tenant_id
