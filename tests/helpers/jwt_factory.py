"""In-process JWT signer for unit tests.

Tests need to construct JWTs that the registry's OIDC validator accepts —
without standing up a full IDP container. This helper signs tokens with a
committed test RSA key and exposes the matching public key as a JWKS so
tests can wire the validator's key store to trust it.

The RSA key in this module is intentionally public. It must NEVER be used
in production. The matching public key is bundled below so test code can
register it as the JWKS for `validate_oidc_token`.

Tokens default to a 15-minute lifetime starting at `int(time.time())`.
Override `iat` and `exp` to test boundary conditions (expired tokens,
clock skew, etc.).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from authlib.jose import JsonWebKey, JsonWebToken  # type: ignore[import-untyped]

# Test issuer URL. Tests should set OIDC_ISSUER_ALLOWLIST to include this
# value (or use the test-only allowlist override) so tokens round-trip
# through validate_oidc_token.
TEST_ISSUER = "https://test-idp.local/realms/registry-tests"

# Default test audience — matches the conventional registry resource URI
# used in tests. Override per-test if exercising aud-mismatch paths.
TEST_AUDIENCE = "registry"

# Default token lifetime in seconds (matches the default
# OIDC_MAX_TOKEN_TTL_SECONDS bound so tokens are always within policy).
DEFAULT_TTL_SECONDS = 900

# Committed test RSA private key — 2048-bit, generated once, public on
# purpose. Used only by tests in this repository. NEVER use in production.
_TEST_PRIVATE_PEM = b"""-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC1w/anA2m/ugRJ
2OcPWynxDGmi5mjKy64K5SQ8yRz4Vbb/+FD01UI4qR2d3VzX1krEzU1izjbj02PL
1G3VEUT99eZTC4U9y+lp5HuhuBZ+9uZOkFTfzVW0TAhjcZBD6BBeJFeSWdB4SSAf
GDT3fawqyH3JJjTeMRCDX0h1QB5Cawzv32en4JG01NH/HOdJhlBPp4sVl3ydgh4P
QyI0OFf/5JkX2sby4lCf86YdQ+lPaJJBwAVJyKSRf4OdVSFS72JugWq76l9HG50W
1yOdfMns4WBJAJ5m3xeGz6Q+kG+SxtxMo3pWGUxokcdroymn90a86gjjcDLgjX6p
9ky8D6RNAgMBAAECggEANLsTgLMsSAtKeDP9IEbVvZDYyoHmb8K0DIQaRaogheiz
7MFYlxaRHgftyCDycMlBqqNWqm3hnalzP6wyasgWSEjAl2H1tw5Dek1nEmzp1c6B
1NPpU33puaL/If5NmG2n5e/MGfCFWof4Uhz/LLdgLY85cpPrbXQ1cq8/QYim3qRa
9ppl+etj7YWJAefWqxQ67dGveGuIKKwGWPESEcu7vvBN5+qV9MswXX/OwIC1cQBm
5zGkPuhWCB6jComvuSTZ5/HbQ5zjP9PO8v5b8VyHIEju6JbW5Md5R85dUPiKdc4C
AibF1IwlG7poxDPY305dK2dQONKOaDx5FiFG6sKo7wKBgQDuQb1q5ZeTkqYkuxW/
PKoW6Z068oAA+ftoCmWnD6ilC1zZT0ik51b8AG3Y5oCtn00YZ/j1Dk02G63RonFU
M7Cfb75KCJ9BYcOstYqBa/PBTXvS89WQPe36FTSo6RyOyn3lYbbbK6mNuDuc4jf2
H5syoZFRLWO4XPYPOI50nblF1wKBgQDDTT3iqOmW+VSezG95ZHGORgGEQFLm9Rmz
E8/ttA8CJyoAnRfQQS+o7jxXQ7yMcnXyipchIwP1erfSBiL7P4M6y2I2OyUNtwmF
2Vq0hdvxiisoxTSmNAFHWyMri/yzqe+MEL5DOT+Gdh8FYsXu2m5suNMcd3JwGFUV
QsyKT77aewKBgCIrbXYCPW3dr1Q/PIwzsBUfJfyJQNBjCapPK2r9NOuOqJ9F3p4/
y1rS2O4tiLDd0tm4N501kt86swAIswYnb6I+DWVivSxMUBrZ4mZTTB8h9Ks5axyH
tTSTi/zZic30vn+CNw5RwbxgerQyQWJcAA8P2t5wiweq1WMzckLJSAP7AoGBAKj1
VPmW+ebDsyJiaHoTnG3iQIOihlYKav5SwIq7QFSzfxHi1ewzyMCTwh4YmrDCgSmg
Hljriww+63JGHtNPwf8GXuPdzRONay6huGf+eiX/S5FM8lxrF0QdI1MUGz1vYa7B
+Wf8yelQnUuyhNw7mlZymyjAaX9yfYEUNhHeJZWrAoGBALI0rEsy/BcYUjgiJpZR
XkoN/P7/ajoTnSLY8KotOOJBDK09PJNaEKbJ6oqBMHUqe5ZXdEEpL9pGgYCmNt7O
QRA9FWwbY6fGMTE88BGpykgiw+UMw30ULWyUDygzc+CkGtk4EWy9h3DVHeMNce0g
Peyic7CWHw4dSpDyft26NmHj
-----END PRIVATE KEY-----
"""

# Stable kid for the test key. validate_oidc_token matches incoming tokens
# against the JWKS by `kid`; using a fixed value lets tests construct the
# JWKS once and reuse it.
TEST_KEY_ID = "test-key-1"

# Cached signing key — JsonWebKey.import_key materializes once at import.
_signing_key = JsonWebKey.import_key(_TEST_PRIVATE_PEM, {"kty": "RSA", "kid": TEST_KEY_ID})

_jwt = JsonWebToken(["RS256"])


def make_jwt(
    *,
    sub: str = "test-user",
    iss: str = TEST_ISSUER,
    aud: str | list[str] = TEST_AUDIENCE,
    iat: int | None = None,
    exp: int | None = None,
    jti: str | None = None,
    winaccountname: str | None = None,
    name: str | None = None,
    azp: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Sign a test JWT and return the compact serialization.

    All parameters are keyword-only to keep call sites self-documenting and
    to make boundary-condition tests (expired token, missing iat, etc.) read
    naturally.
    """
    now = int(time.time())
    issued_at = iat if iat is not None else now
    expires_at = exp if exp is not None else issued_at + DEFAULT_TTL_SECONDS

    payload: dict[str, Any] = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": issued_at,
        "exp": expires_at,
    }
    if jti is not None:
        payload["jti"] = jti
    else:
        payload["jti"] = str(uuid.uuid4())
    if winaccountname is not None:
        payload["winaccountname"] = winaccountname
    if name is not None:
        payload["name"] = name
    if azp is not None:
        payload["azp"] = azp
    if extra_claims:
        payload.update(extra_claims)

    header = {"alg": "RS256", "typ": "JWT", "kid": TEST_KEY_ID}
    token = _jwt.encode(header, payload, _signing_key)
    if isinstance(token, bytes):
        return token.decode("ascii")
    return token


def get_test_jwks() -> dict[str, Any]:
    """Return the JWKS containing the public half of the test signing key.

    Wire this into `validate_oidc_token`'s key resolver (or seed an
    `_OidcCache` with it) so signature verification against test-signed
    tokens succeeds.
    """
    public_jwk = _signing_key.as_dict(is_private=False)
    public_jwk["kid"] = TEST_KEY_ID
    public_jwk["alg"] = "RS256"
    public_jwk["use"] = "sig"
    return {"keys": [public_jwk]}


__all__ = [
    "TEST_ISSUER",
    "TEST_AUDIENCE",
    "TEST_KEY_ID",
    "DEFAULT_TTL_SECONDS",
    "make_jwt",
    "get_test_jwks",
]
