"""FastAPI app factory + OTel + Prometheus + healthz/readyz/metrics.

`create_app(settings)` is the single entry point: builds the engine and
session factory, wires them onto `app.state`, optionally initializes the
OTel SDK with OTLP export when `settings.otlp_endpoint` is set, and
registers all routers.

The scheduler runs three background jobs:
- Embedding drain: flushes the outbox every `outbox_poll_interval_s` seconds
  with `max_instances=1` and `coalesce=True`.
- Scheduler is backed by ``SQLAlchemyJobStore`` (durable across restarts)
  with per-source cron jobs registered on startup via ``register_sync_jobs``.
- Audit partition check: hourly job that logs a WARNING and increments
  ``catalog_audit_partitions_eligible_for_archival`` for any ``audit_log``
  partition whose lower range bound is older than 24 months.
  See ``docs/runbook-ops.md`` for the operator archival procedure.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.openapi.utils import get_openapi
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from sqlalchemy import text

from registry.config import Settings, get_settings
from registry.embedder import StubEmbedder
from registry.logging_config import configure_logging
from registry.service.catalog import CatalogService
from registry.service.embedding_drain import drain_outbox
from registry.service.external_ids import ExternalIdService
from registry.service.includes import IncludeService
from registry.service.lifecycle import LifecycleService
from registry.service.retrieval import RetrievalService
from registry.service.schema import SchemaService
from registry.service.visibility import VisibilityService
from registry.service.vocabulary import VocabularyService
from registry.storage.pg import create_engine, get_session_factory
from registry.types import Embedder, SystemClock
from sync.runner import create_scheduler, register_sync_jobs

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenAPI documentation — preamble + tag descriptions surfaced at /docs.
# Every endpoint is tenant-scoped. "admin: …" tags require the admin role
# within the CALLING tenant; they are never service-operator surfaces.
# Service operators interact via env vars / helm / migrations / mint_token.py.
# ---------------------------------------------------------------------------


_OPENAPI_DESCRIPTION = """\
**registry** — semantic + temporal retrieval of an organisation's
engineering capabilities, with cross-tenant adoption, subscriptions,
notifications, and a breaking-change advisor.

### Authentication & tenancy

Every endpoint is **tenant-scoped**. The calling tenant is resolved from
the `Authorization: Bearer <token>` header (or OIDC JWT when
`OIDC_DISCOVERY_URL` is configured). No endpoint ever returns data from a
tenant other than the caller's — cross-tenant relationships go through
explicit adoption events (see the `adoptions` section).

### Roles within a tenant

| Role       | Typical use                                                     |
|------------|-----------------------------------------------------------------|
| `consumer` | Read capabilities, subscribe to events, list notifications.     |
| `producer` | Create / update / publish capabilities owned by this tenant.    |
| `admin`    | Manage this tenant's vocabulary, schemas, PII policies, RBAC.   |
| `auditor`  | Read-only access to the audit log + notification history.       |

### `admin: …` endpoints are tenant-admin, not service-admin

Sections tagged `admin: …` require the **`admin` role within the calling
tenant**. They are NOT service-operator surfaces. There is no cross-tenant
admin surface in the API. Service operators (the people running the
deployment) interact with the system through environment variables, helm
values, Alembic migrations, and `scripts/mint_token.py` — not REST.

### HTTP method conventions

