# Local development setup

<!--
  title: Local development setup
  audience: contributor
  status: stub
-->

This guide covers setting up a local development environment for the registry codebase — virtual environment, hot reload, pre-commit hooks, and pointing tests at a local database.

For the gate reference and CI platform wiring, see [`ci.md`](02-ci.md).

**Preconditions:**

- Python 3.13 (the project requires `>=3.12`; example CI wirings pin 3.13)
- Docker + Docker Compose (for Postgres, pgvector, observability stack)
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

<!-- stub: python -m venv .venv && source .venv/bin/activate; pip install -e ".[dev]"; note: installs registry package in editable mode so code changes are picked up immediately -->

## Start the dev stack

<!-- stub: docker compose up -d; services and ports (API :8000, Postgres :5544, PgBouncer :6432, Jaeger :16686, Prometheus :9090, Grafana :3000); make dev-token + make dev-seed to populate the dev tenant; pointer to quickstart.md for the full walkthrough -->

## Run the app with hot reload

<!-- stub: uvicorn registry.main:app --reload --host 0.0.0.0 --port 8000; DATABASE_URL must point at the compose Postgres or PgBouncer; changes to .py files restart the server automatically; note: reload does NOT apply to Alembic migrations — apply them with make migrate then restart -->

## Install pre-commit hooks

<!-- stub: pre-commit install (one-time, after cloning); hooks run lint + format-check + typecheck + doc-refs + test-hygiene on every git commit; same make targets as CI — no separate config; how to skip a hook intentionally (--no-verify + justification in commit body, rarely needed) -->

## Run the test gates

<!-- stub: make test-unit (fast, no DB, ~2s); make test-integration (needs Docker, ~2 min); make test-conformance (needs Docker, contract drift); make test-perf (SLO verification, release-pipeline only); full gate reference in ci.md -->

## Point tests at an external database

<!-- stub: export DATABASE_URL=postgresql+asyncpg://... before make test-integration; testcontainer skips its own Postgres when DATABASE_URL is set; useful in CI without Docker access -->

## Regenerate the OpenAPI snapshot

<!-- stub: make openapi-export; commit the updated openapi.json; make test-conformance fails if the committed file is stale; run after adding or changing any API endpoint -->

---

**See also:**

- [`ci.md`](02-ci.md) — gate descriptions, make target reference, CI platform wiring
- [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) — DCO sign-off, code style summary
- [`../../../CLAUDE.md`](../../../CLAUDE.md) — project-wide conventions every contributor must follow
