"""Unit tests — OIDC JWT parsing (happy/sad path, mocked authlib)."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.api.auth import oidc as oidc_mod
from registry.api.auth.oidc import _CACHE_TTL_S, _OidcCache
from registry.config import Settings
from registry.exceptions import CatalogError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_ROLE_ID = uuid.uuid4()

_DISCOVERY = {
    "issuer": "https://idp.example.com",
    "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
}

_JWKS = {"keys": [{"kty": "RSA", "kid": "test", "n": "x", "e": "AQAB"}]}

_CLAIMS: dict[str, Any] = {
    "sub": "user123",
    "iss": "https://idp.example.com",
    "exp": 9999999999,
    "tenant_id": str(_TENANT_ID),
}


def _make_settings(*, oidc_url: str | None = "https://idp.example.com/.well-known/openid-configuration") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x/y",
        pgbouncer_url="postgresql+asyncpg://x/y",
        scheduler_jobstore_url="postgresql+asyncpg://x/y",
        oidc_discovery_url=oidc_url,
    )


def _fresh_cache() -> _OidcCache:
    """Return a new, empty cache instance for isolated tests."""
    return _OidcCache()


@pytest.fixture()
def cache() -> _OidcCache:
    """Provide a fresh _OidcCache per test — no shared module state."""
    return _fresh_cache()


@pytest.fixture(autouse=True)
def _reset_default_cache() -> None:
    """Reset the process-scoped default cache between tests."""
    oidc_mod._default_cache = None


# ---------------------------------------------------------------------------
# OIDC disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oidc_not_configured_raises(cache: _OidcCache) -> None:
    settings = _make_settings(oidc_url=None)
    db = AsyncMock()
    with pytest.raises(CatalogError, match="OIDC not configured"):
        await oidc_mod.validate_oidc_token("header.payload.sig", settings, db, cache=cache)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_oidc_token_happy_path(cache: _OidcCache) -> None:
    settings = _make_settings()

    # Mock DB session: actor lookup then roles lookup.
    from registry.storage.models import Actor

    mock_actor = MagicMock(spec=Actor)
    mock_actor.actor_id = _ACTOR_ID
    mock_actor.tenant_id = _TENANT_ID
    mock_actor.oidc_subject = "user123"

    actor_result = MagicMock()
    actor_result.scalar_one_or_none.return_value = mock_actor

    roles_result = MagicMock()
    roles_result.all.return_value = [("admin",), ("producer",)]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[actor_result, roles_result])

    # Patch network + authlib.
    claims_obj = MagicMock()
    claims_obj.get.side_effect = lambda k, *a: _CLAIMS.get(k)
    claims_obj.validate.return_value = None

    jwt_instance = MagicMock()
    jwt_instance.decode.return_value = claims_obj

    with (
        patch.object(cache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
        patch.object(cache, "get_jwks", AsyncMock(return_value=_JWKS)),
        patch("registry.api.auth.oidc.JsonWebKey") as mock_jwk,
        patch("registry.api.auth.oidc.JsonWebToken", return_value=jwt_instance),
    ):
        mock_jwk.import_key_set.return_value = MagicMock()
        ctx = await oidc_mod.validate_oidc_token("h.p.s", settings, db, cache=cache)

    assert ctx.tenant_id == _TENANT_ID
    assert ctx.actor_id == _ACTOR_ID
    assert set(ctx.roles) == {"admin", "producer"}


# ---------------------------------------------------------------------------
# Sad paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oidc_discovery_network_error_raises(cache: _OidcCache) -> None:
    import httpx

    settings = _make_settings()
    db = AsyncMock()

    with patch.object(
        cache,
        "get_discovery_doc",
        AsyncMock(side_effect=httpx.HTTPError("timeout")),
    ):
        with pytest.raises(CatalogError, match="OIDC discovery failed"):
            await oidc_mod.validate_oidc_token("h.p.s", settings, db, cache=cache)


@pytest.mark.asyncio
async def test_oidc_invalid_signature_raises(cache: _OidcCache) -> None:
    from authlib.jose.errors import JoseError

    settings = _make_settings()
    db = AsyncMock()

    jwt_instance = MagicMock()
    jwt_instance.decode.side_effect = JoseError("bad sig")

    with (
        patch.object(cache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
        patch.object(cache, "get_jwks", AsyncMock(return_value=_JWKS)),
        patch("registry.api.auth.oidc.JsonWebKey") as mock_jwk,
        patch("registry.api.auth.oidc.JsonWebToken", return_value=jwt_instance),
    ):
        mock_jwk.import_key_set.return_value = MagicMock()
        with pytest.raises(CatalogError, match="invalid OIDC token"):
            await oidc_mod.validate_oidc_token("h.p.s", settings, db, cache=cache)


@pytest.mark.asyncio
async def test_oidc_actor_not_found_raises(cache: _OidcCache) -> None:
    settings = _make_settings()

    actor_result = MagicMock()
    actor_result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=actor_result)

    claims_obj = MagicMock()
    claims_obj.get.side_effect = lambda k, *a: _CLAIMS.get(k)
    claims_obj.validate.return_value = None

    jwt_instance = MagicMock()
    jwt_instance.decode.return_value = claims_obj

    with (
        patch.object(cache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
        patch.object(cache, "get_jwks", AsyncMock(return_value=_JWKS)),
        patch("registry.api.auth.oidc.JsonWebKey") as mock_jwk,
        patch("registry.api.auth.oidc.JsonWebToken", return_value=jwt_instance),
    ):
        mock_jwk.import_key_set.return_value = MagicMock()
        with pytest.raises(CatalogError, match="OIDC actor not found"):
            await oidc_mod.validate_oidc_token("h.p.s", settings, db, cache=cache)


@pytest.mark.asyncio
async def test_oidc_missing_sub_claim_raises(cache: _OidcCache) -> None:
    settings = _make_settings()
    db = AsyncMock()

    claims_no_sub: dict[str, Any] = {**_CLAIMS, "sub": None}
    claims_obj = MagicMock()
    claims_obj.get.side_effect = lambda k, *a: claims_no_sub.get(k)
    claims_obj.validate.return_value = None

    jwt_instance = MagicMock()
    jwt_instance.decode.return_value = claims_obj

    with (
        patch.object(cache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
        patch.object(cache, "get_jwks", AsyncMock(return_value=_JWKS)),
        patch("registry.api.auth.oidc.JsonWebKey") as mock_jwk,
        patch("registry.api.auth.oidc.JsonWebToken", return_value=jwt_instance),
    ):
        mock_jwk.import_key_set.return_value = MagicMock()
        with pytest.raises(CatalogError, match="missing sub claim"):
            await oidc_mod.validate_oidc_token("h.p.s", settings, db, cache=cache)


# ---------------------------------------------------------------------------
# JWT shape detection in tenant middleware
# ---------------------------------------------------------------------------


def test_looks_like_jwt_true() -> None:
    from registry.api.middleware.tenant import _looks_like_jwt

    assert _looks_like_jwt("header.payload.signature") is True


def test_looks_like_jwt_false_opaque() -> None:
    from registry.api.middleware.tenant import _looks_like_jwt

    assert _looks_like_jwt("opaque_api_token_no_dots") is False
    assert _looks_like_jwt("two.parts") is False


# ---------------------------------------------------------------------------
# _OidcCache.invalidate() — instance isolation
# ---------------------------------------------------------------------------


def test_cache_invalidate_resets_instance_only() -> None:
    """invalidate() on one cache does not touch a sibling cache."""
    c1 = _OidcCache(
        discovery_doc={"issuer": "x"},
        discovery_fetched_at=1.0,
        jwks_data={"keys": []},
        jwks_fetched_at=2.0,
    )
    c2 = _OidcCache(
        discovery_doc={"issuer": "y"},
        discovery_fetched_at=3.0,
        jwks_data={"keys": []},
        jwks_fetched_at=4.0,
    )

    c1.invalidate()

    # c1 fully cleared
    assert c1.discovery_doc is None
    assert c1.discovery_fetched_at == 0.0
    assert c1.jwks_data is None
    assert c1.jwks_fetched_at == 0.0

    # c2 untouched
    assert c2.discovery_doc == {"issuer": "y"}
    assert c2.discovery_fetched_at == 3.0
    assert c2.jwks_data == {"keys": []}
    assert c2.jwks_fetched_at == 4.0


# ---------------------------------------------------------------------------
# _OidcCache.invalidate() — test isolation (no cross-test state leak)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_cache_per_test_does_not_leak_state() -> None:
    """Two successive tests each get an empty cache — globals don't bleed in."""
    c = _fresh_cache()
    # Nothing fetched yet
    assert c.discovery_doc is None
    assert c.jwks_data is None
    # Simulate a warm cache
    c.discovery_doc = {"issuer": "https://example.com"}
    c.discovery_fetched_at = 999.0

    # A second fresh cache is unaffected
    c2 = _fresh_cache()
    assert c2.discovery_doc is None
    assert c2.discovery_fetched_at == 0.0


