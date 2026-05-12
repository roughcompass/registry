"""MCP server for registry.

Mounts four tools over the Anthropic MCP SDK (FastMCP) as a Starlette
ASGI sub-application under ``/mcp``.  The parent app mounts it with:

    mcp_router = create_mcp_app(server=catalog_mcp_server)
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
``catalog.api.auth.tokens.validate_token`` directly and is semantically
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

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any

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
from registry.service.catalog import CatalogService
from registry.service.includes import IncludeService
from registry.service.notifications import NotificationService, event_to_dict
from registry.service.retrieval import RetrievalService
from registry.service.temporal import normalize_utc
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


# ---------------------------------------------------------------------------
# Factory: build a FastMCP server closed over service instances
# ---------------------------------------------------------------------------


def create_catalog_mcp_server(
    retrieval: RetrievalService,
    catalog: CatalogService,
    session_factory: async_sessionmaker[AsyncSession],
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
        clock: Clock implementation; defaults to SystemClock.
        notifications: NotificationService for the ``list_notifications`` tool.
            When ``None``, the tool is not registered.
        includes: IncludeService instance for bounded sub-resource expansion
            (``?include=components,depends_on,external_ids,interface``).
            When ``None``, the ``include`` parameter is accepted but silently
            ignored — expansion returns ``None`` for all sub-resources.
    """
    _clock = clock or SystemClock()

    mcp_server = FastMCP("registry")

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
        try:
            entity_refs = await retrieval.list_capabilities(
                ctx,
                lifecycle=lifecycle,
                entity_type=entity_type,
                page=page,
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

    return mcp_server


# ---------------------------------------------------------------------------
# ASGI sub-app factory
# ---------------------------------------------------------------------------


def create_mcp_app(server: FastMCP) -> ASGIApp:
    """Build a Starlette ASGI sub-app from a FastMCP server.

    Mounts the MCP server in-process:

        mcp_router = create_mcp_app(server=catalog_mcp_server)
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

    async def handle_sse(request: Request) -> None:
        # Extract Bearer token from request headers and store in ContextVar
        # so every tool call invoked during this SSE session can read it.
        raw_token = _extract_bearer(dict(request.scope))
        token_var_token = _request_token.set(raw_token)
        try:
            async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
                await server._mcp_server.run(
                    streams[0],
                    streams[1],
                    server._mcp_server.create_initialization_options(),
                )
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
    "create_catalog_mcp_server",
    "create_mcp_app",
]
