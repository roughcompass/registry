# Configuration Reference

The canonical inventory of every environment variable the service reads is `.env.example` at the registry project root. This document explains the variables and their operational meaning. The `.env.example` file is the single source of truth for defaults; if these two files disagree, `.env.example` wins.

`Settings` in `registry/registry/config.py` is the single env-var reader. Code outside that file that reads `os.environ` directly is either documented as an intentional exception or is a bug.

**Two intentional exceptions** not in `Settings`:

1. `GITHUB_WEBHOOK_SECRET` and `GITLAB_WEBHOOK_SECRET` are read directly by `sync/webhook.py` to support per-instance secret rotation without a full settings reload.
2. Per-connector credentials (in `sync/`) are resolved by a dynamic reference string at runtime; the set is not fixed, so they cannot live in `Settings`.

---

## Required variables

These have no default. The app raises at startup if they are unset.

| Variable | Type | Description |
|---|---|---|
| `DATABASE_URL` | string | Async (asyncpg) connection string. Format: `postgresql+asyncpg://user:password@host:5432/registry`. The app process talks to PgBouncer in production; migrations talk to Postgres directly. |

---

## Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — (required) | Primary async connection string (asyncpg). |
| `PGBOUNCER_URL` | `$DATABASE_URL` | Runtime app → PgBouncer path. Defaults to `DATABASE_URL` when unset. |
| `SCHEDULER_JOBSTORE_URL` | `$DATABASE_URL` | URL for APScheduler's SQLAlchemyJobStore (durable job rows). Ignored when `SCHEDULER_USE_MEMORY_JOBSTORE=true`. |
| `SCHEDULER_USE_MEMORY_JOBSTORE` | `false` | Set `true` to use APScheduler's in-process MemoryJobStore. Jobs are lost on restart. Useful for local dev. |

---

## Embedding

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformer model name. Set to `stub` to skip download and use zero-vector embeddings (smoke tests only). |
| `EMBEDDING_CHUNK_TOKENS` | `400` | Token budget per chunk when splitting fact bodies for embedding. |
| `EMBEDDING_CACHE_MAXSIZE` | `10000` | LRU cache size for previously-embedded chunks. |

---

## Outbox + drain

| Variable | Default | Description |
|---|---|---|
| `OUTBOX_POLL_INTERVAL_S` | `5` | Drain interval (seconds) for the embedding outbox. |
| `OUTBOX_BATCH_SIZE` | `32` | Max rows claimed per drain pass. |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Per-row retry ceiling before the outbox row moves to the dead-letter table. |
| `BACKFILL_BATCH_SIZE` | `64` | Page size for the backfill / reindex scripts. |

---

