# Disaster Recovery and Operations Runbook

## Audit Log Partition Archival Procedure (TDD §10.5a)

**Policy:** Monthly `audit_log` partitions older than 24 months must be detached and archived by an operator. No automated archival occurs — this is an intentional operator gate.

**Trigger:** The service logs a `WARNING` at startup and emits the Prometheus gauge `catalog_audit_partitions_eligible_for_archival > 0` when any child partition of `audit_log` has a `from` bound older than 24 months. The gauge is re-evaluated every hour via the APScheduler job `audit_partition_check`.

**Prerequisites:**

- Postgres 14+ (required for `DETACH PARTITION ... CONCURRENTLY`)
- `pg_dump` available on the operator workstation or in the ops container
- Write access to the object storage bucket used for archived dumps
- An active DB session with superuser or table-owner privileges

### Step 1 — Identify the oldest eligible partition

```sql
SELECT
    c.relname                                          AS partition_name,
    pg_get_expr(c.relpartbound, c.oid, true)           AS bounds,
    age(now(), lower(
        pg_get_expr(c.relpartbound, c.oid, true)::text::anyrange
    ))                                                 AS approx_age
FROM   pg_inherits i
JOIN   pg_class c ON c.oid = i.inhrelid
JOIN   pg_class p ON p.oid = i.inhparent
WHERE  p.relname = 'audit_log'
ORDER  BY c.relname;
```

Identify partitions whose `from` bound is before `now() - interval '24 months'`.
Example: if today is 2026-05-07, partitions with names like `audit_log_2024_04` and earlier are eligible.

### Step 2 — Detach the partition (non-blocking, Postgres 14+)

```sql
ALTER TABLE audit_log
    DETACH PARTITION audit_log_YYYY_MM CONCURRENTLY;
```

Replace `audit_log_YYYY_MM` with the specific partition name (e.g. `audit_log_2024_03`).

- `CONCURRENTLY` avoids an `ACCESS EXCLUSIVE` lock on the parent table. The operation takes a `SHARE UPDATE EXCLUSIVE` lock on the child and a `SHARE ROW EXCLUSIVE` lock on the parent — reads and writes to other partitions are unaffected.
- One partition at a time. Do not detach multiple partitions in a single transaction.

### Step 3 — Archive via pg_dump and drop after verification

```bash
# Dump the detached partition to object storage
pg_dump \
  --table=audit_log_YYYY_MM \
  --format=custom \
  --file=/tmp/audit_log_YYYY_MM.dump \
  "$DATABASE_URL"

# Upload to object storage (example: S3-compatible)
aws s3 cp /tmp/audit_log_YYYY_MM.dump \
  s3://YOUR_ARCHIVE_BUCKET/audit-log/audit_log_YYYY_MM.dump

# Verify: row count in dump must match the table
pg_restore --list /tmp/audit_log_YYYY_MM.dump | head
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM audit_log_YYYY_MM"

# Drop the detached table only after dump verification
psql "$DATABASE_URL" -c "DROP TABLE audit_log_YYYY_MM"

# Remove the local temp dump
rm /tmp/audit_log_YYYY_MM.dump
```

### Step 4 — Record archival event in `episodes`

Insert a housekeeping episode so the archival is traceable via the audit trail:

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
    'Archived audit_log_YYYY_MM; dump at s3://YOUR_ARCHIVE_BUCKET/audit-log/audit_log_YYYY_MM.dump',
    now(),
    now()
);
```

---

## Partition Cutover Procedure (TDD §10.2)

See `scripts/partition_migrate.py` for the zero-downtime cutover of `audit_log`, `episodes`, and `embeddings` to their partitioned forms. Run with `--dry-run` first.

## Downgrade Notes

See the header comment in `scripts/partition_migrate.py` for the manual rollback procedure for each table.

---

## Backup Configuration (RPO Target: ≤ 1 hour)

### WAL Archiving

Configure Postgres WAL archiving so that point-in-time recovery is possible with a maximum data loss of one hour.

Edit `postgresql.conf` (or set via environment/Helm values):

```ini
# Enable WAL archiving — required for PITR
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://YOUR_WAL_BUCKET/wal/%f'

# Ensure WAL segments are flushed to the archive at least every 55 minutes,
# which keeps RPO inside the ≤ 1 h target even under a low-write workload.
archive_timeout = 3300   # seconds (55 min)
```

Restart Postgres after changing `wal_level` or `archive_mode` (both require a
server restart — they cannot be reloaded with `pg_reload_conf()`).

Verify archiving is active:

```sql
SELECT pg_walfile_name(pg_current_wal_lsn()),
       last_archived_wal,
       last_archived_time,
       last_failed_wal
