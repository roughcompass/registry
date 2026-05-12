# registry

Semantic + temporal retrieval system for an organisation's engineering
capabilities. FastAPI + asyncpg + SQLAlchemy on Python 3.13, bi-temporal
data model, cross-tenant isolation enforced at a single chokepoint.

For repo orientation, see the [top-level README](../README.md). For
project-wide conventions (and the rules every contributor + AI agent
must follow), see [`CLAUDE.md`](../CLAUDE.md).

---

## Prerequisites

- Python **3.13** (project requires `>=3.12`; example CI wirings pin 3.13)
- Docker + Docker Compose (for Postgres, pgvector, observability stack)
- ~4 GB of free RAM
- macOS / Linux supported; Windows via WSL2

---

## Quickstart — running the app

Five commands to a live `/healthz`:

```bash
git clone <repo-url>
cd <repo>/registry
docker compose up -d                                  # ~30s on first run
curl http://localhost:8000/healthz
# → {"status":"ok"}
```

What just started, on which port:

| Service | Port | URL |
|---|---|---|
| Catalog API | 8000 | http://localhost:8000 |
| Swagger UI | 8000 | http://localhost:8000/docs |
| ReDoc | 8000 | http://localhost:8000/redoc |
| Postgres (pgvector) | 5544 | `postgresql://postgres:password@localhost:5544/registry` |
| PgBouncer | 6432 | `postgresql://postgres:password@localhost:6432/registry` |
| Jaeger (traces) | 16686 | http://localhost:16686 |
| Prometheus | 9090 | http://localhost:9090 |
| Grafana | 3000 | http://localhost:3000 (admin / admin) |

To stop everything: `docker compose down`. To wipe the database too: `docker compose down -v`.

---

## Local dev loop (without Docker for the app)

If you want hot-reload + breakpoints in your editor, run the app process
on your host and only the DB + observability stack in Docker.

```bash
# 1. install Python deps
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. start just the DB containers
docker compose up -d postgres pgbouncer

# 3. apply migrations
export DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5544/registry
alembic upgrade head

# 4. run the app
uvicorn catalog.main:create_app --factory --reload --port 8000

# 5. enable pre-commit hooks (one-time)
pre-commit install
```

### Common commands

Every gate the project enforces is a Make target at the repo root —
the `Makefile` is the canonical command surface; CI platforms wire to
it. Run `make help` for the full list.

```bash
make all                # everything a PR must pass (~5s on a clean run)
make lint               # ruff check
make format-check       # ruff format --check
make format             # ruff format (writes)
make typecheck          # mypy --strict
make doc-refs           # no internal-doc references in shipped code
make test-unit          # 1100+ unit tests, <2s
make test-integration   # needs a Postgres testcontainer
make test-conformance   # contract conformance
make test-perf          # SLO perf, marked @slow
make migrate            # alembic upgrade head
make openapi-export     # regenerate openapi.json
make dev-token          # mint a local-dev tenant + token in one shot
make dev-seed           # seed vocabulary + 2 demo capabilities (idempotent)
```

CI platforms (GitHub Actions, GitLab CI, …) invoke these same targets —
they don't redefine the commands. See [`docs/ci.md`](docs/ci.md) for the
architecture and the two shipped example wirings.

### Getting a token to try the API

After `docker compose up -d`, run:

```bash
make dev-token   # mint a tenant + actor + bearer token
make dev-seed    # seed vocabulary + 2 demo capabilities
```

`dev-token` prints the bearer token; paste it into Swagger's
**Authorize** button at [`/docs`](http://localhost:8000/docs), or use it
directly:

```bash
curl -H 'Authorization: Bearer <paste-token-here>' \
     http://localhost:8000/v1/capabilities
```

`dev-seed` is what makes the rest of `/docs` useful — without it, the
dev tenant has no vocabulary values (so `POST /v1/capabilities` rejects
with `unknown vocabulary value`) and no entities (so the GET list is
empty). It seeds the closed-vocabulary values the catalog expects plus
two demo capabilities (Salt Design System + an enterprise user-
preference service) so every endpoint has something to return.

For the full auth model (OIDC, API tokens, local dev), see
[`docs/auth.md`](docs/auth.md).

---

## Where to find things

| Path | What lives there |
|---|---|
| `catalog/api/routers/` | HTTP surface — one router per concern (capabilities, adoptions, subscriptions, …) |
| `catalog/api/middleware/` | Tenant resolution, rate-limit, HTTP-methods router factory |
| `catalog/service/` | Business logic — every cross-tenant query goes through `service/visibility.py` |
| `catalog/workers/` | Background jobs (webhook delivery, closure-cache refresh) |
| `catalog/storage/` | SQLAlchemy models + Alembic migrations under `migrations/versions/` |
| `catalog/security/` | PII scanner (patterns + policy resolver) |
| `sync/` | External-source ingest (GitHub, GitLab, OpenAPI, …); per-connector credentials are env-var refs |
| `scripts/` | Operational CLIs: `mint_token`, `backfill_embeddings`, `reindex_embeddings`, `partition_migrate`, `export_openapi`, `check_no_doc_refs` |
| `tests/{unit,integration,conformance,perf}/` | Test pyramid; unit tests are fast and DB-free, integration uses testcontainers |
| `docs/` | Operator runbooks (currently: disaster-recovery) |
| `eval/EVAL.md` | Phase-by-phase exit-criteria ledger; CAP-PN-TNN entries are commit-history anchors |
| `Dockerfile` | Canonical build instructions — consumers building their own image use this |

Production packaging examples (Helm chart, Grafana dashboards) live in
[`packaging/`](packaging/) — one example deployment, substitute your own.

---

## Configuration

The **canonical inventory of every env var the app reads** is
[`.env.example`](.env.example) at this directory's root. Required vars
(database URL) have no default; optional vars list the shipped default
inline. Operators on any deployment target consume the same env vars —
Kubernetes ConfigMap+Secret, AWS ECS task-definition, EC2 systemd
EnvironmentFile, etc. all wire the same values, just through different
mechanisms.

`Settings` (in `catalog/config.py`) is the single env-var reader; the
only documented bypasses are webhook secrets (per-instance override
pattern) and per-connector credentials (resolved by dynamic ref string
at runtime).

---

## Contributing

- Read [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution flow.
- Read [`../CLAUDE.md`](../CLAUDE.md) for project-wide conventions — this
  is also where the rules that AI agents (Claude, etc.) follow are
  documented.
- Run `pre-commit install` once after cloning to pick up the local
  lint + type-check + doc-refs gate before each commit.