Standard verbs (`PATCH`, `DELETE`) are the canonical surface. Operators
behind enterprise gateways that strip non-GET/POST verbs can opt into
POST-tunneled aliases (`POST .../{id}:update`, `:delete`, etc.) by
setting `REGISTRY_HTTP_METHODS_MODE=both`. The aliases are disabled by
default.
"""


_OPENAPI_TAGS: list[dict[str, str]] = [
    # ---- Producer surfaces ----
    {
        "name": "capabilities",
        "description": (
            "Producer-side CRUD for capabilities owned by the calling "
            "tenant. Create, read, update, soft-delete, and set "
            "visibility (`private` / `tenant-shared` / `public`)."
        ),
    },
    {
        "name": "concepts",
        "description": "Concept-type entities — definitions and terminology referenced by capabilities.",
    },
    {
        "name": "operations",
        "description": "Operation-type entities — the verbs a capability supports (e.g. `createPayment`).",
    },
    {
        "name": "artifacts",
        "description": (
            "Bi-temporal facts attached to a capability — descriptions, "
            "decisions, runbooks, and other free-form content. Each "
            "artifact write supersedes the previous active row."
        ),
    },
    {
        "name": "lifecycle",
        "description": (
            "Promote/demote capabilities between lifecycle states "
            "(`alpha`, `beta`, `ga`, `deprecated`, `retired`). Producer "
            "or admin role; integration capabilities additionally require "
            "≥ 2 `composes` / `depends_on` edges before leaving `alpha`."
        ),
    },
    {
        "name": "interface",
        "description": (
            "Bi-temporal interface surface declarations. Accepts "
            "JSON Schema, TypeScript types (restricted subset), or "
            "OpenAPI 3.x; normalises to a canonical InterfaceSurface used "
            "by the breaking-change advisor."
        ),
    },
    # ---- Consumer surfaces ----
    {
        "name": "retrieval",
        "description": (
            "Hybrid semantic + lexical + graph search across capabilities "
            "and facts. List capabilities, fetch a full capability record "
            "(time-travel via `as_of`), and walk outgoing dependencies."
        ),
    },
    {
        "name": "graph",
        "description": (
            "Graph primitives — reverse traversal (who depends on this?), "
            "blast-radius transitive closure (cache-first), and "
            "provider/consumer projections (`GET /v1/graph/{provider,consumer}`)."
        ),
    },
    {
        "name": "integrations",
        "description": (
            "Pair-discoverability lookup: `GET /v1/integrations?connects=A&and=B` "
            "returns integration capabilities whose member edges touch "
            "both `A` and `B`. Visibility-filtered."
        ),
    },
    # ---- Cross-tenant ----
    {
        "name": "adoptions",
        "description": (
            "Cross-tenant adoption events. A consumer tenant "
            "adopts a provider tenant's capability — the API records "
            "the relationship, creates a `provides_to` edge owned by "
            "the provider, and (transitively) creates an inbox-only "
            "subscription for change events."
        ),
    },
    {
        "name": "breaking-change",
        "description": (
            "Read-only advisory for proposed version bumps. "
            "POST a proposed version + interface; receive the diff "
            "classification, the per-element changes, the list of "
            "affected consumers (cross-tenant consumer IDs are anonymised "
            "so the provider sees impact size without learning which "
            "external tenants are affected), and a release-notes scaffold."
        ),
    },
    # ---- Async surfaces ----
    {
        "name": "subscriptions",
        "description": (
            "Subscribe to capability events (`version_published`, "
            "`deprecation`, `breaking_change`, `conflict_added`, "
            "`integration_added`). Optional webhook URL + HMAC secret; "
            "inbox-only subscriptions default to no webhook. Auto-"
            "subscription on adoption."
        ),
    },
    {
        "name": "notifications",
        "description": (
            "In-catalog inbox for capability events. Cursor-paginated "
            "list (`status=unread/read/all`) + mark-read. Payload is "
            "the `CapabilityRegistryEvent` envelope only — no body text, "
            "description, or freeform content. Follow `fetch_url` to "
            "retrieve the full canonical record."
        ),
    },
    # ---- External-ID registry ----
    {
        "name": "external-ids",
        "description": (
            "Per-entity external-ID mapping — declare that "
            "`capability X` corresponds to `Stripe customer 123` in an "
            "external system. Lookup is bi-directional."
        ),
    },
    # ---- Inbound webhook receivers ----
    {
        "name": "webhooks",
        "description": (
            "Inbound webhook receivers for external systems (GitHub, "
            "GitLab) that push changes into the catalog. HMAC-signed; "
            "the secret is per-tenant per-source (`GITHUB_WEBHOOK_SECRET`, "
            "`GITLAB_WEBHOOK_SECRET`). Public — authenticated by signature, "
            "not Bearer token."
        ),
    },
    # ---- Admin: tenant-scoped administrative surfaces ----
    # (Every "admin: …" section requires the `admin` role in the calling
    # tenant. None of these are service-operator endpoints.)
    {
        "name": "admin: tokens",
        "description": "Mint and revoke API tokens for actors in the calling tenant.",
    },
    {
        "name": "admin: sync",
        "description": (
            "Manage sync sources (connectors that push external data into "
            "the catalog) and inspect sync-run history for the calling "
            "tenant. Trigger an on-demand sync run."
        ),
    },
    {
        "name": "admin: vocabulary",
        "description": (
            "Manage closed-vocabulary values (`entity_type`, `edge_rel`, "
            "`fact_category`, etc.) for the calling tenant. New values "
            "supersede prior bi-temporal rows."
        ),
    },
    {
        "name": "admin: schemas",
        "description": (
            "Register capability-type schemas (`integration`, custom "
            "tenant types) used to validate `capability.attributes` on "
            "write."
        ),
    },
    {
        "name": "admin: edge-schemas",
        "description": (
            "Register edge-property schemas used to validate " "`edge.properties` on write. Advisory or mandatory."
        ),
    },
    {
        "name": "admin: pii",
        "description": (
            "Manage PII pattern definitions and per-tenant field "
            "policies (advisory / block). The default scanner is "
            "always on; this surface configures policy."
        ),
    },
    {
        "name": "admin: rbac",
        "description": (
            "Assign and revoke roles (`producer`, `consumer`, `admin`, "
            "`auditor`) on actors within the calling tenant."
        ),
    },
    {
        "name": "admin: audit",
        "description": (
            "Query the calling tenant's audit log — every content-"
            "access event is recorded with actor, timestamp, and target."
        ),
    },
    {
        "name": "admin: external-systems",
        "description": ("Register external systems that the tenant maps " "capabilities to via `external-ids`."),
    },
]


# Prometheus gauge: number of audit_log child partitions eligible for archival
# (lower range bound older than 24 months).  Operator should run the detach
# procedure in docs/runbook-ops.md when this is > 0.
_AUDIT_ARCHIVAL_GAUGE: Gauge = Gauge(
    "catalog_audit_partitions_eligible_for_archival",
    "Number of audit_log monthly partitions whose lower bound is older than 24 months",
)

# Regex to extract YYYY_MM from partition names like audit_log_2024_03
_PARTITION_NAME_RE = re.compile(r"audit_log_(\d{4})_(\d{2})$")


def audit_partitions_eligible_for_archival(
    partition_names: list[str],
    reference_date: datetime.date | None = None,
    retention_months: int = 24,
) -> list[str]:
    """Return partition names whose lower bound is older than *retention_months*.

    Accepts bare partition names like ``audit_log_2024_03`` and extracts the
    year/month from the suffix.  Partitions that don't match the expected
    pattern are silently skipped (forward partitions created by the migration
    follow the same naming scheme but will not be older than 24 months).

    Args:
        partition_names: List of pg_class relnames for audit_log children.
        reference_date: Date to compute age from; defaults to today (UTC).
        retention_months: Window in months; partitions older than this are eligible.

    Returns:
        Sorted list of eligible partition names.
    """
    ref = reference_date or datetime.date.today()
    cutoff_year = ref.year
    cutoff_month = ref.month - retention_months
    # Normalise month into valid (year, month) pair
    while cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year -= 1
    cutoff = datetime.date(cutoff_year, cutoff_month, 1)

    eligible: list[str] = []
    for name in partition_names:
        m = _PARTITION_NAME_RE.match(name)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        try:
            partition_start = datetime.date(year, month, 1)
        except ValueError:
            continue
        if partition_start < cutoff:
            eligible.append(name)
    return sorted(eligible)


async def check_audit_partition_ages(session_factory: object) -> None:
    """Async job: query pg_inherits, update gauge, emit WARNING if needed.

    Runs under ``AsyncIOScheduler`` (which awaits coroutine jobs) using the
    project's async session factory.  When the factory has no bind (unit-test
    mocks), the function sets the gauge to 0 and returns.
    """
    try:
        bind = getattr(session_factory, "kw", {}).get("bind") or getattr(session_factory, "kw_args", {}).get("bind")
        if bind is None:
            _log.debug("audit_partition_check: no async engine available — skipping")
            _AUDIT_ARCHIVAL_GAUGE.set(0)
            return

        async with bind.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT c.relname
                    FROM   pg_inherits i
                    JOIN   pg_class c ON c.oid = i.inhrelid
                    JOIN   pg_class p ON p.oid = i.inhparent
                    WHERE  p.relname = 'audit_log'
                    """
                )
            )
            rows = result.fetchall()
        names = [row[0] for row in rows]
    except Exception as exc:
        _log.warning("audit_partition_check: failed to query pg_inherits: %s", exc)
        return

    eligible = audit_partitions_eligible_for_archival(names)
    count = len(eligible)
    _AUDIT_ARCHIVAL_GAUGE.set(count)
    if count > 0:
        _log.warning(
            "audit_partition_check: %d audit_log partition(s) eligible for archival "
            "(older than 24 months): %s — run the detach procedure in docs/runbook-ops.md",
            count,
            ", ".join(eligible),
        )
    else:
        _log.debug("audit_partition_check: no partitions eligible for archival")