## Webhook delivery

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_DRAIN_INTERVAL_S` | `5` | Cadence for the WebhookDeliveryWorker drain job (seconds). The p95 SLO caps fan-out at 30 s; this default keeps well inside the SLO with headroom for retries. |
| `WEBHOOK_REQUEST_TIMEOUT_S` | `10.0` | Per-delivery HTTP timeout (seconds). |
| `WEBHOOK_BATCH_SIZE` | `50` | Max deliveries claimed per drain pass. |

---

## HTTP method routing

| Variable | Default | Description |
|---|---|---|
| `REGISTRY_HTTP_METHODS_MODE` | `rest` | `rest` — register standard verbs (PATCH / DELETE). `post_only` — register only POST-tunneled aliases. `both` — register both. Use `post_only` or `both` for deployments behind enterprise proxies that strip non-GET/POST verbs. |
| `REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR` | `colon` | Separator in POST-tunneled aliases. `colon` → `/{id}:update`. `slash` → `/{id}/update`. |

---

## Authentication — JWT validation

The registry accepts OIDC JWT credentials only. There is no opaque-bearer / API-token path. See [Authentication](../01-overview/04-authentication.md) for the full claim contract.

| Variable | Default | Required when | Description |
|---|---|---|---|
| `OIDC_DISCOVERY_URL` | — | always (auth enabled) | OpenID Connect discovery document URL. Validator reads `issuer`, `jwks_uri`, and supported algorithms from this doc. |
| `OIDC_ISSUER_ALLOWLIST` | empty (legacy) | production | Comma-separated list of acceptable `iss` claim values. Tokens with a non-allowlisted issuer are rejected even if the signature validates. Empty = no allowlisting (NOT recommended in production). |
| `RESOURCE_URI_ALLOWLIST` | empty (legacy) | production | Comma-separated list of acceptable `aud` claim values. ADFS carries the resource URI here; this lists what this deployment will accept. |
| `OIDC_EXPECTED_AUDIENCE` | — | optional | Legacy single-value `aud` check. Superseded by `RESOURCE_URI_ALLOWLIST`; still honored when the allowlist is empty. |
| `OIDC_CLIENT_ID_ALLOWLIST` | empty | optional | Comma-separated list of acceptable `azp` / `client_id` values. Empty = check skipped (NOT recommended in production — any token from a trusted JWKS would pass). |
| `OIDC_MAX_TOKEN_TTL_SECONDS` | `900` | always | Registry-enforced upper bound on token lifetime. Tokens where `exp - iat` exceeds this — or where `iat` is absent — are rejected. Defense-in-depth against IdP misconfiguration issuing long-lived tokens. |

## Authentication — entitlement-service grant resolution

Once the JWT validates, the registry resolves grants by calling an external entitlement service keyed on the JWT's `sub`. See [Authorization](../01-overview/05-authorization.md) for the grant flow.

| Variable | Default | Required when | Description |
|---|---|---|---|
| `ENTITLEMENT_SERVICE_URL` | empty | always (auth enabled) | Base URL of the entitlement service. Setting this enables the entitlement-resolution path; the four fields below all become required (`Settings.__post_init__` raises otherwise). |
| `ENTITLEMENT_SERVICE_ENV` | empty | `ENTITLEMENT_SERVICE_URL` set | `env` query parameter passed to the entitlement service. Typically `PRD`, `NPD`, or `DEV`. |
| `ENTITLEMENT_SERVICE_DISCRIMINATOR` | empty | `ENTITLEMENT_SERVICE_URL` set | Per-deployment middle token of the entitlement grammar `<tenant_slug>_<DISCRIMINATOR>_<ROLE>`. Multiple registry-shaped services can share one entitlement endpoint with different discriminators (`REGISTRY`, `GRAPHREGISTRY`, …). Must be non-empty with no whitespace. |
| `ENTITLEMENT_ROLE_MAPPING` | empty dict | `ENTITLEMENT_SERVICE_URL` set | Comma-separated `EXTERNAL:internal` pairs mapping the upstream role suffix to one of `admin / producer / consumer / auditor`. Multiple external suffixes may map to the same internal role (covers LDAP rename rollouts). Example: `ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor`. |
| `ENTITLEMENT_CONNECT_TIMEOUT_MS` | `250` | optional | TCP/TLS connect timeout to the entitlement service (milliseconds). Bounded because this call sits in the auth hot path on every cache miss. |
| `ENTITLEMENT_READ_TIMEOUT_MS` | `1500` | optional | Read timeout for the entitlement-service response (milliseconds). |
| `ENTITLEMENT_MAX_RETRIES` | `1` | optional | Maximum retries on network failure / 5xx / 429. Must be 0 or 1. |
| `ENTITLEMENT_CACHE_MAX_ENTRIES` | `10000` | optional | Per-process in-memory cache size for resolved grants. Per-entry TTL is bounded by the JWT's own `exp` claim, not by a separate setting. |

---

## Progression

| Variable | Default | Description |
|---|---|---|
| `PROGRESSION_DEFINITION_CACHE_TTL_SECONDS` | `60` | TTL (seconds) for the cached progression-definition lookup. `0` disables caching. Short TTL keeps the cache fresh after operator edits without a restart. |

---

## Rate limiting

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Set `false` / `0` / `no` to disable enforcement without redeploying. |
| `RATE_LIMIT_READ_PER_MINUTE` | `600` | Per-tenant read budget (GET/HEAD) per minute, per process. In a multi-process deployment the effective limit across N workers is up to N × this value. |
| `RATE_LIMIT_WRITE_PER_MINUTE` | `60` | Per-tenant write budget (POST/PUT/PATCH/DELETE) per minute, per process. |
| `DEFAULT_READS_PER_SECOND` | `100` | Per-tenant default read RPS (Postgres advisory-lock gate). Tenants can override via the `rate_limits` table. |
| `DEFAULT_WRITES_PER_SECOND` | `10` | Per-tenant default write RPS. |

---

## Observability

| Variable | Default | Description |
|---|---|---|
| `OTLP_ENDPOINT` | — | OTLP HTTP endpoint for trace export (Jaeger, Honeycomb, Tempo, OTel Collector). Omit to disable tracing. Example: `http://otel-collector:4318/v1/traces`. |
| `SERVICE_NAME` | `registry` | Service name used in OTel resource attributes. |
| `QUERY_LATENCY_WARN_MS` | `500.0` | Slow-query warning threshold (milliseconds). Queries beyond this emit a WARNING log. |

---

## External sync

| Variable | Default | Description |
|---|---|---|
| `CONNECTOR_RUN_TIMEOUT_S` | `300` | Per-connector run timeout (seconds). Applies to the full connector coroutine including pagination. |
| `GITHUB_WEBHOOK_SECRET` | — | Webhook secret for GitHub ingest. Set in your deployment secret store; not committed. Read directly by `sync/webhook.py` (not via `Settings`) to support per-instance rotation without a reload. |
| `GITLAB_WEBHOOK_SECRET` | — | Webhook secret for GitLab ingest. Same pattern as `GITHUB_WEBHOOK_SECRET`. |

Per-connector credentials are not listed here — they are resolved by a dynamic reference string at runtime. Set them in your deployment's secret store under the names the connector definitions request.

---

## Performance / partitioning

| Variable | Default | Description |
|---|---|---|
| `EMBEDDINGS_PARTITION_COUNT` | `8` | Partition fan-out for the HASH-partitioned embeddings table. Changing this after initial setup requires a partition migration. |

---

## Closure refresh worker

| Variable | Default | Description |
|---|---|---|
| `CLOSURE_REFRESH_CONCURRENCY` | `8` | Max concurrent outbox-row processing tasks per drain cycle. Each concurrent task holds one DB connection. Keep below your PgBouncer pool size divided by the number of worker processes. |

---

## Deployment patterns

The env vars are the same regardless of deployment target. The only thing that varies is how you set them:

| Target | Mechanism |
|---|---|
| Docker Compose (local) | `docker-compose.yml` `environment:` section or `.env` file |
| Kubernetes | `ConfigMap` for non-secrets + `Secret` for secrets, both mounted as env vars |
| AWS ECS / Fargate | Task definition `environment` + `secrets` (from Secrets Manager or Parameter Store) |
| AWS Lambda | Function environment variables |
| EC2 / systemd | `EnvironmentFile=/etc/registry/env` (chmod 600, root-owned) |
| Google Cloud Run | Service environment variables + Secret Manager for secrets |

**Never commit secrets.** Database passwords, webhook secrets, OIDC client secrets, and API tokens are always operator-provided at deploy time, never checked into the repository.
