# Operations Runbook

Routine and emergency procedures for operators with database access and admin API credentials. For progression-definition operations, see [progression.md](02-progression.md).

---

## Audit log partition archival

### Background

The `audit_log` table is range-partitioned by month. The service checks partition ages hourly and emits a Prometheus gauge and a WARNING log when any child partition's lower bound is older than 24 months:

- **Gauge:** `catalog_audit_partitions_eligible_for_archival`
- **Log level:** `WARNING`, with the list of eligible partition names
- **Threshold:** 24 months (fixed; re-evaluate retention period at each compliance review)

No automatic archival occurs — this is an intentional operator gate.

**Alert threshold:** Alert when `catalog_audit_partitions_eligible_for_archival > 0`.

### Prerequisites

- Postgres 14+ (`DETACH PARTITION ... CONCURRENTLY` requires 14+)
- `pg_dump` on the operator workstation or in an ops container
- Write access to your object-storage bucket for archived dumps
- A database session with superuser or table-owner privileges

### Step 1 — Identify eligible partitions

```sql
SELECT c.relname AS partition_name
FROM   pg_inherits i
JOIN   pg_class c ON c.oid = i.inhrelid
JOIN   pg_class p ON p.oid = i.inhparent
WHERE  p.relname = 'audit_log'
ORDER  BY c.relname;
```

Partitions named `audit_log_YYYY_MM` where the year/month is more than 24 months before today are eligible. Example: if today is 2026-05-12, `audit_log_2024_04` and earlier are eligible.

### Step 2 — Dump the partition to object storage

```bash
pg_dump \
  --table=audit_log_2024_04 \
  --format=custom \
  --no-owner \
  --no-acl \
  "$DATABASE_URL" \
  > audit_log_2024_04_$(date +%Y%m%d).pgdump

# Upload to your object-storage bucket
aws s3 cp audit_log_2024_04_$(date +%Y%m%d).pgdump \
  s3://<your-archive-bucket>/registry/audit_log/

# OR: gcloud storage cp, az storage blob upload, etc.
```

Verify the dump is readable before detaching:

```bash
pg_restore --list audit_log_2024_04_$(date +%Y%m%d).pgdump | head -5
```

### Step 3 — Detach the partition (non-blocking, Postgres 14+)

```sql
ALTER TABLE audit_log
  DETACH PARTITION audit_log_2024_04 CONCURRENTLY;
```

`CONCURRENTLY` allows reads and writes on the parent table to continue during the detach. It acquires a brief exclusive lock only at the end. Without `CONCURRENTLY` (Postgres 13 and earlier), the detach blocks all reads and writes on `audit_log`.

### Step 4 — Verify the parent table is intact

```sql
-- Count rows in the parent (should not change)
SELECT COUNT(*) FROM audit_log;

-- Confirm the detached partition no longer appears in pg_inherits
SELECT c.relname
FROM   pg_inherits i
JOIN   pg_class c ON c.oid = i.inhrelid
JOIN   pg_class p ON p.oid = i.inhparent
WHERE  p.relname = 'audit_log'
  AND  c.relname = 'audit_log_2024_04';
-- Should return 0 rows
```

### Step 5 — Drop or keep the detached table

The detached partition is now an ordinary table. You may drop it once you've confirmed the dump is safely archived:

```sql
DROP TABLE audit_log_2024_04;
```

Or keep it around for a grace period. If you keep it, rename it to avoid confusion:

```sql
ALTER TABLE audit_log_2024_04 RENAME TO _archived_audit_log_2024_04;
```

### Step 6 — Record the archival event

Insert a housekeeping row into `episodes` so the operation is traceable via the audit trail:

```sql
INSERT INTO episodes (
    episode_id,
    tenant_id,
    episode_type,
    source_id,
    content_summary,
    ts,
    ingested_at
) VALUES (
    gen_random_uuid(),
    '00000000-0000-0000-0000-000000000000',  -- system tenant
    'audit_partition_archived',
    'ops/partition_archival',
    'Archived audit_log_2024_04; dump at s3://<your-archive-bucket>/registry/audit_log/audit_log_2024_04_<date>.pgdump',
    now(),
    now()
);
```

### Restore from archive (if needed)

To restore a dumped partition:

