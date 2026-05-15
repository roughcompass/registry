# How to configure sync connectors

<!--
  title: Configure sync connectors
  audience: operator
  status: current
-->

The registry ingests external sources — GitHub repositories, GitLab projects, OpenAPI specs, npm packages — and populates entity facts automatically. This guide covers configuring, running, and troubleshooting sync connectors.

**Preconditions:**

- A bearer token with the `admin` role for the target tenant. In local dev, run `make dev-jwt` to mint a short-lived token.
- Credentials for the external source (GitHub token, GitLab token, etc.) stored in your deployment's secret store — never committed.

**What this guide covers:**

- [Register a sync source](#register-a-sync-source)
- [Configure connector credentials](#configure-connector-credentials)
- [Receive webhook events from GitHub and GitLab](#receive-webhook-events-from-github-and-gitlab)
- [Run a connector manually](#run-a-connector-manually)
- [Understand the fetch / parse pattern](#understand-the-fetch--parse-pattern)
- [Troubleshoot stuck or failed runs](#troubleshoot-stuck-or-failed-runs)

---

## Register a sync source

`POST /v1/admin/sync-sources` registers a new sync source. The tenant is resolved from the bearer token — there is no tenant ID in the URL. The endpoint validates that `source_type` names a known connector and, if `credentials_ref` is provided, calls `connector.validate()` to confirm the credential is present in the environment before persisting the row.

```bash
curl -s -X POST https://api.example.com/v1/admin/sync-sources \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "openapi",
    "display_name": "Payment Gateway OpenAPI Spec",
    "config": {
      "url": "https://internal.example.com/payment-gateway/openapi.json",
      "entity_id": "a1b2c3d4-0000-0000-0000-000000000001"
    },
    "credentials_ref": "PAYMENT_GATEWAY_API_KEY",
    "schedule": "0 */6 * * *"
  }' | jq .
```

| Field | Required | Meaning |
|---|---|---|
| `source_type` | yes | Connector identifier (`openapi`, `package_json`, `markdown_adr_rfc`, `release_notes`, `docs_corpus`) |
| `display_name` | yes | Human label for this source |
| `config` | no | Connector-specific config dict (URLs, repo paths, etc.) |
| `credentials_ref` | no | Name of the env var that holds the secret; resolved at sync time |
| `schedule` | no | Cron expression for automatic runs (UTC) |

The response includes `source_id`. Use it to trigger manual runs and list run history.

**Idempotency:** send `Idempotency-Key: <uuid>` to make the create safe to retry.

**Errors:**
- `422` — `source_type` is not a registered connector, or `connector.validate()` rejected the credential.
- `404` — on trigger: `source_id` not found or the source is inactive.

---

## Configure connector credentials

Connector credentials are resolved exclusively from environment variables at sync time. They are never stored in the database, config files, or `Settings`. This design allows credential rotation without redeploying or restarting the application.

The `credentials_ref` field on a sync-source row is a string naming the environment variable to read. At sync time, `sync/connector.py::resolve_credential(ref)` looks up `os.environ[ref]` and raises `CredentialError` if the variable is absent.

**Deployment patterns:**

Kubernetes Secret mounted as an env var:

```yaml
env:
  - name: PAYMENT_GATEWAY_API_KEY
    valueFrom:
      secretKeyRef:
        name: payment-gateway-creds
        key: api-key
```

AWS Secrets Manager via ECS task definition:

```json
{
  "secrets": [
    {
      "name": "PAYMENT_GATEWAY_API_KEY",
      "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789:secret:payment-gw-api-key"
    }
  ]
}
```

HashiCorp Vault agent sidecar injects the value into the process environment under the configured env-var name before the app process starts.

**Rotation:** update the secret in your secret store and restart the relevant worker pods (or the app process). The connector reads the env var fresh at the start of each sync run — no source-row update is needed.

**`CONNECTOR_RUN_TIMEOUT_S`** (default: `300`) sets the maximum wall-clock time for a single connector run. Runs that exceed this are terminated and marked `failed`. Raise it for sources with large artifact sets.

---

## Receive webhook events from GitHub and GitLab

Register a webhook in the source platform pointing at the registry's webhook endpoint. The endpoint is HMAC-verified — no bearer auth is required or accepted.

**GitHub:**

Webhook URL: `https://api.example.com/webhooks/github?source_id=<source_id>`

Configure the webhook secret in GitHub and set the same value in the registry process environment:

```bash
export GITHUB_WEBHOOK_SECRET="your-github-webhook-secret"
```

The registry reads `GITHUB_WEBHOOK_SECRET` directly from the environment (not from `Settings`) so it can be rotated without an application restart. On receipt, the handler verifies the `X-Hub-Signature-256` header using HMAC-SHA256 over the raw body.

**GitLab:**

Webhook URL: `https://api.example.com/webhooks/gitlab?source_id=<source_id>`

```bash
export GITLAB_WEBHOOK_SECRET="your-gitlab-webhook-secret"
```

The `X-Gitlab-Token` header is compared constant-time against `GITLAB_WEBHOOK_SECRET`.

Both endpoints return `200 {"status": "accepted", "delivery_id": "..."}` immediately and enqueue a one-shot sync job. An invalid or missing signature returns `401`. Duplicate `delivery_id` values are silently ignored (idempotent).

**Secret rotation:** update the env var in your secret store, then restart the webhook-handling worker. The new value is read on the next process start. No database change is needed.

---

## Run a connector manually

To trigger an immediate sync outside the schedule:

```bash
curl -s -X POST \
  "https://api.example.com/v1/admin/sync-sources/<source_id>/trigger" \
  -H "Authorization: Bearer <admin-token>" \
  -H "Idempotency-Key: $(uuidgen)" | jq .
```

The response is `202 Accepted` with a `sync_run_id`. The run executes asynchronously in the scheduler. Poll its status with:

```bash
curl -s \
  "https://api.example.com/v1/admin/sync-runs/<sync_run_id>" \
  -H "Authorization: Bearer <admin-token>" | jq '{status, duration_s, artifact_count, error_summary}'
```

**List recent runs** for a source:

```bash
curl -s \
  "https://api.example.com/v1/admin/sync-runs?source_id=<source_id>&status=failed" \
  -H "Authorization: Bearer <admin-token>" | jq .
```

Query parameters for `GET /v1/admin/sync-runs`:

| Parameter | Meaning |
|---|---|
| `source_id` | Filter by source UUID |
| `status` | Filter by run status (`queued`, `running`, `success`, `failed`) |
| `from` | ISO-8601 lower bound on `started_at` |
| `to` | ISO-8601 upper bound on `started_at` |

---

## Understand the fetch / parse pattern

Every connector implements four methods:

| Method | I/O? | Purpose |
|---|---|---|
| `validate(credentials_ref)` | network | Called once at source-registration time to confirm credentials work |
| `discover(source)` | network | Returns the list of `DiscoveredArtifact` objects available from the source |
| `fetch(artifact, source)` | network | Downloads raw bytes for a single artifact |
| `parse(artifact, raw)` | none | Extracts `ParsedFact` objects from raw bytes — **pure, no side effects** |

`parse()` is intentionally pure: it never makes network calls, never writes to the database, and never mutates global state. Calling it twice with the same inputs returns equivalent results. This separation makes connectors testable without live external services — unit tests mock `fetch` and verify `parse` output in isolation.

`CONNECTOR_RUN_TIMEOUT_S` bounds the entire `discover → fetch → parse → ingest` loop per run. Individual `fetch` calls are not separately bounded — if a single artifact download hangs, it counts against the run timeout.

---

## Troubleshoot stuck or failed runs

**Run exceeded `CONNECTOR_RUN_TIMEOUT_S`:** the run appears as `status: failed` with `error_summary` containing `"timeout"`. Increase `CONNECTOR_RUN_TIMEOUT_S` in the process environment if the source legitimately has a large artifact set, or split the source into multiple narrower sync sources.

**Credential not found:** `error_summary` will contain `CredentialError: Credential environment variable 'XYZ' is not set`. Confirm the env var is present in the worker process:

```bash
kubectl exec -it <worker-pod> -- printenv | grep XYZ
```

Set it via your secret store and restart the pod.

**Connector validation rejected on re-activate:** if you deactivated a sync source (`PATCH /v1/admin/sync-sources/<id>` with `is_active: false`) and then re-activate it, the trigger endpoint calls `connector.validate()` again. Ensure the credential is still valid in the upstream system.

**Outbox backlog not draining:** facts written by a sync run are published to consumers via an outbox. If the outbox worker is behind, check `OUTBOX_POLL_INTERVAL_S` and `OUTBOX_MAX_ATTEMPTS` in your deployment env. Facts that exhaust `OUTBOX_MAX_ATTEMPTS` move to the dead-letter table — see [`operations/ops.md`](../06-operations/01-ops.md) for the recovery procedure.

**Superseded facts:** after a successful run, facts from the previous run that were not re-emitted are marked superseded. Inspect them with:

```bash
curl -s \
  "https://api.example.com/v1/admin/sync-runs/<sync_run_id>/superseded" \
  -H "Authorization: Bearer <admin-token>" | jq 'length'
```

A high superseded count after a config change is normal. A high count on a steady-state source suggests the connector is not discovering all artifacts it previously found.

---

**See also:**

- [`reference/configuration.md`](../05-reference/03-configuration.md) — `CONNECTOR_RUN_TIMEOUT_S`, `OUTBOX_*`, `GITHUB_WEBHOOK_SECRET`, `GITLAB_WEBHOOK_SECRET`
- [`operations/ops.md`](../06-operations/01-ops.md) — embedding backfill after connector runs
- [`reference/api.md`](../05-reference/01-api.md) — sync-sources and sync-runs admin endpoints
