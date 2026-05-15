<!--
  title: Use case — Mirroring an external source of truth
  audience: operator
  archetype: explanation (use-case scenario)
  summary: How to ingest GitHub repositories, OpenAPI specs, or other external sources into the registry via sync connectors.
-->

# Use case: Mirroring an external source of truth

Many platform teams already publish capability metadata somewhere external — GitHub repositories, OpenAPI spec files, npm manifests, or ADR corpora. Rather than manually re-entering that data in the registry, operators configure sync connectors that pull from the external source on a schedule and populate entity facts automatically. The fetch/parse separation keeps connector logic pure and testable; credentials come exclusively from environment variables at runtime and are never stored in the database.

The result is a registry that stays current with the external source without human intervention: GitHub topics become entity attributes, OpenAPI operation lists become structured facts, release notes populate the bi-temporal fact store as timestamped observations.

---

## Preconditions

Before registering a sync source:

- The target tenant must exist and the caller must hold `admin` role within it.
- Every call to the sync-source endpoints requires an `Authorization: Bearer <JWT>` header. See [Authentication](../01-overview/04-authentication.md) and [Authorization](../01-overview/05-authorization.md).
- Connector credentials must already be set in the process environment. They are **never** stored in the database or in `Settings`; they are resolved at run time from a named environment variable using the `credentials_ref` field you supply when registering the source.
- Each connector run is bounded by `CONNECTOR_RUN_TIMEOUT_S` (default `300` seconds). Increase this for large repositories before registering a source.

---

## Connector types

| `source_type` | What it ingests |
|---|---|
| `openapi` | An OpenAPI 3.x spec fetched from a URL; operations and schemas become entity facts. |
| `release_notes` | A release-notes feed (GitHub releases JSON or similar); each entry becomes a timestamped fact. |
| `markdown_adr_rfc` | A directory of Markdown files; files matching ADR/RFC conventions are tagged `adr`, others `markdown`. |
| `package_json` | An npm `package.json`; package name, version, and dependency graph become attributes. |
| `docs_corpus` | A flat corpus of Markdown documents (e.g., a docs site export); pages become full-text facts. |

The `source_type` value must match one of the five controlled-vocabulary strings above exactly. An unknown value causes `POST /v1/admin/sync-sources` to return `422`.

---

## Credential configuration

Connectors that require authentication (tokens, API keys) use an env-var reference string rather than a literal secret. When you register a source you pass `credentials_ref: "MY_GITHUB_TOKEN"` (or any name you choose). At run time `resolve_credential` reads `os.environ["MY_GITHUB_TOKEN"]`. The secret itself never touches the database.

This design lets you rotate a credential without restarting the API: update the env var in your secret store, then trigger a manual run. The next connector invocation picks up the new value.

Webhook secrets for incoming pushes follow the same principle. `GITHUB_WEBHOOK_SECRET` and `GITLAB_WEBHOOK_SECRET` are read directly by the webhook receiver, outside the normal settings path, so they can be rotated independently.

---

## Step 1 — Set credentials in the environment

Set the env var your connector will reference before registering the source. Name it anything that is meaningful to your deployment:

```bash
export GITHUB_API_TOKEN=ghp_...            # your GitHub personal-access token
```

For production deployments, populate this through your secret store (Kubernetes `Secret`, ECS task-definition secret refs, Vault, systemd `EnvironmentFile`) so the value is present in the API process's environment at boot.

---

## Step 2 — Register a sync source

`POST /v1/admin/sync-sources` creates the source for the calling tenant. The tenant is resolved from the JWT; there is no tenant identifier in the URL.

```bash
export TOKEN=$(make dev-jwt)

curl -s -X POST http://localhost:8000/v1/admin/sync-sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "openapi",
    "display_name": "Payments API spec",
    "credentials_ref": "GITHUB_API_TOKEN",
    "config": {
      "spec_url": "https://raw.githubusercontent.com/acme/payments/main/openapi.yaml"
    },
    "schedule": "0 * * * *"
  }'
```

**Request body fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `source_type` | string | yes | One of the five controlled-vocabulary values above. |
| `display_name` | string | yes | Human-readable label shown in run listings. |
| `credentials_ref` | string or null | no | Name of the env var that holds the credential. Omit if the connector needs no auth. |
| `config` | object | no | Connector-specific configuration. Passed verbatim to the connector; shape varies by `source_type`. |
| `schedule` | string or null | no | Cron expression for automatic runs (e.g. `"0 * * * *"` for hourly). Omit to run manually only. |

