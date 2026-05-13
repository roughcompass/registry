# Registry

Multi-tenant catalog where platform teams publish capabilities — services,
libraries, design systems, agents, and anything else one team ships for others
to build on. Consumer teams discover, adopt, and track those capabilities over
time; both human developers and AI agents read and write here through the same
REST and MCP surfaces. For a full architectural overview, see
[`docs/01-overview/02-how-its-structured.md`](docs/01-overview/02-how-its-structured.md).

For repo-wide conventions (rules every contributor and AI agent must follow),
see [`CLAUDE.md`](../CLAUDE.md). For project orientation beyond this repo, see
the [top-level README](../README.md).

---

## Who are you?

| I am… | Start here |
|---|---|
| **Evaluating fit** — assessing whether to adopt this registry | [`docs/01-overview/01-orientation.md`](docs/01-overview/01-orientation.md) → [`docs/01-overview/02-how-its-structured.md`](docs/01-overview/02-how-its-structured.md) |
| **Integrating** — building against the API or connecting an agent | [`docs/02-get-started/01-quickstart.md`](docs/02-get-started/01-quickstart.md) → [`docs/05-reference/01-api.md`](docs/05-reference/01-api.md) → [`docs/05-reference/02-mcp-tools.md`](docs/05-reference/02-mcp-tools.md) |
| **Operating** — deploying and running a production instance | [`docs/05-reference/03-configuration.md`](docs/05-reference/03-configuration.md) → [`docs/06-operations/01-ops.md`](docs/06-operations/01-ops.md) |
| **Contributing** — working on the registry codebase | [`CONTRIBUTING.md`](CONTRIBUTING.md) → [`docs/07-contributing/02-ci.md`](docs/07-contributing/02-ci.md) |

---

## Prerequisites

- Python **3.13** (project requires `>=3.12`; example CI wirings pin 3.13)
- Docker + Docker Compose (for Postgres, pgvector, observability stack)
- ~4 GB of free RAM
- macOS / Linux supported; Windows via WSL2

---

## Quick start

Five commands to a live `/healthz`:

```bash
git clone <repo-url>
cd <repo>/registry
docker compose up -d
curl http://localhost:8000/healthz
# → {"status":"ok"}
```

For the full path — minting a token, seeding demo data, and making an
authenticated call — see [`docs/02-get-started/01-quickstart.md`](docs/02-get-started/01-quickstart.md).

---

## Where to find things

| Path | What lives there |
|---|---|
| `registry/api/routers/` | HTTP surface — one router per concern (capabilities, adoptions, subscriptions, …) |
| `registry/api/middleware/` | Tenant resolution, rate-limit, HTTP-methods router factory |
| `registry/service/` | Business logic — every cross-tenant query goes through `service/visibility.py` |
| `registry/workers/` | Background jobs (webhook delivery, closure-cache refresh) |
| `registry/storage/` | SQLAlchemy models + Alembic migrations under `migrations/versions/` |
| `registry/security/` | PII scanner (patterns + policy resolver) |
| `sync/` | External-source ingest (GitHub, GitLab, OpenAPI, …); per-connector credentials are env-var refs |
| `scripts/` | Operational CLIs: `mint_token`, `backfill_embeddings`, `reindex_embeddings`, `partition_migrate`, `export_openapi`, `check_no_doc_refs` |
| `tests/{unit,integration,conformance,perf}/` | Test pyramid; unit tests are fast and DB-free, integration uses testcontainers |
| `docs/` | Shipped documentation (overview, API reference, runbooks, MCP tools) |
| `eval/EVAL.md` | Exit-criteria ledger; `CAP-PN-TNN` entries are commit-history anchors |
| `Dockerfile` | Canonical build instructions |

Production packaging examples (Helm chart, Grafana dashboards) live in
[`packaging/`](packaging/).

---

## Configuration

The canonical inventory of every env var the app reads is
[`.env.example`](.env.example). For deployment targets and configuration
detail, see [`docs/05-reference/03-configuration.md`](docs/05-reference/03-configuration.md).

---

## Contributing

- Read [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution flow and DCO requirement.
- Read [`../CLAUDE.md`](../CLAUDE.md) for project-wide conventions.
- Run `pre-commit install` once after cloning to pick up the lint, type-check,
  and doc-refs gates before each commit.
- See [`docs/07-contributing/02-ci.md`](docs/07-contributing/02-ci.md) for the full gate
  reference and CI platform wiring.