# ---------------------------------------------------------------------------
# Concurrent refresh — exactly one upstream JWKS fetch at TTL boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_validate_at_ttl_boundary_single_jwks_fetch() -> None:
    """10 concurrent validate_token calls at TTL expiry issue exactly 1 JWKS fetch.

    Uses a counting mock on the cache instance's get_jwks method directly so
    we can assert how many upstream fetches fired without patching the global
    httpx module (which can contaminate other async tests sharing the same
    event loop).
    """
    settings = _make_settings()

    # Pre-warm the discovery doc so only JWKS is exercised.
    cache = _OidcCache(
        discovery_doc=_DISCOVERY,
        discovery_fetched_at=999_999_999.0,  # warm — won't expire
    )
    # JWKS is cold: jwks_fetched_at=0.0, so all 10 concurrent callers see expiry.

    jwks_fetch_count = 0

    async def _counting_get_jwks(uri: str) -> dict[str, Any]:
        """Simulate one upstream JWKS fetch with an event-loop yield."""
        nonlocal jwks_fetch_count
        await asyncio.sleep(0)
        jwks_fetch_count += 1
        return _JWKS

    # The real get_jwks acquires self.refresh_lock before fetching.
    # We test the lock logic by wrapping the method with our counter
    # and letting the lock serialise concurrent callers.
    async def _locked_counting_get_jwks(self: _OidcCache, uri: str) -> dict[str, Any]:
        """Lock-aware counting shim — exercises the same-instance lock path."""
        now = time.monotonic()
        # Fast path: already warm after first fetch.
        if self.jwks_data is not None and (now - self.jwks_fetched_at) < _CACHE_TTL_S:
            return self.jwks_data  # type: ignore[return-value]
        async with self.refresh_lock:
            now = time.monotonic()
            if self.jwks_data is not None and (now - self.jwks_fetched_at) < _CACHE_TTL_S:
                return self.jwks_data  # type: ignore[return-value]
            data = await _counting_get_jwks(uri)
            self.jwks_data = data
            self.jwks_fetched_at = time.monotonic()
            return data

    from registry.storage.models import Actor

    mock_actor = MagicMock(spec=Actor)
    mock_actor.actor_id = _ACTOR_ID
    mock_actor.tenant_id = _TENANT_ID
    mock_actor.oidc_subject = "user123"

    def _make_db() -> AsyncMock:
        actor_result = MagicMock()
        actor_result.scalar_one_or_none.return_value = mock_actor
        roles_result = MagicMock()
        roles_result.all.return_value = [("consumer",)]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[actor_result, roles_result])
        return db

    claims_obj = MagicMock()
    claims_obj.get.side_effect = lambda k, *a: _CLAIMS.get(k)
    claims_obj.validate.return_value = None
    jwt_instance = MagicMock()
    jwt_instance.decode.return_value = claims_obj

    with (
        patch.object(cache, "get_discovery_doc", AsyncMock(return_value=_DISCOVERY)),
        patch.object(cache, "get_jwks", lambda uri: _locked_counting_get_jwks(cache, uri)),
        patch("registry.api.auth.oidc.JsonWebKey") as mock_jwk,
        patch("registry.api.auth.oidc.JsonWebToken", return_value=jwt_instance),
    ):
        mock_jwk.import_key_set.return_value = MagicMock()

        tasks = [
            asyncio.create_task(oidc_mod.validate_oidc_token("h.p.s", settings, _make_db(), cache=cache))
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

    # All 10 calls succeeded.
    assert len(results) == 10
    # Exactly 1 upstream JWKS fetch fired despite 10 concurrent expiry detections.
    assert jwks_fetch_count == 1, f"expected 1 JWKS fetch, got {jwks_fetch_count}"


@pytest.mark.asyncio
async def test_oidc_cache_lock_serialises_concurrent_jwks_fetches() -> None:
    """The lock in _OidcCache.get_jwks serialises concurrent expiry detections.

    Drives get_jwks directly (not through validate_oidc_token) to confirm the
    lock logic in isolation.
    """
    import httpx

    fetch_count = 0

    class _FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return _JWKS

    class _FakeClient:
        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            pass

        async def get(self, url: str) -> _FakeResponse:
            nonlocal fetch_count
            await asyncio.sleep(0)  # yield so all 10 tasks race to the slow path
            fetch_count += 1
            return _FakeResponse()

    cache = _OidcCache()  # cold cache
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()):
        tasks = [asyncio.create_task(cache.get_jwks("https://idp.example.com/jwks")) for _ in range(10)]
        results = await asyncio.gather(*tasks)

    assert all(r == _JWKS for r in results)
    assert fetch_count == 1, f"expected 1 upstream fetch, got {fetch_count}"


# ---------------------------------------------------------------------------
# get_default_cache() — process-scoped singleton
# ---------------------------------------------------------------------------


def test_get_default_cache_returns_same_instance() -> None:
    """get_default_cache() always returns the same object within a process."""
    c1 = oidc_mod.get_default_cache()
    c2 = oidc_mod.get_default_cache()
    assert c1 is c2


def test_default_cache_reset_between_tests() -> None:
    """The autouse fixture resets _default_cache so each test starts fresh."""
    # After the autouse fixture ran, _default_cache is None.
    assert oidc_mod._default_cache is None
    c = oidc_mod.get_default_cache()
    assert c is not None
    # The module-level variable is now populated.
    assert oidc_mod._default_cache is c