A `201` response returns the `SyncSourceResponse` object including the assigned `source_id`:

```json
{
  "source_id": "7f3e1a2b-...",
  "tenant_id": "...",
  "source_type": "openapi",
  "display_name": "Payments API spec",
  "credentials_ref": "GITHUB_API_TOKEN",
  "config": { "spec_url": "..." },
  "schedule": "0 * * * *",
  "is_active": true,
  "created_at": "2026-05-14T12:00:00Z",
  "created_by": "..."
}
```

---

## Step 3 — Trigger a manual run

Once the source is registered, trigger an immediate run without waiting for the next scheduled slot:

```bash
curl -s -X POST \
  http://localhost:8000/v1/admin/sync-sources/<source_id>/trigger \
  -H "Authorization: Bearer $TOKEN"
```

The response is `202 Accepted`. The run is queued immediately and executes within the timeout bound set by `CONNECTOR_RUN_TIMEOUT_S`.

To avoid duplicate submissions on retry, pass `X-Idempotency-Key`:

```bash
curl -s -X POST \
  http://localhost:8000/v1/admin/sync-sources/<source_id>/trigger \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: my-deploy-run-001"
```

---

## Step 4 — Check run status

List all runs for the tenant to confirm the run succeeded:

```bash
curl -s http://localhost:8000/v1/admin/sync-runs \
  -H "Authorization: Bearer $TOKEN"
```

Each `SyncRunResponse` item in the list includes:

| Field | Meaning |
|---|---|
| `sync_run_id` | UUID of this run. |
| `source_id` | The source that triggered it. |
| `status` | `pending`, `running`, `success`, or `failed`. |
| `trigger` | `manual` or `scheduled`. |
| `started_at` | ISO-8601 timestamp. |
| `finished_at` | ISO-8601 timestamp, or null if still running. |
| `duration_s` | Wall-clock seconds, or null if still running. |
| `artifact_count` | Number of artifacts processed. |
| `error_summary` | Short error description on failure, otherwise null. |

A `status` of `success` with a non-zero `artifact_count` confirms the connector fetched and parsed content. Ingested data lands as entity facts and attributes via the same write path as REST writes; it also feeds the embedding outbox so the entities become findable via semantic search.

---

## Step 5 — Configure inbound webhooks (optional)

If you want the registry to receive push events from GitHub or GitLab and trigger an incremental run immediately on each push, set the webhook secret env vars before registering the webhook in your source control provider:

```bash
# In the API process environment:
GITHUB_WEBHOOK_SECRET=<secret>
GITLAB_WEBHOOK_SECRET=<secret>
```

Then point your provider's webhook configuration at:

- `POST /webhooks/github` — for GitHub push events
- `POST /webhooks/gitlab` — for GitLab push events

The URL path contains no tenant segment. The webhook receiver uses the secret and the payload's repository slug to identify the target tenant automatically.

Rotating the secret does not require an API restart. Update the env var in your secret store; the next incoming request picks up the new value.

---

## Troubleshooting

**Run fails with `error_summary` containing "Credential environment variable ... is not set."**
The env var named in `credentials_ref` is absent from the API process environment. Set it and trigger a new run.

**`artifact_count` is 0 but status is `success`.**
The connector ran without error but found nothing to parse. For `openapi`, confirm the `spec_url` in `config` is reachable from the API host. For `markdown_adr_rfc` or `docs_corpus`, confirm the configured path or URL returns content.

**Partial parse failures (some artifacts succeed, some fail).**
Each artifact is parsed independently. A connector that fails on one file logs the error and continues. Check the `error_summary` for the affected run; for a detailed trace, inspect the API log at `sync_run_id` granularity.

**Re-sync after a schema change.**
Trigger a manual run after updating `config` via `PATCH /v1/admin/sync-sources/<source_id>`. Existing facts are overwritten by the new run's output through the bi-temporal write path; no manual cleanup is needed.

**Credential rotation.**
Update the env var in your secret store. Trigger a manual run. The connector picks up the new value on its next invocation without any restart.

---

## See also

- [API reference](../05-reference/01-api.md) — full endpoint contracts for `POST /v1/admin/sync-sources`, `GET /v1/admin/sync-runs`, and `POST /v1/admin/sync-sources/{source_id}/trigger`
- [Authentication](../01-overview/04-authentication.md) — how to obtain a JWT for admin calls
- [Authorization](../01-overview/05-authorization.md) — role grants required for admin endpoints
- [Event-driven consumers](04-event-driven-consumers.md) — once facts are ingested, subscribe to lifecycle events on the resulting capabilities