# Paths that are public by design — operator probes, Swagger's own assets,
# and HMAC-authenticated webhook receivers. They are kept off the document-
# level `security` requirement so Swagger UI does not present an Authorize
# prompt for them and so OpenAPI consumers correctly mark them anonymous.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = ("/healthz", "/readyz", "/metrics", "/webhooks")


def _install_error_envelope(app: FastAPI) -> None:
    """Wrap every error response into the structured envelope.

    Three handlers cover the surface:

    1. ``HTTPException`` (any router) → ``{"errors": [{path, code, message}]}``.
       Routers that ``raise HTTPException(detail="...")`` get auto-wrapped;
       routers that ``raise build_error(...)`` get the path+code they
       provided.
    2. ``RequestValidationError`` (FastAPI's own 422 for malformed bodies /
       query params / path params) → each Pydantic error becomes one
       ErrorItem, with ``loc`` joined as a JSON-Pointer-ish ``path``.
    3. Generic ``Exception`` → 500 with ``code=internal_error``. We don't
       leak the exception message to the client; we log it server-side.
    """
    from fastapi.exceptions import RequestValidationError  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: PLC0415

    from registry.api.errors import coerce_to_envelope  # noqa: PLC0415

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(_request: object, exc: StarletteHTTPException) -> JSONResponse:
        envelope = coerce_to_envelope(exc.status_code, exc.detail)
        return JSONResponse(status_code=exc.status_code, content=envelope, headers=exc.headers)

    # Service-layer typed exceptions — map to HTTP status codes here so any
    # service method that raises NotFoundError/PermissionError surfaces as the
    # right status without every router needing its own try/except. Router-level
    # catches (e.g. via map_catalog_error) still take precedence when present.
    from registry.exceptions import NotFoundError as _NotFoundError  # noqa: PLC0415

    @app.exception_handler(_NotFoundError)
    async def _not_found_handler(_request: object, exc: _NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"errors": [{"path": None, "code": "not_found", "message": str(exc)}]},
        )

    @app.exception_handler(PermissionError)
    async def _permission_handler(_request: object, exc: PermissionError) -> JSONResponse:
        # Visibility chokepoint denials surface as 403 with no detail about
        # the owner tenant — the chokepoint's own message contains the
        # required tenant guidance for the caller. The body intentionally
        # echoes the raised text only when configured by callers; tests
        # asserting against cross-tenant probe responses must verify that
        # the owner-tenant UUID and entity name are not leaked here.
        return JSONResponse(
            status_code=403,
            content={"errors": [{"path": None, "code": "forbidden", "message": "forbidden"}]},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(_request: object, exc: RequestValidationError) -> JSONResponse:
        items = []
        for err in exc.errors():
            loc = err.get("loc") or ()
            # Skip the conventional first segment ("body"/"query"/"path") in the
            # JSON Pointer so $.name reads correctly.
            parts = [str(p) for p in loc[1:]] if len(loc) > 1 else [str(p) for p in loc]
            path = "$" + ("." + ".".join(parts) if parts else "")
            items.append(
                {
                    "path": path,
                    "code": str(err.get("type", "validation_error")),
                    "message": str(err.get("msg", "")),
                }
            )
        return JSONResponse(status_code=422, content={"errors": items})

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(_request: object, exc: Exception) -> JSONResponse:
        _log.exception("unhandled exception in request handler", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "errors": [
                    {"path": None, "code": "internal_error", "message": "internal server error"},
                ],
            },
        )


