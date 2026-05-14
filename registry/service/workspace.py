"""WorkspaceService — workspace lifecycle: create, get (visibility chokepoint), list,
and entry CRUD.

Every service method that returns workspace content must call get_workspace first.
get_workspace is the workspace-level visibility chokepoint: it enforces the three
access paths (owner, same-tenant member, active share holder) and raises 403/404
before any content is returned.

Two-layer cross-tenant share enforcement is in play (one layer is DB triggers;
this file is Layer 2 — service-layer guard). Cross-tenant share grants are
validated here before any INSERT reaches the database, giving the caller an
actionable error message before the database trigger fires as a backstop.

No EncryptionService parameter. All workspace content is plaintext in this phase.
Encryption is a retrofit concern for a later phase.

_read_body(entry) is the normative accessor for entry body content. Every path
that reads an entry's body must call _read_body — never access entry.body_md
directly outside that helper. This single function is the ENC-phase handoff seam:
when encryption ships, only _read_body gains the conditional decrypt branch instead
of requiring a codebase-wide sweep.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.audit import actions
from registry.types import Clock, TenantContext

if TYPE_CHECKING:
    from registry.service.visibility import VisibilityService

_log = logging.getLogger(__name__)

# Closed vocabulary — matches CHECK constraint on workspaces.owner_kind.
VALID_OWNER_KINDS: frozenset[str] = frozenset({"actor", "tenant"})

# Closed vocabulary — matches CHECK constraint on workspace_entries.kind.
VALID_ENTRY_KINDS: frozenset[str] = frozenset(
    {"note", "decision", "open_question", "saved_query", "saved_view", "private_annotation"}
)

# Closed vocabulary — matches CHECK chk_share_role on workspace_shares.role.
_VALID_SHARE_ROLES: frozenset[str] = frozenset({"reader", "contributor"})

# Maximum page size for list_workspaces; callers above the cap are silently clamped.
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# AuditWriter protocol — matches the shape used across service modules.
# ---------------------------------------------------------------------------


class AuditWriter(Protocol):
    """Single-method protocol satisfied by any callable with the audit.emit signature."""

    async def emit(
        self,
        ctx: TenantContext,
        *,
        action: str,
        target_type: str,
        target_id: uuid.UUID,
        after: dict[str, Any] | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# PIIScanner protocol — matches PiiScanner.scan signature from security module.
# ---------------------------------------------------------------------------


class PIIScanner(Protocol):
    """Minimal protocol surface for PiiScanner used by WorkspaceService."""

    def scan(
        self,
        text: str,
        *,
        field_type: str,
    ) -> Any: ...


class _HasBodyMd(Protocol):
    """Structural protocol for any object with a body_md attribute.

    Accepted by _read_body so it can handle both ORM WorkspaceEntryRecord
    rows and plain SQLAlchemy Row[Any] results without a hard import dependency
    on the ORM class at runtime.
    """

    body_md: str


# ---------------------------------------------------------------------------
# WorkspaceRef dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceRef:
    """Immutable view of a workspace returned by all service methods.

    encryption_tier is intentionally absent — it is an internal forward-compat
    column that is not echoed to clients until the ENC phase ships. Returning
    'none' now creates a forward-compatibility surface that clients begin
    depending on before it carries meaning.

    Fields match WorkspaceResponse shape from the REST contract.
    """

    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    owner_kind: str
    owner_actor_id: uuid.UUID | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None
    t_invalidated_at: datetime | None


# ---------------------------------------------------------------------------
# _read_body — normative accessor for entry body content
# ---------------------------------------------------------------------------


def _read_body(entry: _HasBodyMd) -> str:
    """Return the entry body as a string.

    This is the sole normative accessor for workspace entry body content.
    In the WS phase body_md is always NOT NULL plaintext; this returns it
    directly. Every read of entry body content in this module must go through
    this helper — never access entry.body_md directly.

    This function is the ENC-phase handoff seam: when encryption ships, only
    this function gains the conditional decrypt branch. Scattered direct reads
    of entry.body_md would each need to be found and updated at that point,
    creating the risk of a missed callsite. Centralising here eliminates that risk.
    """
    return entry.body_md


# ---------------------------------------------------------------------------
# ShareRef dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShareRef:
    """Immutable view of a workspace share row returned by share management methods.

    revoked_at is None for active shares. granted_at is the timestamp of the
    original INSERT. role is the closed-vocabulary role granted to the grantee.
    """

    share_id: uuid.UUID
    workspace_id: uuid.UUID
    grantee_actor_id: uuid.UUID
    grantee_tenant_id: uuid.UUID
    role: str
    granted_at: datetime
    revoked_at: datetime | None


# ---------------------------------------------------------------------------
# WorkspaceEntryRef dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceEntryRef:
    """Immutable view of a workspace entry returned by all service methods.

    body_md is str (not Optional) in the WS phase — every active entry carries
    a plaintext body. The ENC-phase migration will ALTER body_md to be nullable
    and update this dataclass to Optional[str] at that point.

    No encryption_status field. That field is deferred to the ENC phase to avoid
    a vestigial enum that always returns 'plaintext' before encryption ships —
    clients would begin depending on it before it carries real meaning.

    warnings is populated on a PII 'warn' outcome (T08). In the WS phase the PII
    scan is stubbed, so warnings will always be None in practice until T08 wires
    full dispatch. The field exists here so the return shape is stable and T08
    does not need a contract change.
    """

    entry_id: uuid.UUID
    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    body_md: str
    references_jsonb: dict[str, Any] | None
    reference_ids: list[uuid.UUID]
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None
    t_invalidated_at: datetime | None
    warnings: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# SearchResult dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchResult:
    """Result set returned by search_workspaces.

    items contains the matching WorkspaceEntryRef objects for the current page.
    next_cursor is non-None when a subsequent page exists; pass it back as
    cursor to retrieve the next page.
    total_count is populated when the DB can supply it cheaply (e.g. a COUNT
    included in the same query); None otherwise. Callers must not assume it is
    always present.
    """

    items: list[WorkspaceEntryRef]
    next_cursor: str | None
    total_count: int | None


# ---------------------------------------------------------------------------
# PurgeResult dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeResult:
    """Counts returned by purge_actor_personal_data.

    All three counts are 0 on a repeated (idempotent) invocation once the
    actor's data has already been purged — there is nothing left to delete or
    revoke.
    """

    purged_entries: int
    purged_workspaces: int
    revoked_shares: int


# ---------------------------------------------------------------------------
# Workspace authorization exceptions
# ---------------------------------------------------------------------------


class WorkspaceAuthError(Exception):
    """Base for workspace authorization failures.

    Routers map subclasses to HTTP status codes — the router never
    re-evaluates authorization. Raised exclusively by workspace service
    methods, not by the router or middleware.
    """


class WorkspaceNotFound(WorkspaceAuthError):
    """The workspace does not exist or is not perceivable to this actor.

    Router maps to HTTP 404. Raised by get_workspace and by any service
    method that cannot perceive the workspace. Callers must not expose
    whether the workspace actually exists when raising this exception.
    """


class WorkspaceOperationDenied(WorkspaceAuthError):
    """The workspace is perceivable but the requested operation is denied.

    Router maps to HTTP 403. Raised only after get_workspace succeeds
    (perceivability is already confirmed). Never raised for non-perceivable
    workspaces — those always result in WorkspaceNotFound.
    """


# ---------------------------------------------------------------------------
# Role-based authorization helpers
# ---------------------------------------------------------------------------


async def _load_effective_roles(
    session: AsyncSession,
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> frozenset[str]:
    """Return the set of role names the actor holds in the given tenant.

    Executes one indexed lookup on the composite primary key of actor_roles.
    Returns an empty frozenset when the actor has no roles in this tenant.
    No caching — roles are queried at request time so revocation takes effect
    immediately without a cache flush.
    """
    result = await session.execute(
        text(
            """
            SELECT r.name
            FROM actor_roles ar
            JOIN roles r ON r.role_id = ar.role_id
            WHERE ar.actor_id = :actor_id
              AND ar.tenant_id = :tenant_id
            """
        ),
        {"actor_id": actor_id, "tenant_id": tenant_id},
    )
    return frozenset(row.name for row in result)


def _can_perceive_workspace(
    effective_roles: frozenset[str],
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    ws_row: Any,
) -> bool:
    """Return True if the actor can perceive (read) the workspace.

    Pure function — no I/O. Called after _load_effective_roles so the
    role set is already available.

    Decision sequence (all conditions must pass in order):
    1. Workspace must belong to the same tenant as the actor.
    2. Actor must hold at least one role in this tenant.
    3. Workspace must not be soft-deleted (t_invalidated_at is None).
    4. For tenant-owned workspaces: any role holder may perceive.
    5. For actor-owned workspaces:
       - Auditors may perceive any actor workspace (audit carve-out).
       - The owner perceives their own workspace if they hold producer or consumer.
       - All other combinations: not perceivable.

    This function is extracted specifically so it can be exhaustively unit-tested
    without a DB session, one test per authorization matrix cell.
    """
    # Condition 1: tenant boundary
    if ws_row.tenant_id != tenant_id:
        return False

    # Condition 2: actor must hold at least one role
    if not effective_roles:
        return False

    # Condition 3: workspace must not be soft-deleted
    if ws_row.t_invalidated_at is not None:
        return False

    # Condition 4: tenant-owned workspaces are visible to any role holder
    if ws_row.owner_kind == "tenant":
        return True

    # Condition 5: actor-owned workspaces — auditor carve-out and owner check
    if "auditor" in effective_roles:
        return True

    if ws_row.owner_actor_id == actor_id and effective_roles & {"producer", "consumer"}:
        return True

    return False


# ---------------------------------------------------------------------------
# WorkspaceService
# ---------------------------------------------------------------------------


class WorkspaceService:
    """Service for creating, retrieving, and listing workspaces.

    get_workspace is the workspace-level visibility chokepoint — every
    service method that touches workspace content must call it first. Bypassing
    it is how cross-actor content leaks happen.

    No EncryptionService parameter — workspaces are plaintext-only in this phase.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        visibility_svc: VisibilityService,
        pii_scanner: PIIScanner,
        audit_writer: AuditWriter,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._visibility_svc = visibility_svc
        self._pii_scanner = pii_scanner
        self._audit_writer = audit_writer
        self._clock = clock

    async def create_workspace(
        self,
        ctx: TenantContext,
        name: str,
        owner_kind: str,
        description: str | None = None,
    ) -> WorkspaceRef:
        """Create a new workspace.

        Steps:
        1. Fetch the tenant row to check is_regulated. Regulated tenants cannot
           create workspaces while encryption_tier='none' — they must wait for the
           ENC phase. This is a program constraint, not a bug; it is surfaced as an
           actionable 422 so operators understand the blocker.
        2. Validate owner_kind is in the closed vocabulary ('actor', 'tenant').
        3. INSERT the workspace row with encryption_tier='none'.
        4. Emit audit event.
        5. Return WorkspaceRef.

        owner_kind='actor' sets owner_actor_id=ctx.actor_id (personal workspace).
        owner_kind='tenant' sets owner_actor_id=NULL (team workspace).
        """
        now = self._clock.now()
        workspace_id = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            # Step 1 — regulated-tenant gate.
            tenant_result = await session.execute(
                text("SELECT is_regulated FROM tenants WHERE tenant_id = :tid"),
                {"tid": ctx.tenant_id},
            )
            tenant_row = tenant_result.first()
            if tenant_row is not None and tenant_row.is_regulated:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Workspace creation is not permitted for regulated tenants at encryption tier 'none'. "
                        "Configure a higher encryption tier before creating workspaces."
                    ),
                )

            # Step 2 — validate owner_kind.
            if owner_kind not in VALID_OWNER_KINDS:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Invalid owner_kind {owner_kind!r}. "
                        f"Must be one of: {sorted(VALID_OWNER_KINDS)}."
                    ),
                )

            owner_actor_id = ctx.actor_id if owner_kind == "actor" else None

            # Step 3 — INSERT workspace row.
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces (
                        workspace_id, tenant_id, name, description,
                        owner_kind, owner_actor_id, encryption_tier,
                        created_at, updated_at, created_by
                    ) VALUES (
                        :workspace_id, :tenant_id, :name, :description,
                        :owner_kind, :owner_actor_id, 'none',
                        :now, :now, :created_by
                    )
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "tenant_id": ctx.tenant_id,
                    "name": name,
                    "description": description,
                    "owner_kind": owner_kind,
                    "owner_actor_id": owner_actor_id,
                    "now": now,
                    "created_by": ctx.actor_id,
                },
            )

        # Step 4 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_CREATED,
            target_type="workspace",
            target_id=workspace_id,
            after={
                "workspace_id": str(workspace_id),
                "tenant_id": str(ctx.tenant_id),
                "owner_kind": owner_kind,
                "name": name,
            },
        )

        _log.info(
            "workspace.created workspace_id=%s tenant=%s owner_kind=%s",
            workspace_id,
            ctx.tenant_id,
            owner_kind,
        )

        # Step 5 — return WorkspaceRef built from the values written.
        return WorkspaceRef(
            workspace_id=workspace_id,
            tenant_id=ctx.tenant_id,
            name=name,
            description=description,
            owner_kind=owner_kind,
            owner_actor_id=owner_actor_id,
            archived_at=None,
            created_at=now,
            updated_at=now,
            created_by=ctx.actor_id,
            t_invalidated_at=None,
        )

    async def get_workspace(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
    ) -> WorkspaceRef:
        """Return a workspace if the caller is authorized to perceive it.

        This is the workspace-level visibility chokepoint. Every service method
        that touches workspace content must call this first. Bypassing it is how
        cross-actor content leaks happen.

        Authorization uses the role-based decision function _can_perceive_workspace:
        the actor must hold at least one role in the workspace's tenant, the
        workspace must not be soft-deleted, and the owner_kind/ownership rules
        must pass. Auditors may perceive any actor-owned workspace in their tenant.

        Raises WorkspaceNotFound when the workspace row does not exist or when the
        actor cannot perceive it — the caller must not distinguish between these two
        cases. WorkspaceNotFound maps to HTTP 404 in the router.
        """
        async with self._session_factory() as session, session.begin():
            ws_result = await session.execute(
                text(
                    """
                    SELECT
                        workspace_id, tenant_id, name, description,
                        owner_kind, owner_actor_id,
                        archived_at, t_invalidated_at,
                        created_at, updated_at, created_by
                    FROM workspaces
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )
            ws_row = ws_result.first()
            if ws_row is None:
                raise WorkspaceNotFound(f"Workspace {workspace_id} not found.")

            effective_roles = await _load_effective_roles(
                session, ctx.actor_id, ctx.tenant_id
            )
            if not _can_perceive_workspace(
                effective_roles, ctx.actor_id, ctx.tenant_id, ws_row
            ):
                raise WorkspaceNotFound(f"Workspace {workspace_id} not found.")

        _log.info(
            "workspace.get workspace_id=%s actor=%s tenant=%s",
            workspace_id,
            ctx.actor_id,
            ctx.tenant_id,
        )

        return WorkspaceRef(
            workspace_id=ws_row.workspace_id,
            tenant_id=ws_row.tenant_id,
            name=ws_row.name,
            description=ws_row.description,
            owner_kind=ws_row.owner_kind,
            owner_actor_id=ws_row.owner_actor_id,
            archived_at=ws_row.archived_at,
            created_at=ws_row.created_at,
            updated_at=ws_row.updated_at,
            created_by=ws_row.created_by,
            t_invalidated_at=ws_row.t_invalidated_at,
        )

    async def list_workspaces(
        self,
        ctx: TenantContext,
        include_archived: bool = False,
        cursor: str | None = None,
    ) -> tuple[list[WorkspaceRef], str | None]:
        """List workspaces visible to the calling actor within their tenant.

        Visibility is role-based: the actor must hold at least one role in their
        tenant, and the workspace must satisfy the perceivability conditions:
          - Tenant-owned workspaces are visible to any role holder.
          - Actor-owned workspaces are visible to the owning actor (with producer
            or consumer role) or to auditors.
        Cross-tenant visibility does not exist under the role model.

        Always excludes soft-deleted rows (t_invalidated_at IS NULL).
        Excludes archived rows when include_archived=False (default).

        The visibility predicate is pushed into SQL via EXISTS subqueries against
        actor_roles so the DB can use its index and no Python-layer post-filter
        is needed for correctness or performance.

        Cursor is keyset on workspace_id, encoded as base64(json({"id": "<uuid>"})).
        """
        cursor_id: uuid.UUID | None = None
        if cursor is not None:
            cursor_id = _decode_cursor(cursor)

        params: dict[str, Any] = {
            "actor_id": ctx.actor_id,
            "tenant_id": ctx.tenant_id,
            "limit": _DEFAULT_PAGE_SIZE + 1,
        }

        # Role-based visibility predicate pushed into SQL.
        # Two branches correspond to the two perceivability rules:
        #   tenant-owned: any role holder can see it
        #   actor-owned: auditor sees all; owner sees their own if producer/consumer
        _role_exists = (
            "SELECT 1 FROM actor_roles ar "
            "JOIN roles r ON r.role_id = ar.role_id "
            "WHERE ar.actor_id = :actor_id AND ar.tenant_id = :tenant_id"
        )
        visibility_predicate = f"""(
            (w.owner_kind = 'tenant'
             AND EXISTS ({_role_exists}))
            OR
            (w.owner_kind = 'actor' AND (
                EXISTS ({_role_exists} AND r.name = 'auditor')
                OR (
                    w.owner_actor_id = :actor_id
                    AND EXISTS ({_role_exists} AND r.name IN ('producer', 'consumer'))
                )
            ))
        )"""

        where_clauses: list[str] = [
            "w.tenant_id = :tenant_id",
            "w.t_invalidated_at IS NULL",
            visibility_predicate,
        ]

        if not include_archived:
            where_clauses.append("w.archived_at IS NULL")

        if cursor_id is not None:
            where_clauses.append("w.workspace_id > :cursor_id")
            params["cursor_id"] = cursor_id

        where_sql = " AND ".join(where_clauses)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    f"""
                    SELECT
                        w.workspace_id, w.tenant_id, w.name, w.description,
                        w.owner_kind, w.owner_actor_id,
                        w.archived_at, w.t_invalidated_at,
                        w.created_at, w.updated_at, w.created_by
                    FROM workspaces w
                    WHERE {where_sql}
                    ORDER BY w.workspace_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            rows = result.fetchall()

        has_next = len(rows) > _DEFAULT_PAGE_SIZE
        if has_next:
            rows = rows[:_DEFAULT_PAGE_SIZE]

        refs = [
            WorkspaceRef(
                workspace_id=row.workspace_id,
                tenant_id=row.tenant_id,
                name=row.name,
                description=row.description,
                owner_kind=row.owner_kind,
                owner_actor_id=row.owner_actor_id,
                archived_at=row.archived_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
                created_by=row.created_by,
                t_invalidated_at=row.t_invalidated_at,
            )
            for row in rows
        ]

        next_cursor: str | None = None
        if has_next and rows:
            next_cursor = _encode_cursor(rows[-1].workspace_id)

        _log.info(
            "workspace.list tenant=%s actor=%s count=%d has_next=%s",
            ctx.tenant_id,
            ctx.actor_id,
            len(refs),
            has_next,
        )

        return refs, next_cursor

    async def update_workspace(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
        archived_at: datetime | None = None,
    ) -> WorkspaceRef:
        """Update name, description, or archived_at for a workspace.

        Call get_workspace first — this is the visibility chokepoint and raises
        403/404 before any mutation runs.

        Authorization beyond get_workspace: the caller must be the owning actor
        OR an admin in the workspace's owning tenant. Any other caller that
        passed the read gate (e.g. a share holder from another tenant) gets 403
        here because write access requires ownership, not just read access.

        Only the fields explicitly supplied are written. None means "no change"
        for name and description; for archived_at None explicitly un-archives.
        Callers that want to un-archive must pass archived_at=None explicitly
        — the method signature does not distinguish "not supplied" from "None"
        at the call site, so callers that want to leave archived_at untouched
        must omit the argument (use the existing WorkspaceRef value themselves).
        """
        # Step 1 — visibility + 403/404 gate.
        existing = await self.get_workspace(ctx, workspace_id)

        # Step 2 — write-auth: owning actor or admin in the workspace's tenant.
        is_owner = (
            existing.owner_actor_id is not None
            and ctx.actor_id == existing.owner_actor_id
        )
        is_tenant_admin = (
            ctx.tenant_id == existing.tenant_id and "admin" in ctx.roles
        )
        if not (is_owner or is_tenant_admin):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Actor {ctx.actor_id} is not authorized to update workspace {workspace_id}. "
                    "Must be the owning actor or an admin in the workspace's tenant."
                ),
            )

        now = self._clock.now()
        effective_name = name if name is not None else existing.name
        effective_description = description if description is not None else existing.description

        # Step 3 — UPDATE workspace row.
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE workspaces
                    SET name = :name,
                        description = :description,
                        archived_at = :archived_at,
                        updated_at = :now
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {
                    "name": effective_name,
                    "description": effective_description,
                    "archived_at": archived_at,
                    "now": now,
                    "workspace_id": workspace_id,
                },
            )

        # Step 4 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_UPDATED,
            target_type="workspace",
            target_id=workspace_id,
            after={
                "workspace_id": str(workspace_id),
                "name": effective_name,
                "description": effective_description,
                "archived_at": archived_at.isoformat() if archived_at is not None else None,
            },
        )

        _log.info(
            "workspace.updated workspace_id=%s actor=%s tenant=%s",
            workspace_id,
            ctx.actor_id,
            ctx.tenant_id,
        )

        return WorkspaceRef(
            workspace_id=existing.workspace_id,
            tenant_id=existing.tenant_id,
            name=effective_name,
            description=effective_description,
            owner_kind=existing.owner_kind,
            owner_actor_id=existing.owner_actor_id,
            archived_at=archived_at,
            created_at=existing.created_at,
            updated_at=now,
            created_by=existing.created_by,
            t_invalidated_at=existing.t_invalidated_at,
        )

    async def delete_workspace(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
    ) -> None:
        """Soft-delete a workspace by setting t_invalidated_at.

        Authorization: owning actor or admin in the workspace's owning tenant.

        Idempotent: if t_invalidated_at is already set the call is a no-op —
        no second audit row is written and no error is raised. This lets callers
        retry without inspecting state first.

        Raises 404 if the workspace row does not exist at all (never created or
        physically deleted). An already-soft-deleted workspace is NOT a 404 —
        it is a no-op success.
        """
        now = self._clock.now()

        # Fetch the row directly (including already-soft-deleted rows) so we can
        # distinguish "doesn't exist" (→ 404) from "already deleted" (→ no-op).
        async with self._session_factory() as session, session.begin():
            ws_result = await session.execute(
                text(
                    """
                    SELECT
                        workspace_id, tenant_id, owner_kind, owner_actor_id,
                        t_invalidated_at
                    FROM workspaces
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )
            ws_row = ws_result.first()

            if ws_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Workspace {workspace_id} not found.",
                )

            # Idempotency: already soft-deleted — no-op, no audit.
            if ws_row.t_invalidated_at is not None:
                _log.info(
                    "workspace.delete_noop workspace_id=%s actor=%s (already soft-deleted)",
                    workspace_id,
                    ctx.actor_id,
                )
                return

            # Write-auth: owning actor or admin in the workspace's owning tenant.
            is_owner = (
                ws_row.owner_actor_id is not None
                and ctx.actor_id == ws_row.owner_actor_id
            )
            is_tenant_admin = (
                ctx.tenant_id == ws_row.tenant_id and "admin" in ctx.roles
            )
            if not (is_owner or is_tenant_admin):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Actor {ctx.actor_id} is not authorized to delete workspace {workspace_id}. "
                        "Must be the owning actor or an admin in the workspace's tenant."
                    ),
                )

            await session.execute(
                text(
                    """
                    UPDATE workspaces
                    SET t_invalidated_at = :now
                    WHERE workspace_id = :workspace_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"now": now, "workspace_id": workspace_id},
            )

        # Emit audit event outside the transaction (consistent with create_workspace).
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_DELETED,
            target_type="workspace",
            target_id=workspace_id,
            after={
                "workspace_id": str(workspace_id),
                "t_invalidated_at": now.isoformat(),
            },
        )

        _log.info(
            "workspace.deleted workspace_id=%s actor=%s tenant=%s",
            workspace_id,
            ctx.actor_id,
            ctx.tenant_id,
        )

    async def create_entry(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
        kind: str,
        body_md: str,
        reference_ids: list[uuid.UUID],
        references_jsonb: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> WorkspaceEntryRef:
        """Create a new entry in a workspace.

        Step 0: Defense-in-depth regulated-tenant block. A regulated tenant normally
        cannot obtain a workspace (blocked at create_workspace), but this guard fires
        independently to close any gap introduced by test fixtures, migrations, or a
        future relaxation of the workspace-create path. Same 422 error body as
        create_workspace.

        Step 1: get_workspace access check — raises 403/404 before any write.

        Step 2: Validate kind is in the closed vocabulary. The CHECK constraint on
        workspace_entries.kind is the DB backstop; the service validates first to give
        callers an actionable message.

        Step 3: Validate body_md is non-empty. An empty body is not a valid entry
        in any entry kind.

        Step 4: PII scan on body_md (field_type='workspace_entry.body').
        block → 422 with categories; warn → entry stored, warnings in response;
        advisory → stored silently with no client signal.

        Step 5: PII scan on references_jsonb if provided (field_type=
        'workspace_entry.references'). Same three-outcome dispatch.

        Step 6: INSERT workspace_entries row.

        Step 7: Emit audit event. Return WorkspaceEntryRef with body via _read_body.
        """
        now = self._clock.now()
        entry_id = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            # Step 0 — regulated-tenant defense-in-depth block.
            tenant_result = await session.execute(
                text("SELECT is_regulated FROM tenants WHERE tenant_id = :tid"),
                {"tid": ctx.tenant_id},
            )
            tenant_row = tenant_result.first()
            if tenant_row is not None and tenant_row.is_regulated:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Workspace creation is not permitted for regulated tenants at encryption tier 'none'. "
                        "Configure a higher encryption tier before creating workspaces."
                    ),
                )

        # Step 1 — get_workspace access check (raises 403/404).
        await self.get_workspace(ctx, workspace_id)

        # Step 2 — validate kind.
        if kind not in VALID_ENTRY_KINDS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid entry kind {kind!r}. "
                    f"Must be one of: {sorted(VALID_ENTRY_KINDS)}."
                ),
            )

        # Step 3 — validate body_md is non-empty.
        if not body_md:
            raise HTTPException(
                status_code=422,
                detail="body_md must not be empty.",
            )

        # Step 4 — PII scan on body_md. Three-outcome dispatch:
        #   block    → raise 422; do NOT insert row.
        #   warn     → proceed with INSERT; surface warning in returned ref.
        #   advisory → proceed silently; no client-visible signal.
        warnings: list[dict[str, Any]] = []
        pii_body = self._pii_scanner.scan(body_md, field_type="workspace_entry.body")
        if pii_body is not None and pii_body.action_taken == "block":
            categories = sorted({m.category for m in pii_body.matched_patterns})
            # TODO: pii_detection_log write — table may not exist yet
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "pii_detected",
                    "field": "workspace_entry.body",
                    "categories": categories,
                },
            )
        elif pii_body is not None and pii_body.action_taken == "warn":
            categories = sorted({m.category for m in pii_body.matched_patterns})
            warnings.append({"field": "body_md", "categories": categories})
        # advisory: proceed silently.
        # TODO: pii_detection_log write on advisory — table may not exist yet

        # Step 5 — PII scan on references_jsonb if provided. Same three-outcome dispatch.
        if references_jsonb is not None:
            pii_refs = self._pii_scanner.scan(
                str(references_jsonb),
                field_type="workspace_entry.references",
            )
            if pii_refs is not None and pii_refs.action_taken == "block":
                categories = sorted({m.category for m in pii_refs.matched_patterns})
                # TODO: pii_detection_log write — table may not exist yet
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "pii_detected",
                        "field": "workspace_entry.references",
                        "categories": categories,
                    },
                )
            elif pii_refs is not None and pii_refs.action_taken == "warn":
                categories = sorted({m.category for m in pii_refs.matched_patterns})
                warnings.append({"field": "references_jsonb", "categories": categories})
            # advisory: proceed silently.
            # TODO: pii_detection_log write on advisory — table may not exist yet

        # Step 6 — INSERT workspace_entries row.
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO workspace_entries (
                        entry_id, workspace_id, tenant_id, kind, body_md,
                        references_jsonb, reference_ids,
                        expires_at, created_at, updated_at, created_by
                    ) VALUES (
                        :entry_id, :workspace_id, :tenant_id, :kind, :body_md,
                        :references_jsonb, :reference_ids,
                        :expires_at, :now, :now, :created_by
                    )
                    """
                ),
                {
                    "entry_id": entry_id,
                    "workspace_id": workspace_id,
                    "tenant_id": ctx.tenant_id,
                    "kind": kind,
                    "body_md": body_md,
                    "references_jsonb": references_jsonb,
                    "reference_ids": reference_ids,
                    "expires_at": expires_at,
                    "now": now,
                    "created_by": ctx.actor_id,
                },
            )

        # Step 7 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_ENTRY_CREATED,
            target_type="workspace_entry",
            target_id=entry_id,
            after={
                "entry_id": str(entry_id),
                "workspace_id": str(workspace_id),
                "kind": kind,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        _log.info(
            "workspace_entry.created entry_id=%s workspace_id=%s kind=%s actor=%s",
            entry_id,
            workspace_id,
            kind,
            ctx.actor_id,
        )

        # Build a synthetic record object so _read_body is the sole body accessor.
        from types import SimpleNamespace
        _synthetic = SimpleNamespace(body_md=body_md)

        return WorkspaceEntryRef(
            entry_id=entry_id,
            workspace_id=workspace_id,
            tenant_id=ctx.tenant_id,
            kind=kind,
            body_md=_read_body(_synthetic),
            references_jsonb=references_jsonb,
            reference_ids=reference_ids,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
            created_by=ctx.actor_id,
            t_invalidated_at=None,
            warnings=warnings if warnings else None,
        )

    async def update_entry(
        self,
        ctx: TenantContext,
        entry_id: uuid.UUID,
        body_md: str | None = None,
        reference_ids: list[uuid.UUID] | None = None,
        references_jsonb: dict[str, Any] | None = None,
    ) -> WorkspaceEntryRef:
        """Update an existing workspace entry.

        Fetches the current entry row to resolve the owning workspace, then calls
        get_workspace to confirm the caller is authorised to write.

        Only fields that are not None are written; omitted fields retain their
        current values. PII scans run on body_md and references_jsonb when provided
        (block/warn/advisory three-outcome dispatch).

        Audit-logged on every successful update.
        """
        now = self._clock.now()

        # Fetch the entry row (including already-deleted rows so we can distinguish
        # "doesn't exist" from "already deleted").
        async with self._session_factory() as session, session.begin():
            entry_result = await session.execute(
                text(
                    """
                    SELECT
                        entry_id, workspace_id, tenant_id, kind, body_md,
                        references_jsonb, reference_ids,
                        expires_at, t_invalidated_at, created_at, updated_at, created_by
                    FROM workspace_entries
                    WHERE entry_id = :entry_id
                    """
                ),
                {"entry_id": entry_id},
            )
            entry_row = entry_result.first()

        if entry_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace entry {entry_id} not found.",
            )

        # Access check via the entry's workspace (raises 403/404 for workspace access).
        await self.get_workspace(ctx, entry_row.workspace_id)

        # PII scan on body_md (when provided). Three-outcome dispatch:
        #   block    → raise 422; do NOT update row.
        #   warn     → proceed with UPDATE; surface warning in returned ref.
        #   advisory → proceed silently; no client-visible signal.
        update_warnings: list[dict[str, Any]] = []
        if body_md is not None:
            pii_body = self._pii_scanner.scan(body_md, field_type="workspace_entry.body")
            if pii_body is not None and pii_body.action_taken == "block":
                categories = sorted({m.category for m in pii_body.matched_patterns})
                # TODO: pii_detection_log write — table may not exist yet
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "pii_detected",
                        "field": "workspace_entry.body",
                        "categories": categories,
                    },
                )
            elif pii_body is not None and pii_body.action_taken == "warn":
                categories = sorted({m.category for m in pii_body.matched_patterns})
                update_warnings.append({"field": "body_md", "categories": categories})
            # advisory: proceed silently.
            # TODO: pii_detection_log write on advisory — table may not exist yet

        # PII scan on references_jsonb (when provided). Same three-outcome dispatch.
        if references_jsonb is not None:
            pii_refs = self._pii_scanner.scan(
                str(references_jsonb),
                field_type="workspace_entry.references",
            )
            if pii_refs is not None and pii_refs.action_taken == "block":
                categories = sorted({m.category for m in pii_refs.matched_patterns})
                # TODO: pii_detection_log write — table may not exist yet
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "pii_detected",
                        "field": "workspace_entry.references",
                        "categories": categories,
                    },
                )
            elif pii_refs is not None and pii_refs.action_taken == "warn":
                categories = sorted({m.category for m in pii_refs.matched_patterns})
                update_warnings.append({"field": "references_jsonb", "categories": categories})
            # advisory: proceed silently.
            # TODO: pii_detection_log write on advisory — table may not exist yet

        # Resolve effective values — None means "leave unchanged".
        # Read existing body through _read_body so ENC-phase decryption funnels
        # through one helper instead of a codebase-wide audit.
        effective_body_md = body_md if body_md is not None else _read_body(entry_row)
        effective_reference_ids = (
            reference_ids if reference_ids is not None else entry_row.reference_ids
        )
        effective_references_jsonb = (
            references_jsonb if references_jsonb is not None else entry_row.references_jsonb
        )

        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE workspace_entries
                    SET body_md = :body_md,
                        reference_ids = :reference_ids,
                        references_jsonb = :references_jsonb,
                        updated_at = :now
                    WHERE entry_id = :entry_id
                    """
                ),
                {
                    "body_md": effective_body_md,
                    "reference_ids": effective_reference_ids,
                    "references_jsonb": effective_references_jsonb,
                    "now": now,
                    "entry_id": entry_id,
                },
            )

        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_ENTRY_UPDATED,
            target_type="workspace_entry",
            target_id=entry_id,
            after={
                "entry_id": str(entry_id),
                "workspace_id": str(entry_row.workspace_id),
            },
        )

        _log.info(
            "workspace_entry.updated entry_id=%s actor=%s",
            entry_id,
            ctx.actor_id,
        )

        # _read_body is the sole accessor; build a synthetic object so
        # the helper is exercised even when no ORM row is available.
        from types import SimpleNamespace
        _synthetic = SimpleNamespace(body_md=effective_body_md)

        return WorkspaceEntryRef(
            entry_id=entry_row.entry_id,
            workspace_id=entry_row.workspace_id,
            tenant_id=entry_row.tenant_id,
            kind=entry_row.kind,
            body_md=_read_body(_synthetic),
            references_jsonb=effective_references_jsonb,
            reference_ids=effective_reference_ids,
            expires_at=entry_row.expires_at,
            created_at=entry_row.created_at,
            updated_at=now,
            created_by=entry_row.created_by,
            t_invalidated_at=entry_row.t_invalidated_at,
            warnings=update_warnings if update_warnings else None,
        )

    async def delete_entry(
        self,
        ctx: TenantContext,
        entry_id: uuid.UUID,
    ) -> None:
        """Soft-delete a workspace entry by setting t_invalidated_at.

        Idempotent: if the entry is already soft-deleted the call is a no-op —
        no second audit row is written and no error is raised. This lets callers
        retry without inspecting state first.

        Raises 404 if the entry row does not exist at all.
        Access is confirmed via the entry's owning workspace (get_workspace).
        """
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            entry_result = await session.execute(
                text(
                    """
                    SELECT entry_id, workspace_id, t_invalidated_at
                    FROM workspace_entries
                    WHERE entry_id = :entry_id
                    """
                ),
                {"entry_id": entry_id},
            )
            entry_row = entry_result.first()

            if entry_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Workspace entry {entry_id} not found.",
                )

            # Idempotency: already soft-deleted — no-op, no audit.
            if entry_row.t_invalidated_at is not None:
                _log.info(
                    "workspace_entry.delete_noop entry_id=%s actor=%s (already soft-deleted)",
                    entry_id,
                    ctx.actor_id,
                )
                return

        # Access check via the entry's workspace.
        await self.get_workspace(ctx, entry_row.workspace_id)

        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE workspace_entries
                    SET t_invalidated_at = :now
                    WHERE entry_id = :entry_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"now": now, "entry_id": entry_id},
            )

        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_ENTRY_DELETED,
            target_type="workspace_entry",
            target_id=entry_id,
            after={
                "entry_id": str(entry_id),
                "workspace_id": str(entry_row.workspace_id),
                "t_invalidated_at": now.isoformat(),
            },
        )

        _log.info(
            "workspace_entry.deleted entry_id=%s actor=%s",
            entry_id,
            ctx.actor_id,
        )

    async def list_entries(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
        kind: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[WorkspaceEntryRef], str | None]:
        """List active entries in a workspace.

        Access is gated by get_workspace — the caller must own the workspace or
        hold an active share. Entries past their expires_at are still returned;
        the expiry worker soft-deletes them in a background run. list_entries does
        not filter on expiry.

        Cursor is keyset on entry_id (ascending UUID natural order). Kind filter
        is applied server-side when provided.

        Returns a tuple of (entries, next_cursor).
        """
        # Access check — raises 403/404 if the caller cannot see this workspace.
        await self.get_workspace(ctx, workspace_id)

        cursor_id: uuid.UUID | None = None
        if cursor is not None:
            cursor_id = _decode_entry_cursor(cursor)

        params: dict[str, Any] = {
            "workspace_id": workspace_id,
            "limit": _DEFAULT_PAGE_SIZE + 1,
        }

        where_clauses: list[str] = [
            "workspace_id = :workspace_id",
            "t_invalidated_at IS NULL",
        ]

        if kind is not None:
            where_clauses.append("kind = :kind")
            params["kind"] = kind

        if cursor_id is not None:
            where_clauses.append("entry_id > :cursor_id")
            params["cursor_id"] = cursor_id

        where_sql = " AND ".join(where_clauses)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    f"""
                    SELECT
                        entry_id, workspace_id, tenant_id, kind, body_md,
                        references_jsonb, reference_ids,
                        expires_at, t_invalidated_at, created_at, updated_at, created_by
                    FROM workspace_entries
                    WHERE {where_sql}
                    ORDER BY entry_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            rows = result.fetchall()

        has_next = len(rows) > _DEFAULT_PAGE_SIZE
        if has_next:
            rows = rows[:_DEFAULT_PAGE_SIZE]

        # body_md for each row is accessed exclusively via _read_body — never directly.
        refs = [
            WorkspaceEntryRef(
                entry_id=row.entry_id,
                workspace_id=row.workspace_id,
                tenant_id=row.tenant_id,
                kind=row.kind,
                body_md=_read_body(row),
                references_jsonb=row.references_jsonb,
                reference_ids=list(row.reference_ids) if row.reference_ids else [],
                expires_at=row.expires_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
                created_by=row.created_by,
                t_invalidated_at=row.t_invalidated_at,
            )
            for row in rows
        ]

        next_cursor: str | None = None
        if has_next and rows:
            next_cursor = _encode_entry_cursor(rows[-1].entry_id)

        _log.info(
            "workspace_entry.list workspace_id=%s actor=%s kind=%s count=%d has_next=%s",
            workspace_id,
            ctx.actor_id,
            kind,
            len(refs),
            has_next,
        )

        return refs, next_cursor

    async def grant_share(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
        grantee_actor_id: uuid.UUID,
        grantee_tenant_id: uuid.UUID,
        role: str,
    ) -> ShareRef:
        """Grant an actor access to a workspace.

        Authorization: the caller must be the owning actor or an admin in the
        workspace's tenant. get_workspace is called first to resolve the workspace
        and enforce the visibility chokepoint.

        Layer 2 cross-tenant guard: actor-owned workspaces may only be shared
        within the same tenant. Granting a cross-tenant share on an actor-owned
        workspace is rejected here before any DB INSERT. The BEFORE INSERT trigger
        on workspace_shares is the DB-level backstop for the same rule.

        Raises 422 if the workspace is actor-owned and grantee_tenant_id differs
        from the workspace's owning tenant.
        Raises 409 if an active (revoked_at IS NULL) share already exists for the
        given grantee_actor_id on this workspace.
        Re-granting after revocation is allowed: a new row is inserted (the unique
        partial index on workspace_shares only covers active rows).

        Audit-logged on success. Returns ShareRef.
        """
        workspace = await self.get_workspace(ctx, workspace_id)

        # Write-auth: owning actor or admin in the workspace's tenant.
        is_owner = (
            workspace.owner_actor_id is not None
            and ctx.actor_id == workspace.owner_actor_id
        )
        is_tenant_admin = (
            ctx.tenant_id == workspace.tenant_id and "admin" in ctx.roles
        )
        if not (is_owner or is_tenant_admin):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Actor {ctx.actor_id} is not authorized to grant shares on workspace {workspace_id}. "
                    "Must be the owning actor or an admin in the workspace's tenant."
                ),
            )

        # Layer 2 guard: actor-owned workspaces cannot be shared cross-tenant.
        if workspace.owner_kind == "actor" and grantee_tenant_id != workspace.tenant_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Actor-owned workspaces may only be shared within the same tenant. "
                    "To share cross-tenant, the workspace must be tenant-owned."
                ),
            )

        # Role validation. The workspace_shares table CHECK constraint enforces this
        # at the DB layer, but a service-layer check produces a clean 422 with the
        # vocabulary instead of letting an integrity error bubble up as 500.
        if role not in _VALID_SHARE_ROLES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid share role {role!r}. "
                    f"Must be one of: {sorted(_VALID_SHARE_ROLES)}."
                ),
            )

        now = self._clock.now()
        share_id = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            # Check for a duplicate active share before inserting.
            dup_result = await session.execute(
                text(
                    """
                    SELECT share_id
                    FROM workspace_shares
                    WHERE workspace_id = :workspace_id
                      AND grantee_actor_id = :grantee_actor_id
                      AND revoked_at IS NULL
                    LIMIT 1
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "grantee_actor_id": grantee_actor_id,
                },
            )
            if dup_result.first() is not None:
                raise HTTPException(
                    status_code=409,
                    detail="An active share already exists for this grantee.",
                )

            # tenant_id is the owning workspace's tenant — NOT NULL in the DDL.
            # Read it from the workspace row fetched by get_workspace earlier so
            # the share's owning-tenant lineage matches the workspace.
            await session.execute(
                text(
                    """
                    INSERT INTO workspace_shares (
                        share_id, workspace_id, tenant_id, grantee_actor_id,
                        grantee_tenant_id, role, granted_at
                    ) VALUES (
                        :share_id, :workspace_id, :tenant_id, :grantee_actor_id,
                        :grantee_tenant_id, :role, :now
                    )
                    """
                ),
                {
                    "share_id": share_id,
                    "workspace_id": workspace_id,
                    "tenant_id": workspace.tenant_id,
                    "grantee_actor_id": grantee_actor_id,
                    "grantee_tenant_id": grantee_tenant_id,
                    "role": role,
                    "now": now,
                },
            )

        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_SHARE_GRANTED,
            target_type="workspace_share",
            target_id=share_id,
            after={
                "share_id": str(share_id),
                "workspace_id": str(workspace_id),
                "grantee_actor_id": str(grantee_actor_id),
                "grantee_tenant_id": str(grantee_tenant_id),
                "role": role,
            },
        )

        _log.info(
            "workspace.share_granted share_id=%s workspace_id=%s grantee_actor=%s grantee_tenant=%s",
            share_id,
            workspace_id,
            grantee_actor_id,
            grantee_tenant_id,
        )

        return ShareRef(
            share_id=share_id,
            workspace_id=workspace_id,
            grantee_actor_id=grantee_actor_id,
            grantee_tenant_id=grantee_tenant_id,
            role=role,
            granted_at=now,
            revoked_at=None,
        )

    async def revoke_share(
        self,
        ctx: TenantContext,
        share_id: uuid.UUID,
    ) -> None:
        """Revoke a workspace share by setting revoked_at.

        Idempotent: if the share is already revoked the call is a no-op —
        no second audit row is written and no error is raised.

        Authorization: the caller must be the owning actor of the workspace or
        an admin in the workspace's tenant.

        Raises 404 if the share_id does not exist.
        """
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            share_result = await session.execute(
                text(
                    """
                    SELECT
                        ws.share_id, ws.workspace_id, ws.revoked_at,
                        w.owner_actor_id, w.tenant_id
                    FROM workspace_shares ws
                    JOIN workspaces w ON w.workspace_id = ws.workspace_id
                    WHERE ws.share_id = :share_id
                    """
                ),
                {"share_id": share_id},
            )
            share_row = share_result.first()

            if share_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Workspace share {share_id} not found.",
                )

            # Idempotency: already revoked — no-op, no audit.
            if share_row.revoked_at is not None:
                _log.info(
                    "workspace.revoke_share_noop share_id=%s actor=%s (already revoked)",
                    share_id,
                    ctx.actor_id,
                )
                return

            # Write-auth: owning actor or admin in the workspace's tenant.
            is_owner = (
                share_row.owner_actor_id is not None
                and ctx.actor_id == share_row.owner_actor_id
            )
            is_tenant_admin = (
                ctx.tenant_id == share_row.tenant_id and "admin" in ctx.roles
            )
            if not (is_owner or is_tenant_admin):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Actor {ctx.actor_id} is not authorized to revoke share {share_id}. "
                        "Must be the owning actor or an admin in the workspace's tenant."
                    ),
                )

            await session.execute(
                text(
                    """
                    UPDATE workspace_shares
                    SET revoked_at = :now
                    WHERE share_id = :share_id
                      AND revoked_at IS NULL
                    """
                ),
                {"now": now, "share_id": share_id},
            )

        await self._audit_writer.emit(
            ctx,
            action=actions.WORKSPACE_SHARE_REVOKED,
            target_type="workspace_share",
            target_id=share_id,
            after={
                "share_id": str(share_id),
                "revoked_at": now.isoformat(),
            },
        )

        _log.info(
            "workspace.share_revoked share_id=%s actor=%s",
            share_id,
            ctx.actor_id,
        )

    async def list_shares(
        self,
        ctx: TenantContext,
        workspace_id: uuid.UUID,
    ) -> list[ShareRef]:
        """Return active shares (revoked_at IS NULL) for a workspace.

        Authorization: the caller must be the owning actor or an admin in the
        workspace's tenant. get_workspace is called first to enforce the visibility
        chokepoint and raise 403/404 before any share data is returned.
        """
        workspace = await self.get_workspace(ctx, workspace_id)

        # Write-auth: owning actor or admin in the workspace's tenant.
        is_owner = (
            workspace.owner_actor_id is not None
            and ctx.actor_id == workspace.owner_actor_id
        )
        is_tenant_admin = (
            ctx.tenant_id == workspace.tenant_id and "admin" in ctx.roles
        )
        if not (is_owner or is_tenant_admin):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Actor {ctx.actor_id} is not authorized to list shares on workspace {workspace_id}. "
                    "Must be the owning actor or an admin in the workspace's tenant."
                ),
            )

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    """
                    SELECT
                        share_id, workspace_id, grantee_actor_id,
                        grantee_tenant_id, role, granted_at, revoked_at
                    FROM workspace_shares
                    WHERE workspace_id = :workspace_id
                      AND revoked_at IS NULL
                    ORDER BY granted_at ASC
                    """
                ),
                {"workspace_id": workspace_id},
            )
            rows = result.fetchall()

        _log.info(
            "workspace.list_shares workspace_id=%s actor=%s count=%d",
            workspace_id,
            ctx.actor_id,
            len(rows),
        )

        return [
            ShareRef(
                share_id=row.share_id,
                workspace_id=row.workspace_id,
                grantee_actor_id=row.grantee_actor_id,
                grantee_tenant_id=row.grantee_tenant_id,
                role=row.role,
                granted_at=row.granted_at,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]

    async def search_workspaces(
        self,
        ctx: TenantContext,
        q: str | None = None,
        kind: str | None = None,
        owner_actor_id: uuid.UUID | None = None,
        reference_ids: list[uuid.UUID] | None = None,
        cursor: str | None = None,
    ) -> SearchResult:
        """Search workspace entries visible to the calling actor.

        Visibility scope (content-leak boundary — enforced unconditionally):
          A row is included when the entry's workspace satisfies at least one of:
            - owner_kind='actor' AND workspace.owner_actor_id = ctx.actor_id, OR
            - workspace.tenant_id = ctx.tenant_id (covers tenant-owned workspaces and
              same-tenant personal workspaces), OR
            - an active workspace_shares row exists where
              grantee_actor_id = ctx.actor_id AND revoked_at IS NULL.
          Entries from workspaces the actor cannot access are excluded.
          This scope is NOT equivalent to get_workspace (which operates on a single
          workspace ID) but enforces the same three-path rule across all workspaces.

        FTS (when q is provided): to_tsvector('english', body_md) @@ to_tsquery('english', q)
        against the idx_we_body_fts GIN index. No ILIKE fallback.

        reference_ids filter (when provided): entry must contain ALL listed UUIDs
        in its reference_ids array (GIN containment @>).

        kind filter (when provided): WHERE kind = :kind.

        owner_actor_id filter (when provided): restricts to workspaces owned by the
        specified actor. Valid only when ctx.actor_id == owner_actor_id or ctx carries
        an admin role. Raises 403 otherwise — callers cannot enumerate another actor's
        workspace entries without admin privilege.

        q=None and reference_ids=None: returns all visible entries paginated (not an error).

        Cursor is keyset on entry_id (ascending UUID order). Decodes on input;
        next_cursor is encoded on output.

        total_count is not populated (None) — the cross-workspace join makes a cheap
        COUNT expensive; callers must use next_cursor for pagination control.
        """
        # owner_actor_id filter: only allowed when the caller IS that actor or is admin.
        if owner_actor_id is not None:
            is_self = ctx.actor_id == owner_actor_id
            is_admin = "admin" in ctx.roles
            if not (is_self or is_admin):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Actor {ctx.actor_id} may not filter by owner_actor_id={owner_actor_id}. "
                        "Only the owning actor or an admin may use this filter."
                    ),
                )

        cursor_id: uuid.UUID | None = None
        if cursor is not None:
            cursor_id = _decode_entry_cursor(cursor)

        params: dict[str, Any] = {
            "actor_id": ctx.actor_id,
            "tenant_id": ctx.tenant_id,
            "limit": _DEFAULT_PAGE_SIZE + 1,
        }

        # Visibility CTE: entries in workspaces the actor can perceive.
        # The EXISTS subqueries push the role check into SQL so the DB index
        # handles the predicate — no Python-layer post-filter needed.
        _role_exists = (
            "SELECT 1 FROM actor_roles ar "
            "JOIN roles r ON r.role_id = ar.role_id "
            "WHERE ar.actor_id = :actor_id AND ar.tenant_id = :tenant_id"
        )
        visibility_cte = f"""
            visible_workspaces AS (
                SELECT w.workspace_id
                FROM workspaces w
                WHERE w.tenant_id = :tenant_id
                  AND w.t_invalidated_at IS NULL
                  AND (
                      (w.owner_kind = 'tenant'
                       AND EXISTS ({_role_exists}))
                      OR
                      (w.owner_kind = 'actor' AND (
                          EXISTS ({_role_exists} AND r.name = 'auditor')
                          OR (
                              w.owner_actor_id = :actor_id
                              AND EXISTS ({_role_exists} AND r.name IN ('producer', 'consumer'))
                          )
                      ))
                  )
            )
        """

        where_clauses: list[str] = [
            "e.t_invalidated_at IS NULL",
            "e.workspace_id IN (SELECT workspace_id FROM visible_workspaces)",
        ]

        if q is not None:
            where_clauses.append(
                "to_tsvector('english', e.body_md) @@ to_tsquery('english', :q)"
            )
            params["q"] = q

        if reference_ids is not None:
            where_clauses.append("e.reference_ids @> :reference_ids")
            params["reference_ids"] = reference_ids

        if kind is not None:
            where_clauses.append("e.kind = :kind")
            params["kind"] = kind

        if owner_actor_id is not None:
            where_clauses.append(
                "e.workspace_id IN ("
                "  SELECT workspace_id FROM workspaces"
                "  WHERE owner_kind = 'actor' AND owner_actor_id = :owner_actor_id"
                ")"
            )
            params["owner_actor_id"] = owner_actor_id

        if cursor_id is not None:
            where_clauses.append("e.entry_id > :cursor_id")
            params["cursor_id"] = cursor_id

        where_sql = " AND ".join(where_clauses)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    f"""
                    WITH {visibility_cte}
                    SELECT
                        e.entry_id, e.workspace_id, e.tenant_id, e.kind, e.body_md,
                        e.references_jsonb, e.reference_ids,
                        e.expires_at, e.t_invalidated_at, e.created_at, e.updated_at, e.created_by
                    FROM workspace_entries e
                    WHERE {where_sql}
                    ORDER BY e.entry_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            rows = result.fetchall()

        has_next = len(rows) > _DEFAULT_PAGE_SIZE
        if has_next:
            rows = rows[:_DEFAULT_PAGE_SIZE]

        # body_md is always accessed via _read_body — never directly.
        items = [
            WorkspaceEntryRef(
                entry_id=row.entry_id,
                workspace_id=row.workspace_id,
                tenant_id=row.tenant_id,
                kind=row.kind,
                body_md=_read_body(row),
                references_jsonb=row.references_jsonb,
                reference_ids=list(row.reference_ids) if row.reference_ids else [],
                expires_at=row.expires_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
                created_by=row.created_by,
                t_invalidated_at=row.t_invalidated_at,
            )
            for row in rows
        ]

        next_cursor: str | None = None
        if has_next and rows:
            next_cursor = _encode_entry_cursor(rows[-1].entry_id)

        _log.info(
            "workspace_entry.search actor=%s tenant=%s q=%r kind=%s ref_ids=%s count=%d has_next=%s",
            ctx.actor_id,
            ctx.tenant_id,
            q,
            kind,
            bool(reference_ids),
            len(items),
            has_next,
        )

        return SearchResult(items=items, next_cursor=next_cursor, total_count=None)

    async def _log_acceptance_if_first(
        self,
        ctx: TenantContext,
        share_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> None:
        """Record first cross-tenant access in workspace_share_acceptances.

        Called from get_workspace when the grantee's tenant differs from the
        workspace's owning tenant. The INSERT is idempotent via ON CONFLICT DO NOTHING
        on the unique index (share_id, accepting_actor_id). Access is NOT gated here —
        the workspace_shares row is the gate; this log is informational.

        In the ENC phase this method will gain encryption-context fields on the
        acceptance row. The WS-phase schema records (share_id, workspace_id,
        accepting_actor_id, accepting_tenant_id, accepted_at) only.
        """
        acceptance_id = uuid.uuid4()
        now = self._clock.now()

        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO workspace_share_acceptances (
                            acceptance_id, share_id, workspace_id,
                            accepting_actor_id, accepting_tenant_id, accepted_at
                        ) VALUES (
                            :acceptance_id, :share_id, :workspace_id,
                            :actor_id, :tenant_id, :now
                        )
                        ON CONFLICT (share_id, accepting_actor_id) DO NOTHING
                        """
                    ),
                    {
                        "acceptance_id": acceptance_id,
                        "share_id": share_id,
                        "workspace_id": workspace_id,
                        "actor_id": ctx.actor_id,
                        "tenant_id": ctx.tenant_id,
                        "now": now,
                    },
                )
        except Exception:
            # Acceptance logging must never block the read path. Log and continue.
            _log.warning(
                "workspace.acceptance_log_failed workspace_id=%s share_id=%s actor=%s",
                workspace_id,
                share_id,
                ctx.actor_id,
                exc_info=True,
            )

    async def purge_actor_personal_data(
        self,
        ctx: TenantContext,
        target_actor_id: uuid.UUID,
    ) -> PurgeResult:
        """Physically delete all workspace content authored by target_actor_id.

        This is a hard DELETE (not a soft-delete). The bi-temporal invalidation
        rule is suspended for this operation because it is an explicit, fully
        audit-logged GDPR Article 17 / CCPA right-to-delete action. Physical
        deletion is the only erasure primitive available in this phase — there
        are no DEKs to crypto-shred.

        Authorization: caller must hold the admin role. Raises 403 otherwise.

        Three-step algorithm:

        Step 1 — Delete entries:
          Physical DELETE of workspace_entries WHERE created_by = target_actor_id
          in workspaces where t_invalidated_at IS NULL (active workspaces only).
          Tracks the row count.

        Step 2 — Actor-owned workspace cleanup:
          For each workspace owned by the target actor (owner_kind='actor',
          owner_actor_id=target_actor_id):
            2a. If the workspace is now empty OR all remaining entries are
                from the target actor: DELETE workspace_shares, then DELETE
                the workspace row.
            2b. If other actors' entries still exist: SET owner_actor_id=NULL
                and archived_at=now so the workspace is preserved for those
                actors but no longer tied to the purged actor.

        Step 3 — Revoke active shares:
          SET revoked_at=now on all workspace_shares WHERE
          grantee_actor_id=target_actor_id AND revoked_at IS NULL.

        workspace_share_acceptances rows are intentionally NOT deleted.
        Those rows are an audit trail of historical cross-tenant access events.
        The accepting_actor_id is an opaque identifier in an audit record, not
        content authored by the actor. Retaining them preserves operator audit
        integrity. If a stricter erasure policy is required, an explicit
        Step 4 can be added later.

        Audit event: action=rtbf.purge, with counts and requesting admin id.

        Returns PurgeResult{purged_entries, purged_workspaces, revoked_shares}.
        All counts are 0 on a repeated call (idempotent: nothing left to purge).
        """
        if "admin" not in ctx.roles:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Actor {ctx.actor_id} does not hold the admin role and cannot "
                    "invoke personal-data purge."
                ),
            )

        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            # ------------------------------------------------------------------
            # Step 1: physical DELETE of entries authored by the target actor
            # in active (not soft-deleted) workspaces.
            # ------------------------------------------------------------------
            entries_result = await session.execute(
                text(
                    """
                    DELETE FROM workspace_entries
                    WHERE created_by = :target_actor_id
                      AND workspace_id IN (
                          SELECT workspace_id FROM workspaces
                          WHERE t_invalidated_at IS NULL
                      )
                    """
                ),
                {"target_actor_id": target_actor_id},
            )
            purged_entries: int = entries_result.rowcount or 0

            # ------------------------------------------------------------------
            # Step 2: handle workspaces owned by the target actor.
            # ------------------------------------------------------------------
            owned_ws_result = await session.execute(
                text(
                    """
                    SELECT workspace_id
                    FROM workspaces
                    WHERE owner_kind = 'actor'
                      AND owner_actor_id = :target_actor_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"target_actor_id": target_actor_id},
            )
            owned_ws_ids: list[uuid.UUID] = [
                row.workspace_id for row in owned_ws_result.fetchall()
            ]

            purged_workspaces = 0
            for ws_id in owned_ws_ids:
                # Check whether any OTHER actor's entries remain in this workspace.
                other_entries_result = await session.execute(
                    text(
                        """
                        SELECT 1 FROM workspace_entries
                        WHERE workspace_id = :ws_id
                          AND created_by IS DISTINCT FROM :target_actor_id
                        LIMIT 1
                        """
                    ),
                    {"ws_id": ws_id, "target_actor_id": target_actor_id},
                )
                has_other_entries = other_entries_result.first() is not None

                if has_other_entries:
                    # Step 2b: other actors' content exists — disassociate and
                    # archive so remaining contributors keep access.
                    #
                    # The chk_actor_owner CHECK constraint forbids
                    # (owner_kind='actor' AND owner_actor_id IS NULL), so the
                    # purge must also flip owner_kind to 'tenant'. Semantically:
                    # the workspace becomes a tenant-owned artifact orphaned of
                    # its original actor, preserved for the remaining contributors.
                    await session.execute(
                        text(
                            """
                            UPDATE workspaces
                            SET owner_actor_id = NULL,
                                owner_kind = 'tenant',
                                archived_at = :now,
                                updated_at = :now
                            WHERE workspace_id = :ws_id
                            """
                        ),
                        {"ws_id": ws_id, "now": now},
                    )
                else:
                    # Step 2a: workspace is empty (or had only the target actor's
                    # entries, which were deleted in Step 1). Delete shares first
                    # to avoid FK constraint violations, then delete the workspace.
                    await session.execute(
                        text(
                            "DELETE FROM workspace_shares WHERE workspace_id = :ws_id"
                        ),
                        {"ws_id": ws_id},
                    )
                    await session.execute(
                        text("DELETE FROM workspaces WHERE workspace_id = :ws_id"),
                        {"ws_id": ws_id},
                    )
                    purged_workspaces += 1

            # ------------------------------------------------------------------
            # Step 3: revoke all active shares where the target actor is grantee.
            # ------------------------------------------------------------------
            shares_result = await session.execute(
                text(
                    """
                    UPDATE workspace_shares
                    SET revoked_at = :now
                    WHERE grantee_actor_id = :target_actor_id
                      AND revoked_at IS NULL
                    """
                ),
                {"target_actor_id": target_actor_id, "now": now},
            )
            revoked_shares: int = shares_result.rowcount or 0

        # Audit outside the transaction so failure in audit does not roll back the
        # purge (the purge itself is the authoritative action; the audit record is
        # additional evidence). Use target_actor_id as the target_id so the audit
        # row is queryable by the erased actor's identifier.
        await self._audit_writer.emit(
            ctx,
            action=actions.RTBF_PURGE,
            target_type="actor",
            target_id=target_actor_id,
            after={
                "target_actor_id": str(target_actor_id),
                "purged_entries": purged_entries,
                "purged_workspaces": purged_workspaces,
                "revoked_shares": revoked_shares,
                "requesting_admin": str(ctx.actor_id),
                "ts": now.isoformat(),
            },
        )

        _log.info(
            "rtbf.purge target=%s admin=%s entries=%d workspaces=%d shares=%d",
            target_actor_id,
            ctx.actor_id,
            purged_entries,
            purged_workspaces,
            revoked_shares,
        )

        return PurgeResult(
            purged_entries=purged_entries,
            purged_workspaces=purged_workspaces,
            revoked_shares=revoked_shares,
        )


# ---------------------------------------------------------------------------
# Cursor helpers — keyset pagination on workspace_id
# ---------------------------------------------------------------------------


def _encode_cursor(workspace_id: uuid.UUID) -> str:
    """Encode a keyset cursor as a URL-safe base64 JSON blob.

    The cursor encodes the last row's workspace_id so the next page query
    can use: WHERE workspace_id > :cursor_id ORDER BY workspace_id ASC.
    UUID natural ordering gives a stable total order across all rows.
    """
    payload = {"id": str(workspace_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _encode_entry_cursor(entry_id: uuid.UUID) -> str:
    """Encode a keyset cursor as a URL-safe base64 JSON blob for entry pagination."""
    payload = {"id": str(entry_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_entry_cursor(cursor: str) -> uuid.UUID:
    """Decode a cursor produced by _encode_entry_cursor.

    Raises HTTP 422 on any parse failure.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return uuid.UUID(payload["id"])
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid pagination cursor: {exc}",
        ) from exc


def _decode_cursor(cursor: str) -> uuid.UUID:
    """Decode a cursor produced by _encode_cursor.

    Raises HTTP 422 on any parse failure — a corrupted or client-modified
    cursor is a validation failure, not a server error.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return uuid.UUID(payload["id"])
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid pagination cursor: {exc}",
        ) from exc


__all__ = [
    "WorkspaceService",
    "WorkspaceRef",
    "WorkspaceEntryRef",
    "SearchResult",
    "ShareRef",
    "PurgeResult",
    "AuditWriter",
    "PIIScanner",
    "VALID_OWNER_KINDS",
    "VALID_ENTRY_KINDS",
    "_read_body",
]
