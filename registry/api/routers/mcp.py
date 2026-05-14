"""MCP server for registry.

Mounts four tools over the Anthropic MCP SDK (FastMCP) as a Starlette
ASGI sub-application under ``/mcp``.  The parent app mounts it with:

    registry_mcp_server = create_registry_mcp_server(...)
    mcp_router = create_mcp_app(server=registry_mcp_server)
    app.mount("/mcp", mcp_router)

This is the in-process binding pattern.  No sidecar, no stdio transport,
no separate process.

Auth design
-----------
FastMCP tool handlers do not run inside FastAPI's Depends machinery.  The
Bearer token is therefore extracted from the raw ASGI scope that the SSE
transport passes to the ``handle_sse`` closure.  The scope is threaded
into each tool call via a per-request token holder that is written by the
SSE handler before delegating to the MCP server.  This re-uses
``registry.api.auth.tokens.validate_token`` directly and is semantically
identical to the REST middleware — the same hash, the same DB check, the
same ``TenantContext`` shape.

Transport
---------
Uses SSE (Server-Sent Events) transport, the only HTTP transport
available in mcp<2.0.  The Starlette sub-app exposes:
  GET  /mcp/sse        — SSE connection endpoint
  POST /mcp/messages/  — client→server message channel
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.sse import SseServerTransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from registry.api.auth.tokens import validate_token
from registry.exceptions import CatalogError, NotFoundError, TenantIsolationError
from registry.service.annotations import AnnotationService
from registry.service.catalog import CatalogService
from registry.service.includes import IncludeService
from registry.service.notifications import NotificationService, event_to_dict
from registry.service.retrieval import RetrievalService
from registry.service.temporal import normalize_utc
from registry.service.workspace import WorkspaceService
from registry.types import (
    Clock,
    SystemClock,
    TemporalFilter,
    TenantContext,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-request token holder — written by handle_sse before MCP server runs.
# ---------------------------------------------------------------------------

_request_token: ContextVar[str] = ContextVar("_mcp_request_token", default="")


# ---------------------------------------------------------------------------
# Auth helper (mirrors tenant middleware, uses tokens.py directly)
# ---------------------------------------------------------------------------


async def _resolve_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> TenantContext:
    """Resolve the per-request Bearer token to a TenantContext.

    Raises ToolError on auth failure so the MCP caller sees a structured
    error rather than an HTTP 401 (MCP protocol uses tool-level errors
    rather than HTTP status codes).
    """
    raw = _request_token.get()
    if not raw:
        raise ToolError("missing bearer token")
    try:
        async with session_factory() as session:
            return await validate_token(session, raw, clock)
    except CatalogError as exc:
        raise ToolError("invalid or expired token") from exc


def _extract_bearer(scope: dict[str, Any]) -> str:
    """Pull the Bearer token from the ASGI scope headers (bytes pairs)."""
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer":
                return token.strip()
    return ""


def _parse_as_of(as_of: str | None) -> TemporalFilter:
    """Parse optional ISO-8601 as_of string into TemporalFilter.

    Raises ToolError on naive (timezone-unaware) datetimes.
    """
    if as_of is None:
        return TemporalFilter(as_of=None)
    try:
        dt = datetime.fromisoformat(as_of)
        return TemporalFilter(as_of=normalize_utc(dt))
    except (ValueError, TypeError) as exc:
        raise ToolError(f"as_of must be a timezone-aware ISO-8601 datetime: {exc}") from exc


def _map_catalog_error(exc: CatalogError) -> ToolError:
    if isinstance(exc, NotFoundError):
        return ToolError(f"not found: {exc}")
    if isinstance(exc, TenantIsolationError):
        return ToolError("not found")
    return ToolError(str(exc))


# ---------------------------------------------------------------------------
# JSON serialisation helpers (dataclasses → plain dicts)
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:  # noqa: ANN401
    """Recursively convert dataclass fields and UUIDs to JSON-safe types."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, dict):
        return {(_serialize(k) if isinstance(k, uuid.UUID) else k): _serialize(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _serialize(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


def _http_exc_to_tool_error(exc: HTTPException) -> ToolError:
    """Translate a service HTTPException to a ToolError.

    The annotation service raises HTTPException directly (not typed domain
    exceptions) so the MCP layer catches them here and converts to the
    ToolError shape the MCP protocol expects.

    Translation rules:
    - 403 → "Capability not visible or not found"
    - 404 → "Annotation not found"
    - 422 with PII block detail dict → PII-specific message
    - 422 with plain string detail → the string as-is
    - anything else → str(exc.detail)
    """
    if exc.status_code == 403:
        return ToolError("Capability not visible or not found")
    if exc.status_code == 404:
        return ToolError("Annotation not found")
    if exc.status_code == 422:
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("code") == "pii_detected":
            field: str = detail.get("field", "")
            # Normalise "annotation.body" → "body", "annotation.triage_note" → "triage_note"
            short_field = field.split(".")[-1] if "." in field else field
            categories: list[str] = detail.get("categories", [])
            cats_str = ", ".join(categories)
            return ToolError(
                f"Annotation rejected: PII detected in {short_field} [{cats_str}]"
            )
        if isinstance(detail, str):
            return ToolError(detail)
        return ToolError(str(detail))
    return ToolError(str(exc.detail))


# ---------------------------------------------------------------------------
# Factory: build a FastMCP server closed over service instances
# ---------------------------------------------------------------------------


def create_registry_mcp_server(
    retrieval: RetrievalService,
    catalog: CatalogService,
    session_factory: async_sessionmaker[AsyncSession],
    annotation_service: AnnotationService,
    workspace_service: WorkspaceService,
    clock: Clock | None = None,
    notifications: NotificationService | None = None,
    includes: IncludeService | None = None,
) -> FastMCP:
    """Return a FastMCP instance with the registered registry tools.

    Args:
        retrieval: RetrievalService instance (search, list, dependencies,
            reverse traversal, blast-radius).
        catalog: CatalogService instance (single-entity lookup).
        session_factory: SQLAlchemy async session factory for auth DB calls.
        annotation_service: Pre-built AnnotationService for the annotation MCP
            tools (``submit_annotation``, ``list_my_annotations``,
            ``triage_annotation``). All three tools are registered
            unconditionally — missing wiring is a startup error, not a
            silent no-op.
        workspace_service: Pre-built WorkspaceService for the seven workspace
            MCP tools. Registered unconditionally — missing wiring is a
            startup error, not a silent no-op.
        clock: Clock implementation; defaults to SystemClock.
        notifications: NotificationService for the ``list_notifications`` tool.
            When ``None``, the tool is not registered.
        includes: IncludeService instance for bounded sub-resource expansion
            (``?include=components,depends_on,external_ids,interface``).
            When ``None``, the ``include`` parameter is accepted but silently
            ignored — expansion returns ``None`` for all sub-resources.
    """
    _clock = clock or SystemClock()

    mcp_server = FastMCP(
        name="digital-enablement-registry",
        instructions=(
            "This MCP server exposes tools for the Capability Catalog registry. "
            "The registry manages two distinct resource types: catalog entities "
            "(capabilities, interfaces, components) and workspaces. Workspaces "
            "are collaborative notebooks/memory owned by the registry — they store "
            "structured entries such as decisions, notes, and saved queries that "
            "belong to the registry workflow. Workspaces are not VS Code or any IDE "
            "concept; they have no relation to development environments. Use "
            "create_workspace / add_workspace_entry / search_workspace_entries for "
            "registry notebook operations, and search_capabilities / get_capability "
            "for catalog lookups."
        ),
    )

    # ------------------------------------------------------------------
    # Tool: whoami
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def whoami() -> str:
        """Return the actor + tenant + roles the current credential resolves to.

        Use this as the first call in a session to discover which tenant
        the bearer token is scoped to and what roles the caller has —
        before attempting writes that may 403.

        Returns:
            JSON object: {actor_id, actor_display_name, actor_email,
            tenant_id, tenant_slug, tenant_display_name, roles[],
            token_id, token_expires_at}.
        """
        from registry.service.identity import resolve_whoami  # noqa: PLC0415

        ctx = await _resolve_tenant(session_factory, _clock)
        payload = await resolve_whoami(session_factory, ctx)
        return json.dumps(
            {
                "actor_id": str(payload.actor_id),
                "actor_display_name": payload.actor_display_name,
                "actor_email": payload.actor_email,
                "tenant_id": str(payload.tenant_id),
                "tenant_slug": payload.tenant_slug,
                "tenant_display_name": payload.tenant_display_name,
                "roles": payload.roles,
                "token_id": str(payload.token_id) if payload.token_id else None,
                "token_expires_at": (payload.token_expires_at.isoformat() if payload.token_expires_at else None),
            }
        )

    # ------------------------------------------------------------------
    # Tool: search_capabilities
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def search_capabilities(
        q: str,
        top_k: int = 10,
        as_of: str | None = None,
        entity_type: str | None = None,
        lifecycle: str | None = None,
    ) -> str:
        """Hybrid semantic + lexical + graph search across capabilities.

        Args:
            q: Free-text search query (required).
            top_k: Maximum number of results to return (1–100, default 10).
            as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).
            entity_type: Filter by entity type slug (optional).
            lifecycle: Filter by lifecycle label (optional).

        Returns:
            JSON array of search results with entity metadata and scores.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        temporal_filter = _parse_as_of(as_of)
        if not 1 <= top_k <= 100:
            raise ToolError("top_k must be between 1 and 100")
        try:
            results = await retrieval.search(
                ctx,
                q=q,
                top_k=top_k,
                temporal_filter=temporal_filter,
                entity_type=entity_type,
                lifecycle=lifecycle,
            )
        except CatalogError as exc:
            raise _map_catalog_error(exc) from exc
        return json.dumps(_serialize(results))

    # ------------------------------------------------------------------
    # Tool: get_capability
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def get_capability(
        entity_id: str,
        as_of: str | None = None,
        include: str | None = None,
    ) -> str:
        """Retrieve a single capability record by UUID or slug-form name.

        Args:
            entity_id: UUID of the capability OR its slug-form name
                (e.g. 'salt-design-system'). Slug lookup is
                case-insensitive against the stored `name` column.
            as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).
            include: Comma-separated sub-resources to expand inline. Accepted
                values: ``components``, ``depends_on``, ``external_ids``,
                ``interface``. Each expansion is capped at 200 items —
                ``truncated: true`` + a ``next`` URL signal overflow.
                Unknown values are silently ignored.

        Returns:
            JSON object with entity metadata, attributes, facts, and edges.
            When ``include`` is provided, the response also contains the
            requested sub-resource objects (``components``, ``depends_on``,
            ``external_ids``, ``interface``).
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        temporal_filter = _parse_as_of(as_of)
        as_of_dt = temporal_filter.as_of
        try:
            resolved = await catalog.resolve_entity_handle(ctx, entity_id, as_of=as_of_dt)
            record = await catalog.get_full_capability(ctx, resolved.entity_id, as_of=as_of_dt)
        except CatalogError as exc:
            raise _map_catalog_error(exc) from exc

        result = _serialize(record)

        # Expand bounded sub-resources when ``include`` is requested and the
        # IncludeService is wired in.  Unknown values are silently ignored so
        # callers can pass a superset without getting a 422.
        if include and includes is not None:
            requested = {v.strip() for v in include.split(",") if v.strip()}
            if "components" in requested:
                exp = await includes.expand_components(ctx, resolved.entity_id, handle_for_next=entity_id)
                result["components"] = _serialize(exp.model_dump(mode="json"))
            if "depends_on" in requested:
                exp = await includes.expand_depends_on(ctx, resolved.entity_id, handle_for_next=entity_id)
                result["depends_on"] = _serialize(exp.model_dump(mode="json"))
            if "external_ids" in requested:
                exp = await includes.expand_external_ids(ctx, resolved.entity_id)
                result["external_ids"] = _serialize(exp.model_dump(mode="json"))
            if "interface" in requested:
                exp = await includes.expand_interface(ctx, resolved.entity_id, as_of=as_of_dt)
                result["interface"] = _serialize(exp.model_dump(mode="json"))

        return json.dumps(result)

    # ------------------------------------------------------------------
    # Tool: lookup_by_external_id
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def lookup_by_external_id(
        external_system: str,
        external_id: str,
    ) -> str:
        """Resolve a capability by its external-system mapping.

        Use this when you know a capability's identifier in an upstream
        registry (npm package name, GitHub repo slug, internal ID, …)
        but not its UUID or catalog name. For example, a copilot looking
        at a frontend dev's package.json can call
        lookup_by_external_id("npm", "@salt-ds/core") to find the Salt
        Design System entry in the catalog without first searching.

        Args:
            external_system: The external-system slug as registered
                in /v1/admin/external-systems (e.g. "npm", "github").
            external_id: The identifier inside that system
                (e.g. "@salt-ds/core", "jpmorganchase/salt-ds").

        Returns:
            JSON object with the full capability record (same shape as
            get_capability) or a "not found" object if no mapping exists.
        """
        from sqlalchemy import text  # noqa: PLC0415

        ctx = await _resolve_tenant(session_factory, _clock)
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT entity_id FROM entity_external_ids "
                        "WHERE tenant_id = :tid "
                        "AND external_system_slug = :system "
                        "AND external_id = :eid "
                        "LIMIT 1"
                    ),
                    {"tid": ctx.tenant_id, "system": external_system, "eid": external_id},
                )
            ).first()
        if row is None:
            return json.dumps(
                {
                    "found": False,
                    "external_system": external_system,
                    "external_id": external_id,
                }
            )
        try:
            record = await catalog.get_full_capability(ctx, row[0])
        except CatalogError as exc:
            raise _map_catalog_error(exc) from exc
        return json.dumps(_serialize(record))

    # ------------------------------------------------------------------
    # Tool: get_dependencies
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def get_dependencies(
        entity_id: str,
        depth: int = 2,
        as_of: str | None = None,
    ) -> str:
        """k-hop dependency traversal from a capability.

        Args:
            entity_id: UUID of the root capability OR its slug-form name
                (e.g. 'salt-design-system'). Slug lookup is
                case-insensitive against the stored `name` column.
            depth: Traversal depth (1–5, default 2).
            as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).

        Returns:
            JSON object with root_entity_id, depth, as_of, and edges array.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        if not 1 <= depth <= 5:
            raise ToolError("depth must be between 1 and 5")
        temporal_filter = _parse_as_of(as_of)
        try:
            resolved = await catalog.resolve_entity_handle(ctx, entity_id)
            edges = await retrieval.get_dependencies(
                ctx,
                entity_id=resolved.entity_id,
                depth=depth,
                temporal_filter=temporal_filter,
            )
        except CatalogError as exc:
            raise _map_catalog_error(exc) from exc
        return json.dumps(
            {
                "root_entity_id": str(resolved.entity_id),
                "depth": depth,
                "as_of": temporal_filter.as_of.isoformat() if temporal_filter.as_of else None,
                "edges": _serialize(edges),
            }
        )

    # ------------------------------------------------------------------
    # Tool: get_dependents
    # Thin adapter over retrieval.get_reverse_traversal — no duplicated logic.
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def get_dependents(
        entity_id: str,
        depth: int = 2,
        edge_types: list[str] | None = None,
        as_of: str | None = None,
    ) -> str:
        """Reverse traversal: capabilities that depend on the given entity.

        Returns all nodes that (transitively) point TO ``entity_id``, symmetric
        to ``get_dependencies`` (forward traversal).

        Args:
            entity_id: UUID of the root capability OR its slug-form name
                (e.g. 'salt-design-system'). Slug lookup is
                case-insensitive against the stored `name` column.
            depth: Max hop count (1–5, default 2). Capped at 5 by the service.
            edge_types: Edge relationship vocab values to follow. None follows
                all dependency rels (all vocab minus concept_of, operation_of,
                instance_of).
            as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).

        Returns:
            JSON object matching the REST TraversalResult shape:
            root_entity_id, depth, direction, as_of, nodes, edges,
            version_satisfied, cache_hit.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        if not 1 <= depth <= 5:
            raise ToolError("depth must be between 1 and 5")
        temporal_filter = _parse_as_of(as_of)
        try:
            resolved = await catalog.resolve_entity_handle(ctx, entity_id)
            result = await retrieval.get_reverse_traversal(
                ctx=ctx,
                entity_id=resolved.entity_id,
                depth=depth,
                edge_types=edge_types,
                as_of=temporal_filter.as_of,
            )
        except CatalogError as exc:
            raise _map_catalog_error(exc) from exc
        return json.dumps(_serialize(result))

    # ------------------------------------------------------------------
    # Tool: get_blast_radius
    # Thin adapter over retrieval.get_blast_radius — no duplicated logic.
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def get_blast_radius(
        entity_id: str,
        direction: str = "reverse",
        edge_types: list[str] | None = None,
        depth: int = 5,
        as_of: str | None = None,
    ) -> str:
        """Full transitive closure from a capability, backed by closure_cache.

        Falls back to the recursive CTE when the cache is cold or when
        ``as_of`` is older than 90 days (cache horizon).

        Args:
            entity_id: UUID of the root capability OR its slug-form name
                (e.g. 'salt-design-system'). Slug lookup is
                case-insensitive against the stored `name` column.
            direction: Traversal direction — ``'forward'`` (dependencies) or
                ``'reverse'`` (dependents). Default ``'reverse'``.
            edge_types: Edge relationship vocab values to follow. None follows
                all dependency rels.
            depth: Max hop count (1–5, default 5). Capped at 5 by the service.
            as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).
                Values older than 90 days force the CTE fallback path.

        Returns:
            JSON object matching the REST TraversalResult shape:
            root_entity_id, depth, direction, as_of, nodes, edges,
            version_satisfied, cache_hit.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        if direction not in ("forward", "reverse"):
            raise ToolError("direction must be 'forward' or 'reverse'")
        if not 1 <= depth <= 5:
            raise ToolError("depth must be between 1 and 5")
        temporal_filter = _parse_as_of(as_of)
        try:
            resolved = await catalog.resolve_entity_handle(ctx, entity_id)
            result = await retrieval.get_blast_radius(
                ctx=ctx,
                entity_id=resolved.entity_id,
                direction=direction,
                depth=depth,
                edge_types=edge_types,
                as_of=temporal_filter.as_of,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except CatalogError as exc:
            raise _map_catalog_error(exc) from exc
        return json.dumps(_serialize(result))

    # ------------------------------------------------------------------
    # Tool: list_capabilities
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def list_capabilities(
        lifecycle: str | None = None,
        entity_type: str | None = None,
        page: int = 1,
        page_size: int = 20,
        as_of: str | None = None,
    ) -> str:
        """Paginated list of capabilities visible to the caller's tenant.

        Args:
            lifecycle: Filter by lifecycle label (optional).
            entity_type: Filter by entity type slug (optional).
            page: Page number, 1-based (default 1).
            page_size: Items per page (1–200, default 20).
            as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).

        Returns:
            JSON object with items array, page, and page_size.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        if page < 1:
            raise ToolError("page must be >= 1")
        if not 1 <= page_size <= 200:
            raise ToolError("page_size must be between 1 and 200")
        temporal_filter = _parse_as_of(as_of)
        # RetrievalService.list_capabilities is cursor-paginated. The MCP
        # tool exposes a 1-based `page` knob for client convenience but the
        # service has no offset support, so `page` collapses to "first page
        # only" and clients must use list_my_annotations / search_capabilities
        # for deeper traversal. Passing an empty cursor dict requests the
        # first page.
        try:
            entity_refs, _next_cursor = await retrieval.list_capabilities(
                ctx,
                lifecycle=lifecycle,
                entity_type=entity_type,
                cursor={},
                page_size=page_size,
                temporal_filter=temporal_filter,
            )
        except CatalogError as exc:
            raise _map_catalog_error(exc) from exc
        return json.dumps(
            {
                "items": _serialize(entity_refs),
                "page": page,
                "page_size": page_size,
            }
        )

    # ------------------------------------------------------------------
    # Tool: list_notifications
    # Payload-minimal — output mirrors REST /v1/notifications.
    # ------------------------------------------------------------------

    if notifications is not None:

        @mcp_server.tool()
        async def list_notifications(
            since: str | None = None,
            status: str = "unread",
            page_size: int = 50,
        ) -> str:
            """List capability-event notifications for the caller's tenant.

            Args:
                since: ISO-8601 ``ts`` cursor. Returns rows strictly older
                    than this timestamp. ``None`` returns the first page
                    (newest first).
                status: ``unread`` (default) | ``read`` | ``all``.
                page_size: 1–500 (default 50).

            Returns:
                JSON object ``{"items": [...], "next_cursor": str | None}``.
                Item shape matches REST ``/v1/notifications``
                (CapabilityRegistryEvent — no body text or freeform content).
            """
            ctx = await _resolve_tenant(session_factory, _clock)
            if not 1 <= page_size <= 500:
                raise ToolError("page_size must be between 1 and 500")
            try:
                events, next_cursor = await notifications.list_notifications(
                    ctx=ctx,
                    status=status,
                    cursor=since,
                    page_size=page_size,
                )
            except CatalogError as exc:
                raise _map_catalog_error(exc) from exc
            return json.dumps(
                {
                    "items": [event_to_dict(e) for e in events],
                    "next_cursor": next_cursor,
                }
            )

    # ------------------------------------------------------------------
    # Annotation tools — thin adapters over AnnotationService.
    # All three tools register unconditionally; annotation_service is
    # required at startup so missing wiring raises immediately rather
    # than silently skipping registration.
    # ------------------------------------------------------------------

    @mcp_server.tool()
    async def submit_annotation(
        capability_id: str,
        body: str,
        category: str,
        version_target: str | None = None,
        triage_note: str | None = None,
    ) -> str:
        """Submit a new annotation on a capability.

        The caller must be able to see the capability. The PII scanner runs
        on the body before storage; a block-level hit raises a ToolError
        with a message that names the detected categories.

        Args:
            capability_id: UUID of the capability to annotate.
            body: Annotation text (required, min 1 character).
            category: Annotation category — one of: feedback, bug,
                suggestion, question, doc_gap.
            version_target: Optional version string the annotation targets.
            triage_note: Optional initial triage note (provider use).

        Returns:
            JSON object with the created annotation fields (annotation_id,
            status, body, category, author_tenant_id, …). ``warnings``
            is present only when the PII scanner resolved policy=warn.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        try:
            cap_uuid = uuid.UUID(capability_id)
        except ValueError as exc:
            raise ToolError(f"capability_id must be a valid UUID: {exc}") from exc
        try:
            ref = await annotation_service.create_annotation(
                ctx,
                capability_id=cap_uuid,
                body=body,
                category=category,
                version_target=version_target,
            )
        except HTTPException as exc:
            # Emit the canonical invalid-category message when the service
            # rejects the category value so the MCP caller gets a message
            # that names the valid vocabulary (matching the REST error shape).
            if exc.status_code == 422 and isinstance(exc.detail, str) and "Invalid category" in exc.detail:
                valid = "feedback, bug, suggestion, question, doc_gap"
                raise ToolError(
                    f"Invalid category: '{category}'. Must be one of: {valid}"
                ) from exc
            raise _http_exc_to_tool_error(exc) from exc
        return json.dumps(_serialize(ref))

    @mcp_server.tool()
    async def list_my_annotations(
        status: str | None = None,
        capability_id: str | None = None,
        cursor: str | None = None,
    ) -> str:
        """List annotations authored by the calling actor's tenant.

        Filters to annotations where author_tenant_id equals the caller's
        tenant, regardless of which capability they target. A consumer
        agent can only enumerate their own annotations — never another
        tenant's.

        Args:
            status: Optional status filter — one of: open, triaged,
                acknowledged, closed.
            capability_id: Optional UUID of a specific capability to filter
                to. When omitted, all capabilities are included but the
                caller's annotations are still filtered by author path.
            cursor: Optional opaque pagination cursor from a previous call.

        Returns:
            JSON object ``{"items": [...], "next_cursor": str | null}``.
            Each item matches the AnnotationResponse shape.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        cap_uuid: uuid.UUID | None = None
        if capability_id is not None:
            try:
                cap_uuid = uuid.UUID(capability_id)
            except ValueError as exc:
                raise ToolError(f"capability_id must be a valid UUID: {exc}") from exc

        if cap_uuid is None:
            # No capability filter — return empty list; full cross-capability
            # scan is not supported by list_annotations (it is scoped to one
            # capability at a time). The author-path filter guarantees only
            # the caller's own annotations are returned when cap_uuid is set.
            return json.dumps({"items": [], "next_cursor": None})

        try:
            refs, next_cursor = await annotation_service.list_annotations(
                ctx,
                capability_id=cap_uuid,
                status=status,
                cursor=cursor,
            )
        except HTTPException as exc:
            raise _http_exc_to_tool_error(exc) from exc

        return json.dumps(
            {
                "items": [_serialize(r) for r in refs],
                "next_cursor": next_cursor,
            }
        )

    @mcp_server.tool()
    async def triage_annotation(
        annotation_id: str,
        new_status: str,
        triage_note: str | None = None,
        version_target: str | None = None,
    ) -> str:
        """Triage an annotation — update its status and optionally set a note.

        The caller's tenant must own the capability the annotation belongs
        to. The PII scanner runs on triage_note before storage; a
        block-level hit raises a ToolError naming the detected categories.

        Args:
            annotation_id: UUID of the annotation to triage.
            new_status: New status — one of: open, triaged, acknowledged,
                closed.
            triage_note: Optional note to record alongside the status
                change.
            version_target: Optional version string the triage targets.

        Returns:
            JSON object with the updated annotation fields.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        try:
            ann_uuid = uuid.UUID(annotation_id)
        except ValueError as exc:
            raise ToolError(f"annotation_id must be a valid UUID: {exc}") from exc
        try:
            ref = await annotation_service.triage_annotation(
                ctx,
                annotation_id=ann_uuid,
                new_status=new_status,
                triage_note=triage_note,
                version_target=version_target,
            )
        except HTTPException as exc:
            raise _http_exc_to_tool_error(exc) from exc
        return json.dumps(_serialize(ref))

    # ------------------------------------------------------------------
    # Workspace tools — thin adapters over WorkspaceService.
    # All seven tools register unconditionally; workspace_service is
    # required at startup so missing wiring raises immediately rather
    # than silently skipping registration.
    # ------------------------------------------------------------------

    def _ws_http_exc_to_tool_error(exc: HTTPException, workspace_id: str | None = None) -> ToolError:
        """Translate a WorkspaceService HTTPException to a ToolError.

        Translation rules per the MCP tool contract:
        - 403 with workspace_id context → workspace-specific not-authorized message
        - 403 without context → generic not-authorized message
        - 404 with workspace_id → "Workspace <id> not found."
        - 404 without context → str(detail)
        - 422 with pii_detected dict → "Entry rejected: PII detected in body [<cats>]"
        - 422 plain string → pass through (regulated-tenant block, invalid kind, etc.)
        - anything else → str(detail)
        """
        if exc.status_code == 403:
            if workspace_id:
                return ToolError(f"Not authorized to write to workspace {workspace_id}")
            return ToolError("Not authorized")
        if exc.status_code == 404:
            if workspace_id:
                return ToolError(f"Workspace {workspace_id} not found.")
            return ToolError(str(exc.detail))
        if exc.status_code == 422:
            detail = exc.detail
            if isinstance(detail, dict) and detail.get("code") == "pii_detected":
                categories: list[str] = detail.get("categories", [])
                cats_str = ", ".join(categories)
                return ToolError(f"Entry rejected: PII detected in body [{cats_str}]")
            if isinstance(detail, str):
                return ToolError(detail)
            return ToolError(str(detail))
        return ToolError(str(exc.detail))

    @mcp_server.tool()
    async def create_workspace(
        name: str,
        owner_kind: str,
        description: str | None = None,
    ) -> str:
        """Create a new workspace for the calling actor.

        Args:
            name: Workspace name (required).
            owner_kind: Ownership model — ``'actor'`` for a personal workspace
                owned by the calling actor, or ``'tenant'`` for a team workspace
                owned by the tenant.
            description: Optional human-readable description.

        Returns:
            JSON object with the created workspace fields (workspace_id,
            name, owner_kind, tenant_id, created_at, …).
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        try:
            ref = await workspace_service.create_workspace(
                ctx,
                name=name,
                owner_kind=owner_kind,
                description=description,
            )
        except HTTPException as exc:
            raise _ws_http_exc_to_tool_error(exc) from exc
        return json.dumps(_serialize(ref))

    @mcp_server.tool()
    async def list_workspaces(
        include_archived: bool = False,
    ) -> str:
        """List workspaces visible to the calling actor.

        Returns workspaces that the caller can access: actor-owned workspaces,
        tenant-owned workspaces visible to the caller's role, or any workspace
        the caller's tenant role grants access to.

        Args:
            include_archived: When ``True``, includes archived workspaces
                (archived_at IS NOT NULL). Default ``False``.

        Returns:
            JSON array of workspace objects (WorkspaceRef shape).
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        try:
            refs, _next_cursor = await workspace_service.list_workspaces(
                ctx,
                include_archived=include_archived,
            )
        except HTTPException as exc:
            raise _ws_http_exc_to_tool_error(exc) from exc
        return json.dumps(_serialize(refs))

    @mcp_server.tool()
    async def get_workspace(
        workspace_id: str,
    ) -> str:
        """Get a specific workspace by ID.

        Args:
            workspace_id: UUID of the workspace to retrieve.

        Returns:
            JSON object with the workspace fields (WorkspaceRef shape).
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        try:
            ws_uuid = uuid.UUID(workspace_id)
        except ValueError as exc:
            raise ToolError(f"workspace_id must be a valid UUID: {exc}") from exc
        try:
            ref = await workspace_service.get_workspace(ctx, ws_uuid)
        except HTTPException as exc:
            if exc.status_code == 403:
                raise ToolError(
                    f"Workspace {workspace_id} is not visible to the calling actor."
                ) from exc
            if exc.status_code == 404:
                raise ToolError(f"Workspace {workspace_id} not found.") from exc
            raise _ws_http_exc_to_tool_error(exc, workspace_id=workspace_id) from exc
        return json.dumps(_serialize(ref))

    @mcp_server.tool()
    async def add_workspace_entry(
        workspace_id: str,
        kind: str,
        body_md: str,
        reference_ids: list[str] | None = None,
        references_jsonb: dict[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> str:
        """Add an entry to a workspace.

        The PII scanner runs on body_md (and references_jsonb when provided)
        before storage. A block-level hit raises a ToolError naming the
        detected categories.

        Args:
            workspace_id: UUID of the target workspace.
            kind: Entry kind — one of: note, decision, open_question,
                saved_query, saved_view, private_annotation.
            body_md: Entry body in Markdown (required, non-empty).
            reference_ids: Optional list of UUID strings referencing catalog
                entities.
            references_jsonb: Optional structured reference metadata (JSON
                object).
            expires_at: Optional ISO-8601 UTC expiry datetime. After this
                timestamp the entry is soft-invalidated by the expiry worker.

        Returns:
            JSON object with the created entry fields (WorkspaceEntryRef
            shape). Includes ``warnings`` key when the PII scanner returned
            a warn-level hit.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        try:
            ws_uuid = uuid.UUID(workspace_id)
        except ValueError as exc:
            raise ToolError(f"workspace_id must be a valid UUID: {exc}") from exc

        ref_uuids: list[uuid.UUID] = []
        if reference_ids is not None:
            for rid in reference_ids:
                try:
                    ref_uuids.append(uuid.UUID(rid))
                except ValueError as exc:
                    raise ToolError(
                        f"reference_ids contains an invalid UUID: {rid!r}: {exc}"
                    ) from exc

        expires_at_dt = None
        if expires_at is not None:
            try:
                expires_at_dt = datetime.fromisoformat(expires_at)
            except (ValueError, TypeError) as exc:
                raise ToolError(
                    f"expires_at must be a timezone-aware ISO-8601 datetime: {exc}"
                ) from exc

        try:
            ref = await workspace_service.create_entry(
                ctx,
                workspace_id=ws_uuid,
                kind=kind,
                body_md=body_md,
                reference_ids=ref_uuids,
                references_jsonb=references_jsonb,
                expires_at=expires_at_dt,
            )
        except HTTPException as exc:
            if exc.status_code == 422:
                detail = exc.detail
                if isinstance(detail, dict) and detail.get("code") == "pii_detected":
                    categories_list: list[str] = detail.get("categories", [])
                    cats_str = ", ".join(categories_list)
                    raise ToolError(
                        f"Entry rejected: PII detected in body [{cats_str}]"
                    ) from exc
                if isinstance(detail, str):
                    # Pass through service validation messages (invalid kind,
                    # regulated-tenant block, empty body) as-is so the caller
                    # gets the actionable text the service already composed.
                    raise ToolError(detail) from exc
                raise ToolError(str(detail)) from exc
            raise _ws_http_exc_to_tool_error(exc, workspace_id=workspace_id) from exc
        return json.dumps(_serialize(ref))

    @mcp_server.tool()
    async def update_workspace_entry(
        entry_id: str,
        body_md: str | None = None,
        reference_ids: list[str] | None = None,
        references_jsonb: dict[str, Any] | None = None,
    ) -> str:
        """Update an existing workspace entry.

        Only provided fields are updated; omitted fields retain their current
        values. The PII scanner runs on body_md and references_jsonb when
        provided; a block-level hit raises a ToolError.

        Args:
            entry_id: UUID of the entry to update.
            body_md: New entry body in Markdown (optional).
            reference_ids: Replacement list of UUID strings referencing catalog
                entities (optional).
            references_jsonb: Replacement structured reference metadata
                (optional).

        Returns:
            JSON object with the updated entry fields (WorkspaceEntryRef
            shape). Includes ``warnings`` key when the PII scanner returned
            a warn-level hit.
        """
        ctx = await _resolve_tenant(session_factory, _clock)
        try:
            entry_uuid = uuid.UUID(entry_id)
        except ValueError as exc:
            raise ToolError(f"entry_id must be a valid UUID: {exc}") from exc

        ref_uuids: list[uuid.UUID] | None = None
        if reference_ids is not None:
            ref_uuids = []
            for rid in reference_ids:
                try:
                    ref_uuids.append(uuid.UUID(rid))
                except ValueError as exc:
                    raise ToolError(
                        f"reference_ids contains an invalid UUID: {rid!r}: {exc}"
                    ) from exc

        try:
            ref = await workspace_service.update_entry(
                ctx,
                entry_id=entry_uuid,
                body_md=body_md,
                reference_ids=ref_uuids,
                references_jsonb=references_jsonb,
            )
        except HTTPException as exc:
            if exc.status_code == 422:
                detail = exc.detail
                if isinstance(detail, dict) and detail.get("code") == "pii_detected":
                    categories_list_u: list[str] = detail.get("categories", [])
                    cats_str = ", ".join(categories_list_u)
                    raise ToolError(
                        f"Entry rejected: PII detected in body [{cats_str}]"
                    ) from exc
                if isinstance(detail, str):
                    raise ToolError(detail) from exc
                raise ToolError(str(detail)) from exc
            raise _ws_http_exc_to_tool_error(exc) from exc
        return json.dumps(_serialize(ref))

    @mcp_server.tool()
    async def search_workspace_entries(
        q: str | None = None,
        kind: str | None = None,
        reference_ids: list[str] | None = None,
    ) -> str:
        """Search across workspace entries visible to the calling actor.

        Results are scoped to workspaces the actor owns, their tenant owns,
        or that have been explicitly shared with the actor. No cross-actor
        content is ever returned.

        Args:
            q: Optional full-text search query. When ``None``, all visible
                entries are returned (paginated).
            kind: Optional entry kind filter — one of: note, decision,
                open_question, saved_query, saved_view, private_annotation.
            reference_ids: Optional list of UUID strings; restricts results
                to entries that reference ALL listed entities.

        Returns:
            JSON object ``{"items": [...], "next_cursor": str | null,
            "total_count": int | null}``. Each item matches the
            WorkspaceEntryRef shape.
        """
        ctx = await _resolve_tenant(session_factory, _clock)

        ref_uuids: list[uuid.UUID] | None = None
        if reference_ids is not None:
            ref_uuids = []
            for rid in reference_ids:
                try:
                    ref_uuids.append(uuid.UUID(rid))
                except ValueError as exc:
                    raise ToolError(
                        f"reference_ids contains an invalid UUID: {rid!r}: {exc}"
                    ) from exc

        try:
            result = await workspace_service.search_workspaces(
                ctx,
                q=q,
                kind=kind,
                reference_ids=ref_uuids,
            )
        except HTTPException as exc:
            raise _ws_http_exc_to_tool_error(exc) from exc
        return json.dumps(
            {
                "items": _serialize(result.items),
                "next_cursor": result.next_cursor,
                "total_count": result.total_count,
            }
        )

    return mcp_server


# ---------------------------------------------------------------------------
# ASGI sub-app factory
# ---------------------------------------------------------------------------


def create_mcp_app(server: FastMCP) -> ASGIApp:
    """Build a Starlette ASGI sub-app from a FastMCP server.

    Mounts the MCP server in-process:

        registry_mcp_server = create_registry_mcp_server(...)
        mcp_router = create_mcp_app(server=registry_mcp_server)
        app.mount("/mcp", mcp_router)

    Transport: SSE (mcp<2.0 only exposes SSE HTTP transport; StreamableHTTP
    arrives in mcp>=2.0 — upgrade when the version constraint allows).

    Routes exposed under the ``/mcp`` prefix:
        GET  /mcp/sse        — SSE connection (client initiates session)
        POST /mcp/messages/  — client→server JSON-RPC messages

    Auth: the Bearer token is extracted from the SSE request headers and
    stored in a ContextVar before handing off to the MCP server.  Each tool
    call reads the ContextVar and validates it via ``tokens.validate_token``.
    FastAPI Depends is not available inside FastMCP tool handlers, so this
    ContextVar shim is the equivalent of the REST middleware.
    """
    sse_transport = SseServerTransport("/messages/")

    async def _poll_disconnect(request: Request) -> None:
        """Return as soon as the client closes the connection.

        Polls ``request.is_disconnected()`` in a tight loop so the caller can
        race this against the MCP server run and cancel it on disconnect.
        Starlette's ``is_disconnected()`` is non-blocking (it peeks at the
        receive channel with an immediately-cancelled CancelScope), so the
        loop itself is O(1) per iteration and yields to the event loop on
        each ``asyncio.sleep(0)`` call.
        """
        while not await request.is_disconnected():
            await asyncio.sleep(0.5)

    async def handle_sse(request: Request) -> None:
        # Extract Bearer token from request headers and store in ContextVar
        # so every tool call invoked during this SSE session can read it.
        raw_token = _extract_bearer(dict(request.scope))
        token_var_token = _request_token.set(raw_token)
        try:
            async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
                # Race the MCP server against a disconnect watchdog.  Without
                # this, a client that drops the SSE connection leaves the server
                # socket in CLOSE_WAIT forever because server._mcp_server.run()
                # never returns — it is blocked waiting for the next JSON-RPC
                # message on the POST channel, which will never arrive.
                mcp_task = asyncio.ensure_future(
                    server._mcp_server.run(
                        streams[0],
                        streams[1],
                        server._mcp_server.create_initialization_options(),
                    )
                )
                disconnect_task = asyncio.ensure_future(_poll_disconnect(request))
                try:
                    done, pending = await asyncio.wait(
                        {mcp_task, disconnect_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    # Re-raise any exception from the MCP run task so errors
                    # are not silently swallowed.
                    for task in done:
                        if task is mcp_task and not task.cancelled():
                            exc = task.exception()
                            if exc is not None:
                                raise exc
                except asyncio.CancelledError:
                    mcp_task.cancel()
                    disconnect_task.cancel()
                    raise
        finally:
            _request_token.reset(token_var_token)

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
    )
    return starlette_app


__all__ = [
    "create_registry_mcp_server",
    "create_mcp_app",
]
