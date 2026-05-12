# Authentication

The catalog accepts credentials via three lanes. Pick whichever fits the
audience — the product itself makes no assumption about which lane is in
use. All three resolve to the same `TenantContext` inside the app.

| Lane | Audience | Mechanism | Configured by |
|---|---|---|---|
| 1. Identity provider | Humans (enterprise SSO) | OIDC JWT, validated against an IdP's discovery document | `OIDC_DISCOVERY_URL` |
| 2. API tokens | CI, service accounts, break-glass | Opaque bearer tokens stored as SHA-256 hashes in `api_tokens` | `scripts/mint_token.py` |
| 3. Dev bootstrap | Local development, first 5 minutes | Same as API tokens, plus an idempotent one-shot seeder | `make dev-token` |

Both production lanes (1 and 2) can be active simultaneously. The
middleware tries OIDC first when the incoming token looks like a JWT and
OIDC is configured, then falls through to API-token validation on any
failure. A deployment can therefore give humans SSO and still mint
bearer tokens for CI and break-glass — without changing code.

---

## Looking things up

Once you've authenticated, two ergonomic shortcuts:

**Address by name.** Every endpoint that takes a `{entity_id}` path
parameter also accepts a slug-form name. The catalog mints capability
records under names like `salt-design-system`; a developer or copilot
that knows the name can call the endpoint directly without a search
hop:

```bash
curl -H 'Authorization: Bearer <token>' http://localhost:8000/v1/capabilities/salt-design-system
```

Slugs are lowercase + digits + hyphens, 1-200 chars, alphanumeric
start/end (no leading/trailing/consecutive hyphens). The format is
enforced at write time; existing rows are not retroactively validated.

**Composite retrieval via `?include=`.** Adding `?include=...` to
`GET /v1/capabilities/{handle}` returns the capability plus a bounded
set of related sub-resources in one response. Known values:

| include | What it adds |
|---|---|
| `components` | Outgoing `composes` edges expanded into full entity records (with their own attributes). |
| `depends_on` | Outgoing `depends_on` edges expanded into full entity records. |
| `external_ids` | `entity_external_ids` mappings (npm package name, GitHub repo slug, …). |
| `interface` | Latest registered interface surface (JSON Schema / TypeScript / OpenAPI 3.x). |

Each expansion is capped at 50 items; truncation is signalled with
`truncated: true` plus a `next` URL pointing at the dedicated endpoint
for the full set.

```bash
curl -H 'Authorization: Bearer <token>' \
     'http://localhost:8000/v1/capabilities/salt-design-system?include=components,external_ids'
```

**Resolve a capability from an upstream identifier.** If a copilot has
your `package.json` and sees `@salt-ds/core`, it can find Salt in the
catalog directly via the external-ID registry:

```bash
curl -H 'Authorization: Bearer <token>' \
     'http://localhost:8000/v1/entities?external_system=npm&external_id=@salt-ds/core'
```

Or via the MCP tool `lookup_by_external_id(external_system, external_id)`.

---

## Local development

```bash
docker compose up -d         # if you haven't already
make dev-token
```

That's it. No `DATABASE_URL` to set, no UUIDs to track. The target:

1. Inserts a tenant with slug `dev` if it doesn't already exist.
2. Seeds the four named roles (`consumer`, `producer`, `admin`,
   `auditor`) for that tenant.
3. Inserts a `dev-admin` actor under the dev tenant.
4. Grants the actor all four roles.
5. Mints a fresh API token and prints the plaintext.

The script defaults `DATABASE_URL` to the docker-compose Postgres
(`postgresql+asyncpg://postgres:password@localhost:5544/registry`) when
the env var is unset; export your own value to point at a different
database.

`make dev-token` is idempotent — a second invocation reuses the same
tenant + actor and mints a new token (so you can rotate by re-running).
Pass `TOKEN_OUT=.env.dev` to persist the token to a file:

```bash
make dev-token TOKEN_OUT=.env.dev
# wrote .env.dev (REGISTRY_DEV_TOKEN=...)
```

`.env.dev` is git-ignored.

