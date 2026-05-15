# Local development setup

<!--
  title: Local development setup
  audience: contributor
  status: complete
-->

This guide covers setting up a local development environment for the registry codebase — virtual environment, hot reload, pre-commit hooks, and pointing tests at a local database.

For the gate reference and CI platform wiring, see [`ci.md`](02-ci.md). For first-time setup that just gets you to an authenticated `GET /v1/whoami`, see [`quickstart.md`](../02-get-started/01-quickstart.md).

**Preconditions:**

- Python 3.13 (the project requires `>=3.12`; example CI wirings pin 3.13)
- Docker + Docker Compose (for Postgres, pgvector, observability stack, mock IDP, mock entitlement service)
- `make`, `curl`, `jq` (or `python3 -m json.tool`)
- ~4 GB free RAM

**What this guide covers:**

- [Install dependencies](#install-dependencies)
- [Start the dev stack](#start-the-dev-stack)
- [Run the app with hot reload](#run-the-app-with-hot-reload)
- [Install pre-commit hooks](#install-pre-commit-hooks)
- [Run the test gates](#run-the-test-gates)
- [Point tests at an external database](#point-tests-at-an-external-database)
- [Regenerate the OpenAPI snapshot](#regenerate-the-openapi-snapshot)

---

## Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

`-e ".[dev]"` installs the `registry` package in editable mode plus the `dev` extras (pytest, ruff, mypy, pre-commit, testcontainers, mcp client SDK). Code changes are picked up immediately — no reinstall needed.

The Makefile resolves Python in this order: `registry/.venv/bin/python`, then the first `python3` on `PATH`. Override with `make PYTHON=python3.13 <target>`.

## Start the dev stack

```bash
docker compose up -d
```

This brings up:

| Service | Port (host) | What it is |
|---|---|---|
| `registry-api` | 8000 | The FastAPI app under uvicorn `--reload` |
| `postgres` (pgvector) | 5544 | Primary DB. Database: `registry`. User: `postgres`, password: `password`. |
| `pgbouncer` | 6432 | Transaction-pooling fronting Postgres — what the app talks to |
| `mock-oauth2-server` | 8090 | Local OIDC IdP (`navikt/mock-oauth2-server`). Issues JWTs for dev. |
| `mock-entitlement-service` | 8091 | Local entitlement-service stand-in. Returns canned grants per `userId`. |
| `jaeger` | 16686 | Trace UI |
| `prometheus` | 9090 | Metrics |
| `grafana` | 3000 | Dashboards (admin / admin) |

Bootstrap the dev tenant + mock-IDP credentials + mock entitlements (idempotent):

```bash
make migrate         # apply migrations
make dev-token       # seed tenant + actor + mock OIDC client + canned entitlements
make dev-seed        # (optional) seed the closed-vocabulary + two demo capabilities
```

Get a fresh JWT for any local request:

```bash
export TOKEN=$(make dev-jwt)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/whoami
```

## Run the app with hot reload

The `registry-api` container ships with `uvicorn --reload` already configured — code changes under `registry/` are picked up automatically. If you prefer running uvicorn outside Docker (e.g. for breakpoint debugging in your IDE):

```bash
docker compose up -d postgres pgbouncer mock-oauth2-server mock-entitlement-service
export DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:6432/registry
export OIDC_DISCOVERY_URL=http://localhost:8090/default/.well-known/openid-configuration
export OIDC_ISSUER_ALLOWLIST="http://localhost:8090/default"
export RESOURCE_URI_ALLOWLIST=registry
export ENTITLEMENT_SERVICE_URL=http://localhost:8091
export ENTITLEMENT_SERVICE_ENV=DEV
export ENTITLEMENT_SERVICE_DISCRIMINATOR=REGISTRY
export ENTITLEMENT_ROLE_MAPPING="ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor"
export OIDC_MAX_TOKEN_TTL_SECONDS=3600
export SCHEDULER_USE_MEMORY_JOBSTORE=true

uvicorn registry.main:app --reload --host 0.0.0.0 --port 8000
```

Hot reload **does not** apply Alembic migrations. After changing a migration, run `make migrate` and then restart the process.

## Install pre-commit hooks

One-time, after cloning:

```bash
pre-commit install
```

This installs hooks that run on every `git commit`:

- `make lint` — ruff lint
- `make format-check` — ruff format (read-only check)
- `make typecheck` — mypy `--strict`
- `make doc-refs` — gate against external-doc references in shipped code (`scripts/check_no_doc_refs.py`)
- `make test-hygiene` — gate against phase-named tests (`scripts/check_no_phase_named_tests.py`)

The hooks call the same Make targets CI calls — there is no separate hook config to drift from CI. Skipping a hook (`--no-verify`) is rarely justified; if you do, explain why in the commit body so reviewers can verify the bypass.

## Run the test gates

```bash
make test-unit          # ~2s, no DB, default home for new tests
make test-integration   # ~2 min, testcontainer Postgres, real SQL
make test-conformance   # contract drift gates (openapi snapshot, MCP catalog, cross-tenant)
make test-perf          # SLO verification (latency p95s, webhook fan-out) — release pipeline only
```

`make test` runs `test-unit` + `test-integration` + `test-conformance` together. `test-perf` is excluded from the default suite and `pytest.mark.slow`-gated.

For the rationale behind each tier and the gates wired into CI, see [`ci.md`](02-ci.md).

## Point tests at an external database

By default, `tests/integration/` spins up its own Postgres via testcontainers. To point at an existing database instead (e.g. inside a CI environment without Docker-in-Docker):

```bash
export DATABASE_URL=postgresql+asyncpg://postgres:password@somehost:5432/registry
make test-integration
```

The conftest detects `DATABASE_URL` and skips testcontainer startup. The schema must already be migrated to the latest head (`make migrate` against the same URL).

The compose-stack smoke test at `tests/integration/test_auth_compose_smoke.py` is gated by `COMPOSE_STACK_UP=1` instead — set that env var when the local mocks are reachable and you want the real-JWT round-trip exercised:

```bash
COMPOSE_STACK_UP=1 pytest tests/integration/test_auth_compose_smoke.py -m compose -q
```

## Regenerate the OpenAPI snapshot

The committed `openapi.json` is the conformance baseline. Any change to a router, response model, or security scheme requires regenerating it:

```bash
make openapi-export
```

The script writes to `openapi.json` at the repo root. Commit the diff alongside the code change; `make test-conformance` fails CI if the committed file is stale.

---

**See also:**

- [`ci.md`](02-ci.md) — gate descriptions, make target reference, CI platform wiring
- [`../02-get-started/01-quickstart.md`](../02-get-started/01-quickstart.md) — five-minute path to an authenticated API call
- [`../05-reference/04-architecture.md`](../05-reference/04-architecture.md) — component map and request lifecycle
- [`../../CLAUDE.md`](../../CLAUDE.md) — project-wide conventions every contributor must follow