```bash
# Download the dump
aws s3 cp \
  s3://<your-archive-bucket>/registry/audit_log/audit_log_2024_04_<date>.pgdump \
  /tmp/audit_log_2024_04.pgdump

# Restore as a new table (not re-attached automatically)
pg_restore \
  --dbname="$DATABASE_URL" \
  --table=audit_log_2024_04 \
  /tmp/audit_log_2024_04.pgdump
```

To re-attach as a partition:

```sql
ALTER TABLE audit_log
  ATTACH PARTITION audit_log_2024_04
  FOR VALUES FROM ('2024-04-01') TO ('2024-05-01');
```

---

## Rotating webhook secrets

**Why rotate:** if a subscription secret is compromised or you are following your organization's routine credential-rotation policy.

### Subscription webhook secret

Subscription secrets are stored per-subscription in the database. To rotate:

1. Generate a new secret (keep it secret — it is the HMAC signing key):

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Update the subscription via the admin API:

   ```bash
   curl -X PATCH \
     "https://api.example.com/v1/admin/tenants/<tenant_id>/subscriptions/<subscription_id>" \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"webhook_secret": "<new-secret>"}'
   ```

3. Update the subscriber's endpoint to verify the new secret. Until the subscriber is updated, deliveries will have a valid signature under the new secret but the subscriber's verification code will reject them. Plan the cutover to minimise the verification gap (or briefly accept both secrets in the subscriber code during the transition).

4. Verify the signature format:

   Webhook deliveries include the header `X-Registry-Signature-256: sha256=<hex>`. The HMAC is computed over the raw request body with the subscription's secret using SHA-256. Verify:

   ```python
   import hmac, hashlib
   expected = "sha256=" + hmac.new(
       secret.encode(), body, hashlib.sha256
   ).hexdigest()
   assert hmac.compare_digest(expected, received_header)
   ```

### Sync webhook secrets (GitHub / GitLab)

The `GITHUB_WEBHOOK_SECRET` and `GITLAB_WEBHOOK_SECRET` env vars are read directly by the sync layer on each request — not cached at startup — so rotation does not require an app restart:

1. Generate a new secret.
2. Update the secret in your deployment's secret store (Kubernetes Secret, AWS Secrets Manager, etc.).
3. If your platform re-injects env vars without a restart, the new secret takes effect immediately. If not, perform a rolling restart.
4. Update the corresponding webhook configuration in GitHub or GitLab to use the new secret.

---

## Log output and trace correlation

### Breaking change — log format is now JSON

The service previously emitted unformatted plain-text lines via Python's default
stdlib logging handler (no formatter configured). Log output is now a single JSON
object per line, written to stdout.