def _install_openapi_security(app: FastAPI, settings: Settings) -> None:
    """Override `app.openapi` so the spec declares the auth surface Swagger UI needs.

    Declares two OpenAPI 3 security schemes:

    - `bearerAuth` — HTTP Bearer with `bearerFormat: opaque`. Always present.
      `make dev-token` mints one for local dev (zero-state); production /
      break-glass tokens come from `scripts/mint_token.py` (needs UUIDs).
    - `oidcAuth` — `openIdConnect` pointing at the configured discovery URL.
      Emitted only when `settings.oidc_discovery_url` is set, so deployments
      without an IdP advertise only the bearer lane.

    The document-level `security` requirement is `[bearerAuth] OR [oidcAuth]`
    (each scheme listed as its own single-element entry — OpenAPI 3 treats
    multiple entries as logical OR). Public paths override that to `[]`.
    """

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )

        schemes: dict[str, dict[str, Any]] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": (
                    "Paste an API token.\n\n"
                    "- **Local development:** run `make dev-token` — it seeds a "
                    "tenant + actor + role grants and prints a fresh token. "
                    "No env-var setup required if the docker-compose stack is up.\n"
                    "- **Production / break-glass:** mint with "
                    "`python scripts/mint_token.py --tenant-id <uuid> "
                    "--actor-id <uuid> --roles <role>`. Requires the tenant "
                    "and actor to already exist."
                ),
            },
        }
        if settings.oidc_discovery_url is not None:
            schemes["oidcAuth"] = {
                "type": "openIdConnect",
                "openIdConnectUrl": settings.oidc_discovery_url,
                "description": (
                    "JWT issued by the configured OpenID Connect provider. "
                    "The token must carry `sub` and `tenant_id`/`tid` claims; "
                    "the matching actor must pre-exist in the catalog."
                ),
            }
        components = schema.setdefault("components", {})
        components["securitySchemes"] = schemes

        security_requirement: list[dict[str, list[str]]] = [{"bearerAuth": []}]
        if "oidcAuth" in schemes:
            security_requirement.append({"oidcAuth": []})
        schema["security"] = security_requirement

        for path, methods in schema.get("paths", {}).items():
            if not path.startswith(_PUBLIC_PATH_PREFIXES):
                continue
            for op in methods.values():
                if isinstance(op, dict):
                    op["security"] = []

        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]


