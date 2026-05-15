"""Configuration surface for the registry service.

`get_settings()` reads from environment variables. Tests construct `Settings`
directly. No module-level singleton — wired by FastAPI DI like `Clock`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

# Internal role names accepted by the registry's RBAC layer. Entitlement
# strings carry external suffix names that must map to one of these four.
_VALID_INTERNAL_ROLES: frozenset[str] = frozenset({"admin", "producer", "consumer", "auditor"})


def _parse_csv_list(value: str | None) -> list[str]:
    """Parse a comma-separated env value into a stripped, non-empty list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_role_mapping(value: str | None) -> dict[str, str]:
    """Parse `EXTERNAL:internal,EXTERNAL:internal,...` into a dict.

    Pairs missing a colon raise ValueError immediately. Whitespace surrounding
    keys and values is stripped. Duplicate external keys take last-wins —
    legitimate during LDAP rename rollouts where old and new strings ship
    concurrently. Semantic validation (non-empty, internal role membership)
    happens in Settings.__post_init__ so direct-dict construction in tests
    is also covered.
    """
    if not value:
        return {}
    result: dict[str, str] = {}
    for raw_pair in value.split(","):
        pair = raw_pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"ENTITLEMENT_ROLE_MAPPING pair {pair!r} is missing the ':' delimiter; "
                "expected 'EXTERNAL:internal'."
            )
        external, internal = pair.split(":", maxsplit=1)
        result[external.strip()] = internal.strip()
    return result


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
    # The expected `aud` claim in JWTs issued for this service. When set,
    # validate_oidc_token rejects tokens whose audience does not match — this
    # blocks confused-deputy attacks in shared-IdP deployments (Auth0, Okta,
    # …) where a token issued for a different application would otherwise be
    # accepted. Leave unset for backward compatibility, but a startup warning
    # fires whenever OIDC is enabled and this is absent.
    oidc_expected_audience: str | None = None
    token_hash_algorithm: str = "sha256"

    # --- Auth: external claim source legacy fields ---
    # Retained pending OAR-T04 deletion (depends on this task landing).
    auth_claim_source_url: str | None = None
    auth_claim_cache_ttl_seconds: int = 300

    # Maximum staleness (seconds) tolerated when the claim source is unreachable
    # and auth_serve_stale_on_failure is True. Acts as a hard ceiling — responses
    # older than this are never served even in stale-on-failure mode.
    auth_stale_ceiling_seconds: int = 86400

    # When True, serve stale cached claim data if the external claim source is
    # unreachable, up to auth_stale_ceiling_seconds. Default False (fail closed).
    # Operators should consider the security trade-off before enabling this.
    auth_serve_stale_on_failure: bool = False

    # HTTP header name used to carry the per-request tenant identifier.
    # Must match whatever the upstream gateway or client sends.
    auth_tenant_id_header: str = "X-Tenant-ID"

    # Optional alias header accepted alongside auth_tenant_id_header.
    # Useful for compatibility with clients that send a legacy header name.
    # Set to None to disable alias header acceptance.
    auth_seal_id_header_alias: str | None = "X-SEAL-ID"

    # --- Auth: OIDC validation contract ---
    # Acceptable `iss` values. Tokens whose issuer is not in this list are
    # rejected even if their signature validates against a trusted JWKS — this
    # blocks confused-deputy attacks across applications sharing an IDP.
    # Empty list = legacy behavior (no issuer allowlisting). Production
    # deployments should populate this.
    oidc_issuer_allowlist: list[str] = field(default_factory=list)

    # Acceptable `azp` (authorized party) or `client_id` values. Applies to
    # all token grant types. Empty list = check skipped (NOT recommended in
    # production; an empty allowlist allows any token issued by a trusted
    # JWKS to pass the service-token check).
    oidc_client_id_allowlist: list[str] = field(default_factory=list)

    # Registry-enforced upper bound on token lifetime: middleware rejects
    # tokens where `exp - iat` exceeds this bound, or where `iat` is absent.
    # Defense-in-depth against IDP misconfiguration that could issue
    # long-lived tokens. Clock skew tolerance ±60s (applied at validation,
    # not in this setting).
    oidc_max_token_ttl_seconds: int = 900

    # Set of acceptable `aud` (audience) values. ADFS carries the resource
    # URI here. Replaces the singular `oidc_expected_audience` for the
    # multi-resource case. Empty list = legacy behavior (use
    # `oidc_expected_audience` if set).
    resource_uri_allowlist: list[str] = field(default_factory=list)

    # --- Auth: Entitlement service ---
    # Base URL of the enterprise entitlement service. When set, this enables
    # the entitlement-resolution code path; the entitlement-related fields
    # below all become required (validated in __post_init__). Empty/unset =
    # entitlement path is disabled (legacy behavior continues to apply via
    # the legacy claim-source URL field).
    entitlement_service_url: str = ""

    # Environment indicator passed as the `env` query param to the
    # entitlement service (e.g. `PRD`, `NPD`, `DEV`). Required if
    # entitlement_service_url is set.
    entitlement_service_env: str = ""

    # Middle token of the entitlement grammar for this deployment
    # (`<tenant_slug>_<DISCRIMINATOR>_<ROLE_SUFFIX>`). Per-deployment
    # config — multiple registry-shaped services may share one entitlement
    # endpoint with different discriminators. Required if
    # entitlement_service_url is set; non-empty; no internal whitespace.
    entitlement_service_discriminator: str = ""

    # External-suffix → internal-role mapping
    # (e.g. {"ADMIN": "admin", "ADMINISTRATOR": "admin"}). Internal values
    # must be in {admin, producer, consumer, auditor}. Multiple external
    # suffixes may map to the same internal role — covers LDAP rename
    # rollouts with concurrent old/new strings. Required if
    # entitlement_service_url is set; non-empty.
    entitlement_role_mapping: dict[str, str] = field(default_factory=dict)

    # HTTP timeouts and retry budget for entitlement-service calls. The
    # entitlement call sits in the auth hot path on every request, so
    # bounded failure behavior is required to prevent request thread
    # pile-up on a slow upstream.
    entitlement_connect_timeout_ms: int = 250
    entitlement_read_timeout_ms: int = 1500
    entitlement_max_retries: int = 1

    # In-process LRU cache size bound for entitlement responses (per process).
    # TTL is bounded by the JWT's own `exp`, not this setting.
    entitlement_cache_max_entries: int = 10000

    # --- Progression ---
    # TTL (seconds) for the cached progression-definition lookup. The definition
    # describes which capability transitions are allowed. A short TTL keeps the
    # cache fresh after operator edits; 0 disables caching entirely.
    progression_definition_cache_ttl_seconds: int = 60

    def __post_init__(self) -> None:
        # Entitlement service config: required-together. When the entitlement
        # path is wired (entitlement_service_url is non-empty), every related
        # field must also be provided and well-formed. Defaults of empty
        # string / empty dict / empty list are permitted only when the
        # entitlement path is disabled — this keeps existing tests that
        # construct minimal Settings(...) without the new fields working,
        # while still failing loudly at startup the moment the new path is
        # enabled with incomplete config.
        if self.entitlement_service_url:
            if not self.entitlement_service_env:
                raise ValueError(
                    "ENTITLEMENT_SERVICE_ENV must be set when ENTITLEMENT_SERVICE_URL is set."
                )
            if not self.entitlement_service_discriminator:
                raise ValueError(
                    "ENTITLEMENT_SERVICE_DISCRIMINATOR must be set when ENTITLEMENT_SERVICE_URL is set."
                )
            if any(c.isspace() for c in self.entitlement_service_discriminator):
                raise ValueError(
                    "ENTITLEMENT_SERVICE_DISCRIMINATOR may not contain whitespace; "
                    f"got {self.entitlement_service_discriminator!r}."
                )
            if not self.entitlement_role_mapping:
                raise ValueError(
                    "ENTITLEMENT_ROLE_MAPPING must be a non-empty mapping when "
                    "ENTITLEMENT_SERVICE_URL is set."
                )
            for external, internal in self.entitlement_role_mapping.items():
                if not external:
                    raise ValueError(
                        "ENTITLEMENT_ROLE_MAPPING contains an entry with an empty external "
                        f"key (mapped to {internal!r})."
                    )
                if not internal:
                    raise ValueError(
                        f"ENTITLEMENT_ROLE_MAPPING entry {external!r} has an empty internal "
                        "role value."
                    )
                if internal not in _VALID_INTERNAL_ROLES:
                    raise ValueError(
                        f"ENTITLEMENT_ROLE_MAPPING entry {external!r}:{internal!r} maps to an "
                        f"unknown internal role; valid roles are "
                        f"{sorted(_VALID_INTERNAL_ROLES)}."
                    )
            mapped_roles = set(self.entitlement_role_mapping.values())
            uncovered = _VALID_INTERNAL_ROLES - mapped_roles
            if uncovered:
                # Soft warning, not a hard failure: a deployment may legitimately
                # not expose every internal role (e.g. `auditor` may be omitted).
                logging.getLogger(__name__).warning(
                    "ENTITLEMENT_ROLE_MAPPING does not map any external suffix to "
                    "the following internal role(s): %s. Endpoints requiring those "
                    "roles will be inaccessible.",
                    sorted(uncovered),
                )

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
    # Timeout (seconds) for a single OTLP export attempt.  The exporter uses
    # blocking HTTP under the hood, so this caps how long the BatchSpanProcessor
    # worker thread can be tied up on a slow or unreachable collector.  Keeping
    # it short (default 2 s) means a stalling Jaeger/OTEL collector cannot block
    # the worker long enough to fill the span queue and cause span drops on busy
    # services.  Raise it only if your collector is reliably slow but functional.
    otlp_exporter_timeout_s: int = 2

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

    # --- Logging ---
    # "json" emits structured JSON to stdout (production default); "text" emits
    # human-readable plain text (local development). configure_logging() branches
    # on this value — unrecognised strings fall through to the text renderer.
    log_format: str = "json"

    # Root logger level. logging.DEBUG surfaces SQLAlchemy queries and
    # OpenTelemetry SDK internals — high volume; reserve for diagnosis.
    log_level: int = logging.INFO


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
        oidc_expected_audience=os.environ.get("OIDC_EXPECTED_AUDIENCE"),
        default_reads_per_second=int(os.environ.get("DEFAULT_READS_PER_SECOND", "100")),
        default_writes_per_second=int(os.environ.get("DEFAULT_WRITES_PER_SECOND", "10")),
        rate_limit_enabled=os.environ.get("RATE_LIMIT_ENABLED", "true").lower() not in ("0", "false", "no"),
        rate_limit_write_per_minute=int(os.environ.get("RATE_LIMIT_WRITE_PER_MINUTE", "60")),
        rate_limit_read_per_minute=int(os.environ.get("RATE_LIMIT_READ_PER_MINUTE", "600")),
        otlp_endpoint=os.environ.get("OTLP_ENDPOINT"),
        service_name=os.environ.get("SERVICE_NAME", "registry"),
        otlp_exporter_timeout_s=int(os.environ.get("OTLP_EXPORTER_TIMEOUT_S", "2")),
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
        auth_claim_source_url=os.environ.get("AUTH_CLAIM_SOURCE_URL") or None,
        auth_claim_cache_ttl_seconds=int(os.environ.get("AUTH_CLAIM_CACHE_TTL_SECONDS", "300")),
        auth_stale_ceiling_seconds=int(os.environ.get("AUTH_STALE_CEILING_SECONDS", "86400")),
        auth_serve_stale_on_failure=(
            os.environ.get("AUTH_SERVE_STALE_ON_FAILURE", "false").lower() in ("1", "true", "yes")
        ),
        auth_tenant_id_header=os.environ.get("AUTH_TENANT_ID_HEADER", "X-Tenant-ID"),
        auth_seal_id_header_alias=os.environ.get("AUTH_SEAL_ID_HEADER_ALIAS", "X-SEAL-ID") or None,
        oidc_issuer_allowlist=_parse_csv_list(os.environ.get("OIDC_ISSUER_ALLOWLIST")),
        oidc_client_id_allowlist=_parse_csv_list(os.environ.get("OIDC_CLIENT_ID_ALLOWLIST")),
        oidc_max_token_ttl_seconds=int(os.environ.get("OIDC_MAX_TOKEN_TTL_SECONDS", "900")),
        resource_uri_allowlist=_parse_csv_list(os.environ.get("RESOURCE_URI_ALLOWLIST")),
        entitlement_service_url=os.environ.get("ENTITLEMENT_SERVICE_URL", ""),
        entitlement_service_env=os.environ.get("ENTITLEMENT_SERVICE_ENV", ""),
        entitlement_service_discriminator=os.environ.get("ENTITLEMENT_SERVICE_DISCRIMINATOR", ""),
        entitlement_role_mapping=_parse_role_mapping(os.environ.get("ENTITLEMENT_ROLE_MAPPING")),
        entitlement_connect_timeout_ms=int(os.environ.get("ENTITLEMENT_CONNECT_TIMEOUT_MS", "250")),
        entitlement_read_timeout_ms=int(os.environ.get("ENTITLEMENT_READ_TIMEOUT_MS", "1500")),
        entitlement_max_retries=int(os.environ.get("ENTITLEMENT_MAX_RETRIES", "1")),
        entitlement_cache_max_entries=int(os.environ.get("ENTITLEMENT_CACHE_MAX_ENTRIES", "10000")),
        progression_definition_cache_ttl_seconds=int(os.environ.get("PROGRESSION_DEFINITION_CACHE_TTL_SECONDS", "60")),
        log_format=os.environ.get("LOG_FORMAT", "json"),
        log_level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    )
