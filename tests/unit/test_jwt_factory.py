"""Unit tests for the in-process JWT signer used by other tests.

Verifies round-trip claim preservation, default lifetime, optional claim
emission, signature verifiability against the bundled JWKS, and rejection
of expired tokens by the same key set.
"""

from __future__ import annotations

import time

import pytest
from authlib.jose import JsonWebKey, JsonWebToken  # type: ignore[import-untyped]
from authlib.jose.errors import (  # type: ignore[import-untyped]
    ExpiredTokenError,
)

from tests.helpers.jwt_factory import (
    DEFAULT_TTL_SECONDS,
    TEST_AUDIENCE,
    TEST_ISSUER,
    TEST_KEY_ID,
    get_test_jwks,
    make_jwt,
)


def _decode(token: str) -> dict:
    """Verify the token signature against the bundled JWKS and return claims."""
    jwks = get_test_jwks()
    key_set = JsonWebKey.import_key_set(jwks)
    jwt = JsonWebToken(["RS256"])
    return jwt.decode(token, key_set)


class TestRoundTrip:
    def test_default_claims(self):
        before = int(time.time())
        token = make_jwt()
        after = int(time.time())

        claims = _decode(token)
        assert claims["sub"] == "test-user"
        assert claims["iss"] == TEST_ISSUER
        assert claims["aud"] == TEST_AUDIENCE
        assert claims["jti"]  # uuid auto-populated
        assert before <= claims["iat"] <= after
        assert claims["exp"] == claims["iat"] + DEFAULT_TTL_SECONDS

    def test_winaccountname_round_trips(self):
        token = make_jwt(winaccountname="DOMAIN\\jdoe")
        claims = _decode(token)
        assert claims["winaccountname"] == "DOMAIN\\jdoe"

    def test_name_claim_round_trips(self):
        token = make_jwt(name="Jane Doe")
        claims = _decode(token)
        assert claims["name"] == "Jane Doe"

    def test_azp_round_trips(self):
        token = make_jwt(azp="some-service-client-id")
        claims = _decode(token)
        assert claims["azp"] == "some-service-client-id"

    def test_extra_claims_merge(self):
        token = make_jwt(extra_claims={"groups": ["a", "b"], "custom": 42})
        claims = _decode(token)
        assert claims["groups"] == ["a", "b"]
        assert claims["custom"] == 42

    def test_explicit_jti_preserved(self):
        token = make_jwt(jti="fixed-jti-for-test")
        claims = _decode(token)
        assert claims["jti"] == "fixed-jti-for-test"

    def test_audience_list(self):
        token = make_jwt(aud=["registry", "other-service"])
        claims = _decode(token)
        assert claims["aud"] == ["registry", "other-service"]


class TestExpirationBoundary:
    def test_expired_token_rejected_by_validation(self):
        """A token with exp in the past must fail validation when the
        decoder enforces standard claim checks. authlib's validate() is
        what the registry's validator calls; this test confirms our test
        helper produces tokens whose expiry is honored by that path."""
        past = int(time.time()) - 7200
        token = make_jwt(iat=past, exp=past + 60)  # expired ~2h ago

        claims = _decode(token)  # decode itself succeeds (signature valid)
        with pytest.raises(ExpiredTokenError):
            claims.validate()

    def test_long_lived_token_decodes_but_violates_ttl_bound(self):
        """A token with a longer-than-policy lifetime still decodes; the
        registry's TTL bound check is a separate concern (see oidc validator
        and OIDC_MAX_TOKEN_TTL_SECONDS). This test just confirms the helper
        does not impose its own ceiling."""
        now = int(time.time())
        token = make_jwt(iat=now, exp=now + 86400)  # 24h
        claims = _decode(token)
        assert claims["exp"] - claims["iat"] == 86400


class TestJwksShape:
    def test_jwks_contains_test_key(self):
        jwks = get_test_jwks()
        assert "keys" in jwks
        assert len(jwks["keys"]) == 1
        key = jwks["keys"][0]
        assert key["kid"] == TEST_KEY_ID
        assert key["alg"] == "RS256"
        assert key["use"] == "sig"
        assert key["kty"] == "RSA"

    def test_jwks_does_not_leak_private_components(self):
        """Defense against accidentally including the private exponent."""
        jwks = get_test_jwks()
        key = jwks["keys"][0]
        # RSA private fields that must not appear in a published JWKS.
        for private_field in ("d", "p", "q", "dp", "dq", "qi"):
            assert private_field not in key, (
                f"Test JWKS leaked private RSA field {private_field!r}; "
                "the bundled key would be unusable for verification but "
                "leaking the private side defeats the purpose of having a JWKS"
            )