def _init_otel(settings: Settings) -> None:
    """Initialize the OTel SDK with OTLP HTTP export. No-op when otlp_endpoint is None."""
    if settings.otlp_endpoint is None:
        return
    resource = Resource.create({"service.name": settings.service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint)))
    trace.set_tracer_provider(provider)


def _build_embedder(settings: Settings) -> Embedder:
    """Build the production embedder, falling back to StubEmbedder on import error.

    The sentence-transformers model download can fail in restricted envs (CI,
    air-gapped). StubEmbedder returns zero vectors — suitable for smoke tests
    that don't exercise retrieval recall. Set EMBEDDING_MODEL=stub to force it.
    """
    if settings.embedding_model == "stub":
        return StubEmbedder()
    try:
        from registry.embedder import SentenceTransformerEmbedder  # noqa: PLC0415

        return SentenceTransformerEmbedder()
    except Exception:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning("SentenceTransformerEmbedder failed to load; falling back to StubEmbedder")
        return StubEmbedder()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI app. Idempotent — safe to call repeatedly in tests."""
    settings = settings or get_settings()
    configure_logging(settings)
    _init_otel(settings)

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)

    clock = SystemClock()
    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, clock)
    visibility = VisibilityService(session_factory, clock)
    catalog = CatalogService(session_factory, clock, vocabulary, schema, visibility=visibility)
    # Inject catalog so LifecycleService can delegate replaced_by edge creation
    # via the public CatalogService.create_edge() API.
    lifecycle = LifecycleService(session_factory, clock, catalog=catalog)
    embedder = _build_embedder(settings)
    retrieval = RetrievalService(session_factory, clock, embedder, settings, visibility=visibility)
    external_ids = ExternalIdService(session_factory, clock)
    # visibility is instantiated above and injected into CatalogService so
    # visibility filtering is available throughout the service graph.
    # SubscriptionService is built before AdoptionService so it can be wired
    # into the auto_subscribe hook (adoption transparently creates an inbox-only
    # subscription).
    from registry.service.subscriptions import SubscriptionService  # noqa: PLC0415

    subscriptions = SubscriptionService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
    )
    from registry.service.adoption import AdoptionService  # noqa: PLC0415

    adoption = AdoptionService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
        auto_subscribe=subscriptions.adoption_hook(),
    )
    from registry.service.projections import ProjectionService  # noqa: PLC0415

    projections = ProjectionService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
    )
    from registry.service.notifications import NotificationService  # noqa: PLC0415

    notifications_svc = NotificationService(
        session_factory=session_factory,
        clock=clock,
    )
    from registry.service.breaking_change import BreakingChangeAdvisor  # noqa: PLC0415

    breaking_change_advisor = BreakingChangeAdvisor(
        session_factory=session_factory,
        clock=clock,
        retrieval=retrieval,
        visibility=visibility,
    )
    from registry.service.integration_lookup import IntegrationLookupService  # noqa: PLC0415

    integration_lookup = IntegrationLookupService(
        session_factory=session_factory,
        visibility=visibility,
    )
    from registry.service.interface_storage import InterfaceStorageService  # noqa: PLC0415

    interface_storage = InterfaceStorageService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
    )

    includes = IncludeService(
        session_factory=session_factory,
        visibility=visibility,
        interface_storage=interface_storage,
    )

    # create_scheduler() uses SQLAlchemyJobStore (durable across restarts) and
    # falls back to MemoryJobStore when
    # settings.scheduler_use_memory_jobstore=True (unit tests / no sync driver).
    scheduler = create_scheduler(settings)
    scheduler.add_job(
        drain_outbox,
        trigger="interval",
        seconds=settings.outbox_poll_interval_s,
        kwargs={
            "session_factory": session_factory,
            "embedder": embedder,
            "settings": settings,
        },
        max_instances=1,
        coalesce=True,
        id="embedding_drain",
        replace_existing=True,
    )

    # Hourly check for audit_log partitions eligible for archival (> 24 months old).
    # First run fires at startup so operators see the warning without waiting;
    # subsequent runs follow the interval trigger every hour.
    scheduler.add_job(
        check_audit_partition_ages,
        trigger="interval",
        hours=1,
        kwargs={"session_factory": session_factory},
        max_instances=1,
        coalesce=True,
        id="audit_partition_check",
        replace_existing=True,
    )

    # Drain pending notification_deliveries rows on an interval so the webhook
    # fan-out SLO ("< 30s from triggering write") is met at runtime. The
    # worker instance is constructed here so it binds to the same event loop
    # the scheduler runs on.
    import httpx as _httpx  # noqa: PLC0415

    from registry.workers.webhook_delivery import WebhookDeliveryWorker  # noqa: PLC0415

    webhook_http_client = _httpx.AsyncClient(timeout=settings.webhook_request_timeout_s)
    webhook_worker = WebhookDeliveryWorker(
        session_factory=session_factory,
        clock=clock,
        http_client=webhook_http_client,
    )

    async def _drain_webhooks() -> None:
        try:
            await webhook_worker.run_once(batch_size=settings.webhook_batch_size)
        except Exception as exc:  # noqa: BLE001
            _log.warning("webhook_delivery_drain: %s", exc)

    scheduler.add_job(
        _drain_webhooks,
        trigger="interval",
        seconds=settings.webhook_drain_interval_s,
        max_instances=1,
        coalesce=True,
        id="webhook_delivery_drain",
        replace_existing=True,
    )

    # Hourly soft-invalidation of workspace entries whose expires_at has passed.
    # The worker runs across all tenants in one pass; entries are retained for
    # audit linkage and RTBF — only t_invalidated_at is set, no physical delete.
    from registry.workers.workspace_expiry import WorkspaceExpiryWorker  # noqa: PLC0415

    expiry_worker = WorkspaceExpiryWorker(
        session_factory=session_factory,
        clock=clock,
    )

    async def _expire_workspace_entries() -> None:
        try:
            result = await expiry_worker.run()
            _log.info(
                "workspace_expiry.run: expired=%d batch_ts=%s",
                result.expired_count,
                result.batch_ts,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("workspace_expiry_run: %s", exc)

    scheduler.add_job(
        _expire_workspace_entries,
        trigger="interval",
        hours=1,
        max_instances=1,
        coalesce=True,
        id="workspace_expiry",
        replace_existing=True,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        from registry.api.auth.oidc import _OidcCache  # noqa: PLC0415

        app.state.oidc_cache = _OidcCache()
        scheduler.start()
        # Fire the audit partition age check once at startup so operators see
        # the WARNING immediately without waiting up to an hour.
        await check_audit_partition_ages(session_factory=session_factory)
        # Register sync-source cron jobs after scheduler is running.
        await register_sync_jobs(
            scheduler=scheduler,
            session_factory=session_factory,
            catalog=catalog,
            settings=settings,
        )
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            # Release the webhook worker's HTTP client on shutdown.
            await webhook_worker.close()

    app = FastAPI(
        title=settings.service_name,
        lifespan=lifespan,
        description=_OPENAPI_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.vocabulary = vocabulary
    app.state.schema = schema
    app.state.lifecycle = lifecycle
    app.state.catalog = catalog
    app.state.embedder = embedder
    app.state.retrieval = retrieval
    app.state.external_ids = external_ids
    app.state.scheduler = scheduler
    app.state.visibility = visibility
    app.state.adoption = adoption
    app.state.projections = projections
    app.state.subscriptions = subscriptions
    app.state.notifications = notifications_svc
    app.state.breaking_change = breaking_change_advisor
    app.state.integrations = integration_lookup
    app.state.interface_storage = interface_storage
    app.state.includes = includes

    from registry.api.routers import admin, artifacts, capabilities, concepts, operations, whoami  # noqa: PLC0415

    app.include_router(whoami.router)
    app.include_router(capabilities.router)
    app.include_router(concepts.router)
    app.include_router(operations.router)
    app.include_router(artifacts.router)
    app.include_router(admin.router)

    # Mutation routers — PATCH/DELETE registered via HttpMethodRouter so
    # REGISTRY_HTTP_METHODS_MODE controls the exposed surface.
    app.include_router(capabilities.mutation_router)
    app.include_router(concepts.mutation_router)
    app.include_router(operations.mutation_router)
    app.include_router(artifacts.mutation_router)
    app.include_router(admin.admin_mutation_router)

    # Lifecycle endpoint registered via HttpMethodRouter so mode env var is honoured.
    app.include_router(admin.lifecycle_mutation_router)

    # PII admin endpoints — already use HttpMethodRouter.
    app.include_router(admin.pii_pattern_router)
    app.include_router(admin.pii_field_policy_router)

    # Webhook receiver (public, HMAC-verified).
    from sync.webhook import router as webhook_router  # noqa: PLC0415

    app.include_router(webhook_router)

    # Graph routers — admin stubs + reverse traversal + projections.
    from registry.api.routers.graph import (
        capability_graph_router,  # noqa: PLC0415
        graph_admin_mutation_router,
        projection_router,
    )
    from registry.api.routers.graph import router as graph_admin_router

    app.include_router(graph_admin_router)
    app.include_router(capability_graph_router)
    # Edge-property-schema PATCH via HttpMethodRouter.
    app.include_router(graph_admin_mutation_router)
    # /v1/graph/provider and /v1/graph/consumer projection endpoints.
    app.include_router(projection_router)

    # External-ID registry routers.
    from registry.api.routers.external_ids import (  # noqa: PLC0415
        entity_external_ids_router,
        external_systems_admin_router,
    )

    app.include_router(external_systems_admin_router)
    app.include_router(entity_external_ids_router)

    # Adoption routers.
    from registry.api.routers.adoptions import (  # noqa: PLC0415
        mutation_router as adoptions_mutation_router,
    )
    from registry.api.routers.adoptions import (
        router as adoptions_router,
    )

    app.include_router(adoptions_router)
    app.include_router(adoptions_mutation_router)

    # Subscription routers.
    from registry.api.routers.subscriptions import (  # noqa: PLC0415
        mutation_router as subscriptions_mutation_router,
    )
    from registry.api.routers.subscriptions import (
        router as subscriptions_router,
    )

    app.include_router(subscriptions_router)
    app.include_router(subscriptions_mutation_router)

    # Notification inbox router.
    from registry.api.routers.notifications import router as notifications_router  # noqa: PLC0415

    app.include_router(notifications_router)

    # Breaking-change advisor router.
    from registry.api.routers.breaking_change import router as breaking_change_router  # noqa: PLC0415

    app.include_router(breaking_change_router)

    # Integration-pair lookup router.
    from registry.api.routers.integrations import router as integrations_router  # noqa: PLC0415

    app.include_router(integrations_router)

    # Interface storage router.
    from registry.api.routers.interface import router as interface_router  # noqa: PLC0415

    app.include_router(interface_router)

    # Annotation routers — POST/GET scoped to capability, PATCH/DELETE scoped to annotation.
    from registry.api.routers.annotations import (  # noqa: PLC0415
        mutation_router as annotations_mutation_router,
    )
    from registry.api.routers.annotations import (
        router as annotations_router,
    )

    app.include_router(annotations_router)
    app.include_router(annotations_mutation_router)

    # Workspace CRUD + entry CRUD + share + search routers.
    from registry.api.routers.workspaces import (  # noqa: PLC0415
        entry_mutation_router as workspace_entry_mutation_router,
    )
    from registry.api.routers.workspaces import (
        mutation_router as workspace_mutation_router,
    )
    from registry.api.routers.workspaces import (
        router as workspace_router,
    )
    from registry.api.routers.workspaces import (
        share_mutation_router as workspace_share_mutation_router,
    )
    from registry.api.routers.workspaces import (
        share_router as workspace_share_router,
    )

    app.include_router(workspace_router)
    app.include_router(workspace_mutation_router)
    app.include_router(workspace_entry_mutation_router)
    app.include_router(workspace_share_router)
    app.include_router(workspace_share_mutation_router)

    # Progression definition admin endpoints (POST/GET/PUT/DELETE).
    from registry.api.routers.admin_progression import router as admin_progression_router  # noqa: PLC0415

    app.include_router(admin_progression_router)

    # RTBF admin endpoint — DELETE /v1/admin/actors/{actor_id}/personal-data.
    from registry.api.routers.admin_workspaces import router as admin_workspaces_router  # noqa: PLC0415

    app.include_router(admin_workspaces_router)

    # Consumer read router: /v1/search, /v1/capabilities (list), and
    # /v1/capabilities/{entity_id}/dependencies.
    # Mounted after the capabilities router so FastAPI resolves the exact-match
    # PATCH/DELETE routes first (they share the same prefix).
    from registry.api.routers import retrieval as retrieval_router  # noqa: PLC0415

    app.include_router(retrieval_router.router)

    # Mount MCP server under /mcp — same process, same port, no sidecar.
    from registry.api.routers.annotations import _build_annotation_service  # noqa: PLC0415
    from registry.api.routers.mcp import create_catalog_mcp_server, create_mcp_app  # noqa: PLC0415
    from registry.api.routers.workspaces import _build_workspace_service  # noqa: PLC0415

    annotation_svc = _build_annotation_service(app)
    app.state.annotation_service = annotation_svc

    workspace_svc = _build_workspace_service(app)
    app.state.workspace_service = workspace_svc

    catalog_mcp_server = create_catalog_mcp_server(
        retrieval=retrieval,
        catalog=catalog,
        session_factory=session_factory,
        clock=clock,
        notifications=notifications_svc,
        includes=includes,
        annotation_service=annotation_svc,
        workspace_service=workspace_svc,
    )
    mcp_router = create_mcp_app(server=catalog_mcp_server)
    app.mount("/mcp", mcp_router)

    # ASGI middleware: per-tenant in-process token-bucket rate limiting.
    # Mounted as ASGI middleware (not a FastAPI dependency) so it covers every
    # route automatically — including routes added in the future — without
    # requiring per-router wiring.  The middleware skips public paths
    # (/healthz, /readyz, /metrics, /webhooks) and unauthenticated requests,
    # both of which have no tenant context to key the bucket on.
    from registry.api.middleware.ratelimit import RateLimitMiddleware  # noqa: PLC0415

    app.add_middleware(
        RateLimitMiddleware,
        settings=settings,
        session_factory=session_factory,
    )

    _install_openapi_security(app, settings)
    _install_error_envelope(app)

    FastAPIInstrumentor.instrument_app(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content="db unreachable")
        return Response(status_code=status.HTTP_200_OK, content="ok")

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


__all__ = ["create_app"]
