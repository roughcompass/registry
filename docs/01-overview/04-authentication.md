# Authentication

How the registry decides **who is making this request**. Every authenticated endpoint receives an `Authorization: Bearer <JWT>` header; this doc covers the steps that turn that JWT into a verified identity. Tenant scope and role grants are derived separately and live in [Authorization](05-authorization.md).

---

## Pipeline

```
Authorization: Bearer <JWT>
        │
        ▼
Bearer extraction          → 401 if missing or malformed
        │
        ▼
JWT signature validation   → 401 if signature, expiry, or claim shape fails
  (against OIDC discovery doc + JWKS)
        │
        ▼
Issuer / audience / TTL    → 401 if `iss` not allowlisted,
  bound checks                       `aud` not allowlisted,
                                     `exp - iat` exceeds the registry ceiling
        │
        ▼
Validated claim set        → handed to the claim resolver (authorization)
```

JWTs are the only credential the registry accepts. There is no opaque-bearer or in-DB token table. Validation runs once per request; the OIDC discovery doc and JWKS are cached in-process.

---

## OIDC discovery

The registry is provider-agnostic. Point `OIDC_DISCOVERY_URL` at any OpenID-Connect-compliant provider's `.well-known/openid-configuration` and the validator reads `issuer`, `jwks_uri`, and `id_token_signing_alg_values_supported` from the document. Okta, Azure AD, Auth0, Keycloak, Google Workspace, Ory Hydra, AWS Cognito, and the local mock IDP all work without code changes.

JWKS is fetched lazily on first use and cached. Key rotation at the IdP is picked up on cache expiry; the registry does not require a restart.

---

## Claim contract

Every accepted token must satisfy:

| Claim | Constraint | Configured by |
|---|---|---|
| `iss` | Must appear in the issuer allowlist. | `OIDC_ISSUER_ALLOWLIST` (comma-separated) |
| `aud` | At least one value must appear in the resource-URI allowlist. | `RESOURCE_URI_ALLOWLIST` (comma-separated) |
| `azp` / `client_id` | If the allowlist is non-empty, the value must appear there. Empty = check skipped. | `OIDC_CLIENT_ID_ALLOWLIST` |
| `exp` | Token must not be past expiry. | (standard) |
| `iat` | Token must carry `iat`; `exp - iat` ≤ `OIDC_MAX_TOKEN_TTL_SECONDS`. | `OIDC_MAX_TOKEN_TTL_SECONDS` (default 900) |
| `sub` | Identifies the calling principal. Passed forward to the claim resolver. | (no validator-side constraint) |

The TTL bound is defense-in-depth: even an IdP that's mis-configured to issue long-lived tokens will be capped by the registry. Production deployments should keep this at 900 (15 min) or lower; local dev relaxes it because the bundled mock IDP signs 3600s tokens by default.

Failure modes all map to **HTTP 401** with body `{"errors":[{"code":"unauthenticated","message":"authentication required"}]}`. The specific reason (`iss-not-allowed`, `aud-not-allowed`, `azp-not-allowed`, `token-ttl-exceeded`, `missing-iat`, `missing-identity-claim`) is logged but not surfaced to the caller — failure modes are intentionally opaque to anyone holding an invalid token.

---

## Local development

Three commands take a developer from a fresh clone to a JWT that authenticates against `/v1/whoami`:

```bash
docker compose up -d                              # postgres, api, mock OIDC, mock entitlement service
make migrate                                      # apply alembic migrations
make dev-token                                    # seed tenant + actor + mock-IDP client + canned entitlements
```

`make dev-token` is idempotent — re-running reuses the existing tenant + actor + client. It writes to `.env.dev`:

```
DEV_TENANT_SLUG=111205
DEV_TENANT_ID=<uuid>
DEV_ACTOR_ID=<uuid>
DEV_USER_ID=dev-admin
CLIENT_ID=registry-dev
CLIENT_SECRET=dev-secret
```

`.env.dev` is git-ignored. Pass `--env-file <path>` to write somewhere else, or `--skip-mock-seed` if the mock services aren't running.

### Fetching a JWT

The compose stack ships `mock-oauth2-server` (`ghcr.io/navikt/mock-oauth2-server`) on host port 8090. The convenience target reads `.env.dev`, exchanges the seeded credentials, and prints the JWT to stdout:

```bash
export TOKEN=$(make dev-jwt)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/whoami
```

Or inlined for one-shot calls:

```bash
curl -H "Authorization: Bearer $(make dev-jwt)" http://localhost:8000/v1/whoami
```

Expected: 200 with the resolved tenant + actor + role. JWT TTL is 3600s — re-run `make dev-jwt` to refresh.

Under the hood, the target POSTs to `http://localhost:8090/default/token` with `grant_type=client_credentials` and `scope=registry`. The `scope` value is load-bearing — mock-oauth2-server copies it into the `aud` claim, and `registry` is what the api's `RESOURCE_URI_ALLOWLIST` accepts. The equivalent raw curl:

```bash
source .env.dev
curl -s -X POST http://localhost:8090/default/token \
  -d grant_type=client_credentials \
  -d client_id=$CLIENT_ID \
  -d client_secret=$CLIENT_SECRET \
  -d scope=registry
```

If `make dev-jwt` returns 401 on whoami, check `docker compose logs --tail 50 api` for the rejection reason.

### Swagger UI

[`http://localhost:8000/docs`](http://localhost:8000/docs) → **Authorize** → paste `$TOKEN` into **bearerAuth**. The OIDC tab (oauth-code flow) is only offered when `OIDC_DISCOVERY_URL` is set — which it is in compose, so both tabs work.

---

## Production

Set `OIDC_DISCOVERY_URL` to your IdP's discovery document and tune the allowlists to match what your IdP issues. Nothing in the codebase is provider-specific; the discovery doc is the only abstraction the registry knows.

A typical production env block:

```
OIDC_DISCOVERY_URL=https://idp.example.com/.well-known/openid-configuration
OIDC_ISSUER_ALLOWLIST=https://idp.example.com
RESOURCE_URI_ALLOWLIST=https://registry.example.com
OIDC_CLIENT_ID_ALLOWLIST=registry-prod,registry-ci
OIDC_MAX_TOKEN_TTL_SECONDS=900
```

The deployment's secret store (Kubernetes `Secret`, ECS task-definition `secrets`, systemd `EnvironmentFile`, Vault, …) supplies these values at boot.

---

## What's not in this doc

- **Tenant scope and role grants.** Resolving the validated `sub` to a tenant + role set is authorization — see [Authorization](05-authorization.md).
- **Per-provider setup tutorials.** The discovery URL is the abstraction layer between this product and any specific IdP — point your IdP's docs at "how do I expose an OIDC discovery document?" and you have what you need.
- **Actor provisioning flows.** Actors are JIT-materialized on first entitlement; bulk-import / SCIM / out-of-band provisioning is a per-deployment concern.
- **SAML.** Not supported. OIDC covers every modern IdP.