**Any log shipper configured to parse plain-text lines must be reconfigured.**
Set `LOG_FORMAT=text` as a temporary escape hatch while you update your shipper
pipeline; see [LOG_FORMAT=text guidance](#log_formattext-guidance) below.

### JSON field reference

Every log line in JSON mode contains the following keys. Keys are lowercase with
underscores. Optional fields are absent — not null — when the condition is not met.

| Field | Always present | Type | Description |
|---|---|---|---|
| `timestamp` | yes | string | ISO 8601 UTC timestamp: `2026-05-12T14:03:22.417456Z`. |
| `level` | yes | string | Lowercase severity: `debug`, `info`, `warning`, `error`, `critical`. |
| `logger` | yes | string | Module name from `logging.getLogger(__name__)`, e.g. `registry.workers.webhook_delivery`. |
| `event` | yes | string | Log message. Positional `%s` arguments are interpolated before structlog sees the record. |
| `trace_id` | conditional | string | 32-character lowercase hex OTel trace ID. Present only when the log line is emitted inside an active OTel span. |
| `span_id` | conditional | string | 16-character lowercase hex OTel span ID. Present only when inside an active span. |
| `exception` | conditional | string | Formatted traceback string. Present when `_log.exception(...)` or `exc_info=True` is used. Newlines within the traceback are serialized as `\n` escape sequences — the full line remains a single parseable JSON object. |

Example JSON line (inside a traced request):

```json
{"timestamp": "2026-05-12T14:03:22.417456Z", "level": "info", "logger": "registry.api.routers.entities", "event": "entity created", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7"}
```

### Platform-specific trace correlation

`trace_id` and `span_id` follow the OTel field-naming convention. Some platforms
need a one-time pipeline mapping step.

**Splunk**

Configure the log source type to JSON or add `INDEXED_EXTRACTIONS = json` to your
`inputs.conf` stanza. Once enabled, `trace_id` becomes a directly searchable index
field:

```
index=registry trace_id=4bf92f3577b34da6a3ce929d0e0e4736
```

**Dynatrace**

When the Dynatrace OneAgent log module is enabled, `trace_id` is recognized
automatically and linked to the corresponding distributed-trace record in the
Dynatrace UI. No additional pipeline mapping is required.

**Datadog**

Datadog's APM correlation requires the field names `dd.trace_id` and `dd.span_id`.
Configure a Datadog log pipeline processor to remap:

- `trace_id` → `dd.trace_id`
- `span_id` → `dd.span_id`

Once mapped, log lines correlate to APM traces in the Datadog UI. This is a
one-time pipeline configuration step; no application code change is needed.

**Grafana Loki**

Use the `json` parser stage in your Promtail or Alloy pipeline config to extract
`trace_id` and `span_id` as log labels or structured metadata fields. Example query
to pivot from a trace ID to its log lines:

```logql
{app="registry"} | json | trace_id="4bf92f3577b34da6a3ce929d0e0e4736"
```

### `LOG_FORMAT=text` guidance

Set `LOG_FORMAT=text` when:

- Running locally and you want human-readable, colour-coded output in the terminal.
- Operating in an environment where the log collector cannot parse JSON (e.g. a
  legacy syslog forwarder or a CI system whose log capture strips JSON structure).
- Debugging a live issue where multi-line tracebacks are easier to read than
  JSON-escaped strings.

In `text` mode the output is not parseable as JSON. Trace IDs and span IDs are
still present in the log lines but are formatted for human readability rather than
machine extraction. Do not use `text` mode in production environments that route
logs to a structured shipper.

### Local development

Add the following to your local `.env` file for a comfortable development experience:

```
LOG_FORMAT=text     # human-readable output; avoids JSON noise in your terminal
LOG_LEVEL=DEBUG     # optional — surfaces SQLAlchemy query strings and OTel SDK
                    # internals; high-volume; use only when diagnosing issues
```

`LOG_LEVEL=DEBUG` makes the root logger emit records from every dependency
(SQLAlchemy, httpx, FastAPI, OTel SDK). This is useful for tracing an unexpected
query or header, but expect significantly more output than `INFO`. Reserve `DEBUG`
for targeted diagnosis sessions, not always-on development.

---

## Replaying failed webhook deliveries

The `notification_deliveries` table tracks every attempted delivery. Rows with `status='failed'` have exhausted retries (failed on a 4xx that is not 408 or 429). Rows with `status='pending'` and `next_retry_at` in the future are still in the retry queue.

### Identify failed deliveries

```sql
SELECT
    d.id,
    d.subscription_id,
    d.notification_id,
    d.status,
    d.attempt_count,
    d.last_attempt_at,
    d.last_error
FROM notification_deliveries d
WHERE d.tenant_id = '<tenant_uuid>'
  AND d.status = 'failed'
ORDER BY d.last_attempt_at DESC
LIMIT 50;
```

### Replay a delivery

To reset a failed delivery so the worker will retry it:

```sql
UPDATE notification_deliveries
SET
    status          = 'pending',
    next_retry_at   = now(),
    attempt_count   = 0,
    last_error      = NULL
WHERE id = '<delivery_uuid>'
  AND tenant_id = '<tenant_uuid>';
```

The worker picks it up on the next drain pass (at most `WEBHOOK_DRAIN_INTERVAL_S` seconds, default 5). If the subscriber endpoint is still returning 4xx errors, the delivery will fail again permanently.

### Bulk replay by subscription

```sql
UPDATE notification_deliveries
SET
    status          = 'pending',
    next_retry_at   = now(),
    attempt_count   = 0,
    last_error      = NULL
WHERE subscription_id = '<subscription_uuid>'
  AND tenant_id       = '<tenant_uuid>'
  AND status          = 'failed';
```

---

## Refreshing the closure cache

The `closure_cache` table holds the pre-computed transitive closure of entity edges. It is warmed lazily via the `closure_outbox` — edge mutations enqueue a refresh row, and the `ClosureRefreshWorker` processes them. Reads fall back to a recursive CTE when the cache is cold.

### When to manually trigger a rebuild

- After a bulk import of edges that bypassed the outbox pattern.
- After a `TRUNCATE closure_cache` (note: `TRUNCATE` does not seed the outbox — rows stay warm via natural edge mutations).
- When blast-radius queries are unexpectedly slow and `cache_hit: false` is appearing in results.

### Check cache health

```sql
-- Count cached closures
SELECT COUNT(*) FROM closure_cache WHERE tenant_id = '<tenant_uuid>';

-- Check outbox backlog
SELECT COUNT(*) FROM closure_outbox WHERE tenant_id = '<tenant_uuid>';

-- Check recent refresh timestamps
SELECT
    MIN(refreshed_at) AS oldest_entry,
    MAX(refreshed_at) AS newest_entry,
    COUNT(*)          AS total_rows
FROM closure_cache
WHERE tenant_id = '<tenant_uuid>';
```

### Force a rebuild for a specific entity

Insert an outbox row to trigger a refresh for one entity:

```sql
INSERT INTO closure_outbox (id, tenant_id, entity_id, edge_op, created_at)
VALUES (gen_random_uuid(), '<tenant_uuid>', '<entity_uuid>', 'upsert', now())
ON CONFLICT DO NOTHING;
```

The worker picks this up on the next drain cycle and upserts the full forward and reverse closure for that entity.

### Clear stale cache entries

The nightly maintenance job deletes closure rows with `refreshed_at < now() - 90 days`. To run this manually:

```sql
DELETE FROM closure_cache
WHERE refreshed_at < now() - interval '90 days';
```

---

## Tenant onboarding

Onboarding a new production tenant requires two steps: creating the tenant record and seeding its vocabulary.

### Step 1 — Create the tenant

Via the admin API (requires an existing admin-level token):

```bash
curl -X POST \
  "https://api.example.com/v1/admin/tenants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "<tenant-slug>",
    "display_name": "<Tenant Display Name>"
  }'
```

The response includes the `tenant_id` UUID. Save it — you need it for subsequent calls.

### Step 2 — Seed vocabulary

Closed-vocabulary values (entity types, edge relationship types, lifecycle states, visibility values) must be seeded before any entity can be created. Common values to seed:

```bash
# Seed entity types
for TYPE in service library component platform; do
  curl -X POST \
    "https://api.example.com/v1/admin/tenants/<tenant_id>/vocabulary" \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"vocab_type\": \"entity_type\", \"value\": \"$TYPE\"}"
done
```

Repeat for `lifecycle_state` values (`active`, `deprecated`, `archived`, `experimental`), `edge_rel` values, and any other closed vocabulary your deployment uses.

### Step 3 — Create roles and the first actor

```bash
# Mint an admin actor for the new tenant
curl -X POST \
  "https://api.example.com/v1/admin/tenants/<tenant_id>/actors" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"display_name": "tenant-admin", "email": "admin@example.com"}'
# → returns actor_id

# Mint a token for that actor
python scripts/mint_token.py \
  --tenant-id <tenant_uuid> \
  --actor-id <actor_uuid> \
  --roles admin --roles producer --roles consumer \
  --description 'initial admin token'
```

The script prints the plaintext token exactly once. Store it in your secret management system immediately.

---

## Applying database migrations

Migrations use Alembic and must be applied before or alongside a service deployment.

```bash
export DATABASE_URL=postgresql+asyncpg://user:password@host:5432/registry
make migrate        # equivalent to: alembic upgrade head
```

**Before applying in production:**

1. Take a database snapshot/backup.
2. Review the migration files (`registry/registry/storage/migrations/versions/`) to understand what schema changes are being applied.
3. Apply during a maintenance window for destructive migrations (column drops, table renames).

**Rolling back:** Alembic supports downgrade steps for each migration. To roll back one revision:

```bash
cd registry && alembic downgrade -1
```

Not all migrations have a downgrade path — check the migration file before assuming a downgrade is safe.

---

## Backfilling and reindexing embeddings

When you change the embedding model (`EMBEDDING_MODEL`) or when bulk-imported entities are missing embeddings, run the backfill script:

```bash
python registry/scripts/backfill_embeddings.py
```

To reindex all existing embeddings with the current model (destructive — drops and rebuilds):

```bash
python registry/scripts/reindex_embeddings.py
```

Both scripts use `BACKFILL_BATCH_SIZE` (default 64) to control page size and require `DATABASE_URL`. They are safe to run while the service is live — they use the same outbox pattern as the online drain.

---

## Disaster recovery

### Backup configuration

The service uses Postgres as its sole storage backend. Point-in-time recovery requires two components configured by the operator before an incident occurs: WAL archiving and periodic base backups.

**WAL archiving** — edit `postgresql.conf` (or set via Helm values):

```ini
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://<your-wal-bucket>/wal/%f'

# Flush WAL to the archive on a schedule even under low write volume.
# Set this to a value that satisfies your recovery-point objective.
archive_timeout = 3300   # seconds (55 min) — adjust to your RPO requirement
```

Restart Postgres after changing `wal_level` or `archive_mode` — these settings require a server restart and cannot be applied with `pg_reload_conf()`.

Verify archiving is active:

```sql
SELECT pg_walfile_name(pg_current_wal_lsn()),
       last_archived_wal,
       last_archived_time,
       last_failed_wal
FROM   pg_stat_archiver;
```

`last_failed_wal` must be `NULL`. If it is set, fix the archive command before taking a base backup.

**Base backup** — take one immediately after enabling WAL archiving, then on a recurring schedule:

```bash
pg_basebackup \
  --host=<DB_HOST> \
  --port=5432 \
  --username=postgres \
  --pgdata=/tmp/base_backup \
  --format=tar \
  --gzip \
  --wal-method=stream \
  --checkpoint=fast \
  --label="catalog-$(date +%Y%m%d)"

aws s3 sync /tmp/base_backup/ s3://<your-wal-bucket>/base/$(date +%Y%m%d)/
rm -rf /tmp/base_backup
```

**Daily logical backup** — supplemental to WAL archiving; provides a human-readable snapshot independent of the physical backup format:

```bash
pg_dump \
  --format=custom \
  --compress=9 \
  --file=/tmp/registry-$(date +%Y%m%d).dump \
  "$DATABASE_URL"

aws s3 cp /tmp/registry-$(date +%Y%m%d).dump \
  s3://<your-archive-bucket>/logical/registry-$(date +%Y%m%d).dump

# Verify the dump is readable before discarding the local copy
pg_restore --list /tmp/registry-$(date +%Y%m%d).dump | wc -l

rm /tmp/registry-$(date +%Y%m%d).dump
```

Recommended schedule: `0 02 * * *` (02:00 UTC daily). Retention periods (logical dumps, WAL archives, base backups) are operator-defined and must reflect your organization's data-recovery SLA.

### Point-in-time restore procedure

Use this procedure to restore the database from a base backup and WAL replay to a target point in time. Complete the backup-configuration steps above and confirm WAL archiving is healthy before an incident requires them.

**Step 1 — Stop the application**

Scale the service to zero replicas before beginning restore to prevent split-brain writes:

```bash
kubectl scale deployment capability-fabric --replicas=0 -n catalog
```

**Step 2 — Identify the target recovery time and base backup**

List available base backups:

```bash
aws s3 ls s3://<your-wal-bucket>/base/ --recursive | sort
```

Choose the newest base backup whose timestamp is before the target recovery time.

**Step 3 — Restore the base backup to a new Postgres data directory**

```bash
mkdir -p /var/lib/postgresql/restore
chmod 700 /var/lib/postgresql/restore

aws s3 sync s3://<your-wal-bucket>/base/YYYYMMDD/ /tmp/base_restore/

cd /var/lib/postgresql/restore
tar -xzf /tmp/base_restore/base.tar.gz
```

**Step 4 — Configure WAL replay**

Create `/var/lib/postgresql/restore/postgresql.auto.conf`:

```ini
restore_command = 'aws s3 cp s3://<your-wal-bucket>/wal/%f %p'

# Remove or comment out to replay all available WAL
recovery_target_time = '2026-05-07 03:00:00 UTC'
recovery_target_action = 'promote'
```

Create the recovery signal file (Postgres 12+):

```bash
touch /var/lib/postgresql/restore/recovery.signal
```

**Step 5 — Start Postgres and monitor WAL replay**

```bash
pg_ctl start -D /var/lib/postgresql/restore -l /var/log/postgresql/restore.log

tail -f /var/log/postgresql/restore.log | grep -E 'restored|recovery|promoted'
```

Postgres emits `LOG: database system is ready to accept connections` once recovery is complete and the instance is promoted.

**Step 6 — Verify data integrity**

```sql
SELECT
    c.relname   AS partition,
    pg_size_pretty(pg_relation_size(c.oid)) AS size
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_class p ON p.oid = i.inhparent
WHERE p.relname IN ('audit_log', 'episodes', 'embeddings')
ORDER BY p.relname, c.relname;

SELECT COUNT(*) FROM audit_log;
SELECT COUNT(*) FROM episodes;
SELECT COUNT(*) FROM embeddings;
```

Run the application smoke test:

```bash
curl -f http://<RESTORE_HOST>:8000/healthz
```

**Step 7 — Reattach archived partitions if needed**

A physical restore only includes partitions that were attached at the time the base backup was taken. Partitions detached and archived after the backup will not be present in the restored cluster. For each archived partition that should be visible:

```bash
aws s3 cp s3://<your-archive-bucket>/registry/audit_log/audit_log_YYYY_MM.dump /tmp/audit_log_YYYY_MM.dump

pg_restore \
  --dbname="$DATABASE_URL" \
  --no-owner \
  --no-privileges \
  /tmp/audit_log_YYYY_MM.dump

psql "$DATABASE_URL" -c "
    ALTER TABLE audit_log
        ATTACH PARTITION audit_log_YYYY_MM
        FOR VALUES FROM ('YYYY-MM-01') TO ('YYYY-MM+1-01');
"

rm /tmp/audit_log_YYYY_MM.dump
```

See [Audit log partition archival](#audit-log-partition-archival) for the inverse operation.

**Step 8 — Cut over traffic and scale up**

Update `DATABASE_URL` in your deployment configuration to point to the restored instance, then scale the service back up:

```bash
kubectl set env deployment/capability-fabric DATABASE_URL="postgresql+asyncpg://..." -n catalog
kubectl scale deployment capability-fabric --replicas=2 -n catalog
kubectl rollout status deployment/capability-fabric -n catalog
```

### Quarterly restore drill checklist

Perform a full restore drill once per quarter in a non-production environment to validate the backup chain and operator familiarity with the procedure.

- [ ] Confirm `pg_stat_archiver.last_failed_wal` is `NULL` on production (WAL archiving healthy).
- [ ] Download the latest base backup to the drill host.
- [ ] Restore base backup to a fresh data directory (Step 3 above).
- [ ] Configure WAL replay to a target time before drill start (Step 4 above).
- [ ] Start Postgres and confirm `recovery.signal` is removed automatically on promotion (Step 5 above).
- [ ] Run SQL integrity checks: row counts, partition listing (Step 6 above).
- [ ] Run `/healthz` smoke test against the restored instance (Step 6 above).
- [ ] Verify that reattaching one archived partition works end-to-end (Step 7 above).
- [ ] Record actual elapsed time and compare against your organization's RTO target.
- [ ] Document any gaps or failures in the incident log and address before the next quarter.
- [ ] Destroy the drill environment after sign-off.

**Schedule:** Last Friday of March, June, September, and December.
**Owner:** On-call operator for that week.
**Sign-off required by:** Engineering lead.

Record drill results in the team incident log with the tag `dr-drill-YYYY-QN` (e.g. `dr-drill-2026-Q2`).

---

## Appendix A — Dev database rename (`fabric` → `catalog`)

The dev compose stack renamed the Postgres database from `fabric` to `catalog`. If you have a dev instance with data worth preserving, do this before wiping the volume:

```bash
# 1. Dump the existing fabric database before bringing the stack down.
docker compose exec postgres pg_dump -U postgres fabric > /tmp/registry_backup.sql

# 2. Destroy the dev volume and recreate the stack.
#    POSTGRES_DB=catalog now, so Postgres initializes the new database on startup.
docker compose down -v
docker compose up -d

# 3. Wait for Postgres to become healthy, then restore your dump.
docker compose exec -T postgres psql -U postgres catalog < /tmp/registry_backup.sql

# 4. Apply any new migrations on top of the restored data.
docker compose exec api alembic upgrade head
```

If you have no data worth preserving, skip steps 1 and 3 and run `docker compose down -v && docker compose up -d`. The api container applies `alembic upgrade head` on its first start.

Production environments are out of scope for this appendix. Never run `docker compose down -v` against a production volume.
