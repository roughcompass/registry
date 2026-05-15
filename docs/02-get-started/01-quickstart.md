# Quickstart

Get from zero to an authenticated API call in under five minutes. This guide uses Docker Compose to start all dependencies.

**Prerequisites:** Docker + Docker Compose, no Python installation required for this path.

---

## Step 1 — Clone and start

```bash
git clone <repo-url>
cd <repo>/registry
docker compose up -d
```

First run downloads images and may take 30–60 seconds. When complete, the service is listening on port 8000.

Verify it started:

```bash
curl http://localhost:8000/healthz
```

Expected output:

```json
{"status":"ok"}
```

What is now running:

| Service | Port | URL |
|---|---|---|
| Registry API | 8000 | http://localhost:8000 |
| Swagger UI | 8000 | http://localhost:8000/docs |
| ReDoc | 8000 | http://localhost:8000/redoc |
| Postgres (pgvector) | 5544 | `postgresql://postgres:password@localhost:5544/registry` |
| PgBouncer | 6432 | `postgresql://postgres:password@localhost:6432/registry` |
| Jaeger (traces) | 16686 | http://localhost:16686 |
| Prometheus | 9090 | http://localhost:9090 |
| Grafana | 3000 | http://localhost:3000 (admin / admin) |

---

## Step 2 — Bootstrap the dev tenant + fetch a JWT

```bash
make dev-token
```

This seeds a `111205` tenant + a `dev-admin` actor in Postgres, registers a `registry-dev` client in the local mock OIDC server (`mock-oauth2-server` on port 8090), and seeds canned entitlements for that user in the mock entitlement service. The IDs and mock-client credentials land in `.env.dev`:

```
DEV_TENANT_SLUG=111205
DEV_TENANT_ID=<uuid>
DEV_ACTOR_ID=<uuid>
DEV_USER_ID=dev-admin
CLIENT_ID=registry-dev
CLIENT_SECRET=dev-secret
```

`.env.dev` is git-ignored. `make dev-token` is idempotent — re-running reuses the same tenant + actor + client.

Exchange the dev credentials for a bearer JWT against the mock IDP:

```bash
export TOKEN=$(make dev-jwt)
```

`make dev-jwt` reads `.env.dev`, hits the mock IDP, and prints the JWT to stdout — composable with `$(make dev-jwt)` inside any curl command. TTL is 3600s; re-run to refresh. See [authentication.md](../01-overview/04-authentication.md#fetching-a-jwt) for the equivalent raw curl and why `scope=registry` matters.

---

## Step 3 — Seed demo data

```bash
make dev-seed
```

Seeds closed-vocabulary values (entity types, edge relationship types, lifecycle states) and inserts two demo capabilities: Salt Design System and an enterprise user-preference service. Without this, `POST /v1/capabilities` rejects with `unknown vocabulary value` and `GET /v1/capabilities` returns an empty list.

`make dev-seed` is idempotent — re-running produces the same entity UUIDs.

---

## Step 4 — Make an authenticated call

```bash
curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/v1/capabilities
```

Expected output: a JSON array with the two seeded capabilities.

Try fetching one by name:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/v1/capabilities/salt-design-system
```

Expected: a single capability record.

---

## Step 5 — Explore via Swagger

Open http://localhost:8000/docs, click **Authorize**, and paste the token into the **bearerAuth** field. Every endpoint's **Try it out** button then sends the bearer header automatically.

For full API reference including request/response schemas, see [reference/api.md](../05-reference/01-api.md) and the live OpenAPI spec at http://localhost:8000/openapi.json.

---

## Stopping the stack

```bash
docker compose down        # stop containers, keep volume data
docker compose down -v     # stop containers AND wipe the database
```

---

## Next steps

| I want to… | Go to |
|---|---|
| Understand tenants, entities, visibility | [overview/vocabulary.md](../01-overview/03-vocabulary.md) |
| Set up OIDC or production tokens | [overview/authentication.md](../01-overview/04-authentication.md) |
| Understand role grants, tenant selection, entitlements | [overview/authorization.md](../01-overview/05-authorization.md) |
| Configure env vars | [reference/configuration.md](../05-reference/03-configuration.md) |
| Call from an AI agent via MCP | [reference/mcp-tools.md](../05-reference/02-mcp-tools.md) |
