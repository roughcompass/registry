# How to configure sync connectors

<!--
  title: Configure sync connectors
  audience: operator
  status: stub
-->

The registry ingests external sources — GitHub repositories, GitLab projects, OpenAPI specs, npm packages — and populates entity facts automatically. This guide covers configuring, running, and troubleshooting sync connectors.

**Preconditions:**

- An admin-level token for the target tenant.
- Credentials for the external source (GitHub token, GitLab token, etc.) stored in your deployment's secret store — never committed.
- A registered sync-source entry for the tenant: `POST /v1/admin/tenants/<tenant_id>/sync-sources`.

**What this guide covers:**

- [Register a sync source](#register-a-sync-source)
- [Configure connector credentials](#configure-connector-credentials)
- [Receive webhook events from GitHub and GitLab](#receive-webhook-events-from-github-and-gitlab)
- [Run a connector manually](#run-a-connector-manually)
- [Understand the fetch / parse pattern](#understand-the-fetch--parse-pattern)
- [Troubleshoot stuck or failed runs](#troubleshoot-stuck-or-failed-runs)

---

## Register a sync source

<!-- stub: POST /v1/admin/tenants/{tenant_id}/sync-sources — external_system slug, display_name; this slug is the external_system value used in external-ID lookups -->

## Configure connector credentials

<!-- stub: credentials come exclusively from env vars at runtime, never stored in DB; per-connector reference strings; deployment secret store patterns (Kubernetes Secret, Secrets Manager, Vault); CONNECTOR_RUN_TIMEOUT_S env var -->

## Receive webhook events from GitHub and GitLab

<!-- stub: GITHUB_WEBHOOK_SECRET + GITLAB_WEBHOOK_SECRET env vars; read directly by sync/webhook.py (not Settings) to allow rotation without restart; webhook endpoints registered in the source platform; no restart required for rotation -->

## Run a connector manually

<!-- stub: how to trigger a connector run (CLI / direct invocation); two-step pattern — fetch (side effects, I/O) then parse (pure, no I/O); checking run logs for errors -->

## Understand the fetch / parse pattern

<!-- stub: Connector base class; fetch() pulls raw data; parse() is pure, produces structured records; why this split makes connectors testable; CONNECTOR_RUN_TIMEOUT_S ceiling; per-connector credential resolution via dynamic ref string -->

## Troubleshoot stuck or failed runs

<!-- stub: check logs for CONNECTOR_RUN_TIMEOUT_S exceeded; verify credentials are present in env; re-run manually; embedding outbox backlog (OUTBOX_POLL_INTERVAL_S, OUTBOX_MAX_ATTEMPTS, dead-letter table) -->

---

**See also:**

- [`reference/configuration.md`](../05-reference/03-configuration.md) — `CONNECTOR_RUN_TIMEOUT_S`, `OUTBOX_*`, `GITHUB_WEBHOOK_SECRET`, `GITLAB_WEBHOOK_SECRET`
- [`operations/ops.md`](../06-operations/01-ops.md) — embedding backfill after connector runs
- [`reference/api.md`](../05-reference/01-api.md) — `/v1/admin/tenants/{tenant_id}/sync-sources` admin endpoint
