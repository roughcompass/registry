"""Configuration surface for the registry service.

`get_settings()` reads from environment variables. Tests construct `Settings`
directly. No module-level singleton — wired by FastAPI DI like `Clock`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # --- Database ---
    # asyncpg requires prepared_statement_cache_size=0 for PgBouncer transaction mode — wired in storage/pg.py
    database_url: str
    pgbouncer_url: str

    # --- APScheduler ---
    scheduler_jobstore_url: str
    # Set True to force MemoryJobStore (unit tests, envs without psycopg2).
    # Auto-inferred by get_settings() when SCHEDULER_USE_MEMORY_JOBSTORE=true.
    scheduler_use_memory_jobstore: bool = False

    # --- Embedding ---
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_chunk_tokens: int = 400
    embedding_cache_maxsize: int = 10_000

    # --- Outbox ---
    outbox_poll_interval_s: int = 5
    outbox_batch_size: int = 32
    outbox_max_attempts: int = 5

    # --- Webhook delivery ---
    webhook_drain_interval_s: int = 5
    webhook_request_timeout_s: float = 10.0
    webhook_batch_size: int = 50

    # --- HTTP method routing ---
    # 'rest'     — only the standard verb (PATCH/DELETE) is registered.
    # 'post_only'— only the POST-tunneled alias (POST .../{id}:action) is registered.
    # 'both'     — both routes are registered for enterprise-gateway compatibility.
    # Default is 'rest': the POST-tunneled aliases are opt-in for deployments
    # behind proxies that strip non-GET/POST verbs.
    http_methods_mode: str = "rest"
    http_method_alias_separator: str = "colon"

    # --- Backfill / reindex scripts ---
    backfill_batch_size: int = 64

    # --- Auth ---
    oidc_discovery_url: str | None = None
    token_hash_algorithm: str = "sha256"

    # --- Rate limiting ---
    default_reads_per_second: int = 100
    default_writes_per_second: int = 10
    # In-process token-bucket limits (per tenant, per minute).  Separate
    # budgets for reads (GET/HEAD) and writes (POST/PUT/PATCH/DELETE).
    # Set rate_limit_enabled=False to disable enforcement without redeploying.
    rate_limit_enabled: bool = True
    rate_limit_write_per_minute: int = 60
    rate_limit_read_per_minute: int = 600

    # --- OTel ---
    otlp_endpoint: str | None = None
    service_name: str = "registry"

    # --- Sync ---
    connector_run_timeout_s: int = 300
    webhook_secret_github: str | None = None
    webhook_secret_gitlab: str | None = None

    # --- SLO ---
    query_latency_warn_ms: float = 500.0

    # --- Partitioning ---
    embeddings_partition_count: int = 8

    # --- Closure refresh worker ---
    # Max concurrent outbox-row processing tasks per drain cycle.
    # Each task opens its own DB session; keep this below your PgBouncer pool
    # size divided by the number of worker processes you run.
    closure_refresh_concurrency: int = 8


def get_settings() -> Settings:
    """Construct Settings from environment variables. Required vars must be set."""
    database_url = os.environ["DATABASE_URL"]
    pgbouncer_url = os.environ.get("PGBOUNCER_URL", database_url)
    scheduler_jobstore_url = os.environ.get("SCHEDULER_JOBSTORE_URL", database_url)
    scheduler_use_memory_jobstore = os.environ.get("SCHEDULER_USE_MEMORY_JOBSTORE", "").lower() in ("1", "true", "yes")

    return Settings(
        database_url=database_url,
        pgbouncer_url=pgbouncer_url,
        scheduler_jobstore_url=scheduler_jobstore_url,
        scheduler_use_memory_jobstore=scheduler_use_memory_jobstore,
        embedding_model=os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        embedding_chunk_tokens=int(os.environ.get("EMBEDDING_CHUNK_TOKENS", "400")),
        embedding_cache_maxsize=int(os.environ.get("EMBEDDING_CACHE_MAXSIZE", "10000")),
        outbox_poll_interval_s=int(os.environ.get("OUTBOX_POLL_INTERVAL_S", "5")),
        outbox_batch_size=int(os.environ.get("OUTBOX_BATCH_SIZE", "32")),
        outbox_max_attempts=int(os.environ.get("OUTBOX_MAX_ATTEMPTS", "5")),
        backfill_batch_size=int(os.environ.get("BACKFILL_BATCH_SIZE", "64")),
        oidc_discovery_url=os.environ.get("OIDC_DISCOVERY_URL"),
        default_reads_per_second=int(os.environ.get("DEFAULT_READS_PER_SECOND", "100")),
        default_writes_per_second=int(os.environ.get("DEFAULT_WRITES_PER_SECOND", "10")),
        rate_limit_enabled=os.environ.get("RATE_LIMIT_ENABLED", "true").lower() not in ("0", "false", "no"),
        rate_limit_write_per_minute=int(os.environ.get("RATE_LIMIT_WRITE_PER_MINUTE", "60")),
        rate_limit_read_per_minute=int(os.environ.get("RATE_LIMIT_READ_PER_MINUTE", "600")),
        otlp_endpoint=os.environ.get("OTLP_ENDPOINT"),
        service_name=os.environ.get("SERVICE_NAME", "registry"),
        connector_run_timeout_s=int(os.environ.get("CONNECTOR_RUN_TIMEOUT_S", "300")),
        webhook_secret_github=os.environ.get("GITHUB_WEBHOOK_SECRET"),
        webhook_secret_gitlab=os.environ.get("GITLAB_WEBHOOK_SECRET"),
        query_latency_warn_ms=float(os.environ.get("QUERY_LATENCY_WARN_MS", "500.0")),
        embeddings_partition_count=int(os.environ.get("EMBEDDINGS_PARTITION_COUNT", "8")),
        webhook_drain_interval_s=int(os.environ.get("WEBHOOK_DRAIN_INTERVAL_S", "5")),
        webhook_request_timeout_s=float(os.environ.get("WEBHOOK_REQUEST_TIMEOUT_S", "10.0")),
        webhook_batch_size=int(os.environ.get("WEBHOOK_BATCH_SIZE", "50")),
        http_methods_mode=os.environ.get("REGISTRY_HTTP_METHODS_MODE", "rest").strip().lower(),
        http_method_alias_separator=os.environ.get("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", "colon").strip().lower(),
        closure_refresh_concurrency=int(os.environ.get("CLOSURE_REFRESH_CONCURRENCY", "8")),
    )