Paste the printed token into Swagger's **Authorize** button at
[`/docs`](http://localhost:8000/docs) — Try It Out on any endpoint
sends the bearer header automatically thereafter.

To make the endpoints return something interesting, follow up with:

```bash
make dev-seed
```

That seeds the closed-vocabulary values the catalog expects plus two
demo capabilities (Salt Design System + an enterprise user-preference
service). Without it, `POST /v1/capabilities` rejects with
`unknown vocabulary value` and `GET /v1/capabilities` returns an empty
list. `dev-seed` is idempotent — re-runs produce the same `entity_id`s.

---

## Production with OIDC

Set one environment variable:

```
OIDC_DISCOVERY_URL=https://<your-idp>/.well-known/openid-configuration
```

Any OpenID Connect provider works — Okta, Azure AD, Auth0, Keycloak,
Google Workspace, Ory Hydra, Cognito, your own provider — because the
discovery document advertises the JWKS URI, issuer, and supported
algorithms. The catalog reads them at runtime; nothing in the codebase
is provider-specific.

Required JWT claims:

| Claim | Purpose |
|---|---|
| `iss` | Must match the discovery doc's `issuer`. |
| `exp` | Standard expiry. Tokens past `exp` are rejected. |
| `sub` | Resolved against `actors.oidc_subject` to identify the caller. |
| `tenant_id` or `tid` | UUID of the calling tenant. |

The actor must be pre-provisioned. Tenant admins create actors via the
admin API (separate from this doc) and set `oidc_subject` to the value
the IdP issues for that user — typically an email or stable user ID.

---

## Production without an IdP (or service-to-service)

```bash
python scripts/mint_token.py \
    --tenant-id <tenant-uuid> \
    --actor-id <actor-uuid> \
    --roles producer --roles consumer \
    --description 'ci deploy token' \
    --expires-days 365
```

Stores the SHA-256 hash in `api_tokens`; prints the plaintext exactly
once. The plaintext is never persisted by the catalog — wherever your
platform stores secrets is where you put it:

- Kubernetes: `Secret` mounted as env var
- AWS ECS: task-definition `secrets` from Secrets Manager / Parameter Store
- systemd: `EnvironmentFile=/etc/registry/token` (chmod 600, root-owned)
- HashiCorp Vault, GCP Secret Manager, Azure Key Vault: pull at boot

This is the canonical path for CI runners and service-to-service callers
that don't have a human identity to authenticate.

---

## Coexistence

```
Authorization: Bearer <token>
              │
              ▼
        looks like a JWT?
              │
       yes ───┴─── no
        │           │
        ▼           ▼
  OIDC configured?  bearer
        │              │
   yes ─┴─ no          │
    │     │            │
    ▼     ▼            ▼
   OIDC  bearer       resolve api_tokens
    │                  │
    │  on CatalogError │
    └──── fall through ┘
              │
              ▼
        TenantContext
```

The fallthrough is intentional: opaque tokens that happen to contain
dots (unlikely with `secrets.token_urlsafe(32)`, but allowed) still
authenticate against `api_tokens`.

---

## Swagger UI

Open `/docs` (default at `http://localhost:8000/docs`) and click
**Authorize**. You get up to two tabs:

- **bearerAuth** — paste a token from `make dev-token` or
  `scripts/mint_token.py`.
- **oidcAuth** — appears only when `OIDC_DISCOVERY_URL` is set. Swagger
  reads the discovery doc, derives the OAuth flow, and walks you through
  the IdP's authorisation code dance.

After authorising, the **Try it out** button on every endpoint signs
requests automatically.

---

## What's deliberately not in this doc

- **Per-provider setup tutorials.** The discovery URL is the abstraction
  layer between this product and any specific IdP — point your IdP's
  docs at "how do I expose an OIDC discovery document?" and you have
  what you need.
- **Actor provisioning flows.** Tenant admins create actors via the
  admin API. The mechanism (manual, SCIM, batch import) is a per-
  deployment concern.
- **SAML.** Not supported. OIDC covers every modern IdP.