FROM   pg_stat_archiver;
```

`last_failed_wal` must be `NULL`. If it is set, fix the archive command before
proceeding.

### Base Backup

A base backup is required as the starting point for any PITR restore. Take one
immediately after enabling WAL archiving, then on a recurring schedule:

```bash
# Take a base backup (pg_basebackup streams WAL in parallel by default)
pg_basebackup \
  --host=DB_HOST \
  --port=5432 \
  --username=postgres \
  --pgdata=/tmp/base_backup \
  --format=tar \
  --gzip \
  --wal-method=stream \
  --checkpoint=fast \
  --label="catalog-$(date +%Y%m%d)"

# Upload to object storage
aws s3 sync /tmp/base_backup/ s3://YOUR_WAL_BUCKET/base/$(date +%Y%m%d)/

# Clean up local copy
rm -rf /tmp/base_backup
```

### Daily Logical Backup (supplemental)

In addition to WAL archiving, take a daily logical dump of the database. This
provides a human-readable, restorable snapshot that is independent of the
physical backup format and Postgres version.

```bash
# Logical dump — runs daily via cron or Kubernetes CronJob
pg_dump \
  --format=custom \
  --compress=9 \
  --file=/tmp/registry-$(date +%Y%m%d).dump \
  "$DATABASE_URL"

# Upload
aws s3 cp /tmp/registry-$(date +%Y%m%d).dump \
  s3://YOUR_ARCHIVE_BUCKET/logical/registry-$(date +%Y%m%d).dump

# Verify dump is readable
pg_restore --list /tmp/registry-$(date +%Y%m%d).dump | wc -l

# Clean up
rm /tmp/registry-$(date +%Y%m%d).dump
```

Recommended schedule: `0 02 * * *` (02:00 UTC daily).

Retention: keep logical dumps for 30 days; keep WAL archives and base backups
for 14 days (adjust based on your data-recovery SLA).

---

## Restore Procedure (RTO Target: ≤ 4 hours)

The following procedure restores the database from a base backup + WAL replay
up to a given point in time. For a full-cluster failure, expect the restore
to complete within 4 hours for a database up to 100 GB.

### Step 1 — Stop the application (≤ 5 min)

Scale the catalog deployment to zero replicas before beginning restore to
prevent split-brain writes:

```bash
kubectl scale deployment capability-fabric --replicas=0 -n catalog
```

### Step 2 — Identify the target recovery time and base backup (≤ 10 min)

List available base backups:

```bash
aws s3 ls s3://YOUR_WAL_BUCKET/base/ --recursive | sort
```

Choose the newest base backup whose timestamp is before the target recovery
time. Record the `backup_label` timestamp from the listing.

### Step 3 — Restore the base backup to a new Postgres data directory (≤ 30 min)

```bash
# On the recovery host, as the postgres OS user:
mkdir -p /var/lib/postgresql/restore
chmod 700 /var/lib/postgresql/restore

# Download the base backup
aws s3 sync s3://YOUR_WAL_BUCKET/base/YYYYMMDD/ /tmp/base_restore/

# Extract
cd /var/lib/postgresql/restore
tar -xzf /tmp/base_restore/base.tar.gz
```

### Step 4 — Configure WAL replay (≤ 10 min)

Create a `postgresql.auto.conf` (or `recovery.conf` for Postgres ≤ 11) with
the restore command and target time:

```ini
# In /var/lib/postgresql/restore/postgresql.auto.conf

restore_command = 'aws s3 cp s3://YOUR_WAL_BUCKET/wal/%f %p'

# Optional: stop at a specific time (omit to replay all available WAL)
recovery_target_time = '2026-05-07 03:00:00 UTC'
recovery_target_action = 'promote'
```

Create the recovery signal file (Postgres 12+):

```bash
touch /var/lib/postgresql/restore/recovery.signal
```

### Step 5 — Start Postgres and monitor WAL replay (≤ 60 min for 14 days of WAL)

```bash
pg_ctl start -D /var/lib/postgresql/restore -l /var/log/postgresql/restore.log

