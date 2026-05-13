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

## Step 2 — Mint a token

```bash
make dev-token
```

This seeds a `dev` tenant, creates a `dev-admin` actor with all four roles (`consumer`, `producer`, `admin`, `auditor`), and prints a bearer token. Example output:

```
REGISTRY_DEV_TOKEN=reg_dev_xxxxxxxxxxxxxx...
```

Copy the token value. To persist it to a file for reuse:

```bash
make dev-token TOKEN_OUT=.env.dev
```

`.env.dev` is git-ignored. `make dev-token` is idempotent — running it again reuses the same tenant and actor and mints a fresh token.

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
export TOKEN=<paste-token-here>

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
| Set up OIDC or production tokens | [overview/auth.md](../01-overview/04-auth.md) |
| Configure env vars | [reference/configuration.md](../05-reference/03-configuration.md) |
| Call from an AI agent via MCP | [reference/mcp-tools.md](../05-reference/02-mcp-tools.md) |