# Tail the log to confirm WAL segments are downloading and replaying
tail -f /var/log/postgresql/restore.log | grep -E 'restored|recovery|promoted'
```

Postgres will emit `LOG: database system is ready to accept connections` once
recovery is complete and the instance is promoted.

### Step 6 — Verify data integrity (≤ 20 min)

```sql
-- Confirm partition attachment and row counts look sane
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
curl -f http://RESTORE_HOST:8000/healthz
```

### Step 7 — Reattach archived partitions if needed (variable)

**Important — partition table interaction:** A physical restore only includes
partitions that were attached to the parent table at the time the base backup
was taken. Any partition that was detached (archived) after the base backup was
created will not be present in the restored cluster.

If your recovery point is before any archival events you can skip this step.
Otherwise, for each archived partition that should be visible:

```bash
# Download the archived partition dump
aws s3 cp s3://YOUR_ARCHIVE_BUCKET/audit-log/audit_log_YYYY_MM.dump /tmp/audit_log_YYYY_MM.dump

# Restore the standalone table
pg_restore \
  --dbname="$DATABASE_URL" \
  --no-owner \
  --no-privileges \
  /tmp/audit_log_YYYY_MM.dump

# Re-attach to the parent
psql "$DATABASE_URL" -c "
    ALTER TABLE audit_log
        ATTACH PARTITION audit_log_YYYY_MM
        FOR VALUES FROM ('YYYY-MM-01') TO ('YYYY-MM+1-01');
"

rm /tmp/audit_log_YYYY_MM.dump
```

See also: [Audit Log Partition Archival Procedure](#audit-log-partition-archival-procedure-tdd-105a)
for the inverse operation (detach + archive).

### Step 8 — Cut over traffic and scale up (≤ 10 min)

Update the `DATABASE_URL` environment variable or Helm values to point to the
restored instance, then scale the deployment back up:

```bash
kubectl set env deployment/capability-fabric DATABASE_URL="postgresql+asyncpg://..." -n catalog
kubectl scale deployment capability-fabric --replicas=2 -n catalog
kubectl rollout status deployment/capability-fabric -n catalog
```

**Total estimated RTO:** 5 + 10 + 30 + 10 + 60 + 20 + variable + 10 = ~2.5 h
for a 100 GB database with 14 days of WAL. This satisfies the RTO ≤ 4 h target.

---

## Quarterly Restore Drill Checklist

Perform a full restore drill once per quarter in a non-production environment
to validate the backup chain and operator familiarity with the procedure.

**Checklist:**

- [ ] Confirm `pg_stat_archiver.last_failed_wal` is NULL on production (WAL
      archiving healthy).
- [ ] Download the latest base backup to the drill host.
- [ ] Restore base backup to a fresh data directory (Step 3 above).
- [ ] Configure WAL replay to a target time one hour before drill start
      (Step 4 above).
- [ ] Start Postgres and confirm `recovery.signal` is removed automatically on
      promotion (Step 5 above).
- [ ] Run SQL integrity checks: row counts, partition listing (Step 6 above).
- [ ] Run `/healthz` smoke test against the restored instance (Step 6 above).
- [ ] Verify that re-attaching one archived partition works end-to-end (Step 7
      above).
- [ ] Record actual elapsed time and compare against RTO ≤ 4 h target.
- [ ] Document any gaps or failures in the incident log and address before next
      quarter.
- [ ] Destroy the drill environment after sign-off (no stale credentials or
      data copies left).

**Schedule:** Last Friday of March, June, September, and December.
**Owner:** On-call operator for that week.
**Sign-off required by:** Engineering lead.

Record drill results in the team incident log with the tag `dr-drill-YYYY-QN`
(e.g. `dr-drill-2026-Q2`).

---

## Appendix A — Dev DB name change (`fabric` → `catalog`)

The dev compose stack renamed the Postgres database from `fabric` to `catalog`
as part of CAP-RENAME-T07. If you have a dev instance with valuable data in the
`fabric` database, **preserve it before wiping the volume**:

```bash
# 1. Dump existing fabric DB before bringing the stack down.
docker compose exec postgres pg_dump -U postgres fabric > /tmp/registry_backup.sql

# 2. Destroy the dev volume and recreate the stack. POSTGRES_DB=catalog now,
#    so postgres initializes the new database on startup.
docker compose down -v
docker compose up -d

# 3. Wait for postgres to become healthy, then restore your dump.
docker compose exec -T postgres psql -U postgres catalog < /tmp/registry_backup.sql

# 4. Apply any new migrations on top of the restored data.
docker compose exec api alembic upgrade head
```

If you have no data worth preserving (the common dev case), skip steps 1 and 3
and just run `docker compose down -v && docker compose up -d`. The api container
will run `alembic upgrade head` on its first start.

Production environments are out of scope for this appendix — coordinate any
production DB rename with the operator on call. Never run `docker compose
down -v` against a production volume.
