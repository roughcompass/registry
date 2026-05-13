"""AnnotationService — consumer-to-provider capability annotations.

Each annotation is written against a specific capability. The capability's
owner tenant is the authorization scope for triage and delete; the submitting
tenant is recorded as author_tenant_id so the cross-tenant attribution is
always available without an extra join.

Body access always goes through AnnotationRecord._serialize_body() rather than
reading record.body directly. That single method is the ENC-phase handoff seam:
when encryption is retrofitted, only that one method grows the conditional decrypt
branch. Scattered record.body reads would require a broad sweep at that point.

No EncryptionService parameter — annotations are plaintext-only in this phase.
Encryption is a retrofit concern for a later phase.
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
from registry.storage.models import AnnotationRecord
from registry.types import Clock, TenantContext

if TYPE_CHECKING:
    from registry.service.visibility import VisibilityService

_log = logging.getLogger(__name__)

# Closed vocabulary — matches CHECK constraint on capability_annotations.category.
VALID_CATEGORIES: frozenset[str] = frozenset({"feedback", "bug", "suggestion", "question", "doc_gap"})

# Closed vocabulary — matches CHECK constraint on capability_annotations.status.
VALID_STATUSES: frozenset[str] = frozenset({"open", "triaged", "acknowledged", "closed"})


# ---------------------------------------------------------------------------
# AuditWriter protocol — wraps the existing audit.emit function shape.
# ---------------------------------------------------------------------------


class AuditWriter(Protocol):
    """Single-method protocol satisfied by any callable with the audit.emit signature.

    The production implementation is a thin lambda or functools.partial wrapping
    registry.api.audit.emit with the session_factory bound. Tests inject a mock.
    """

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
    """Minimal protocol surface for PiiScanner used by AnnotationService.

    The full PiiScanner class in security/pii_scanner.py satisfies this protocol.
    Tests inject a mock.
    """

    def scan(
        self,
        text: str,
        *,
        field_type: str,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# AnnotationRef dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnnotationRef:
    """Immutable view of a capability annotation returned by all service methods.

    tenant_id is the capability's owner tenant (used for visibility scoping);
    author_tenant_id is the submitting tenant (used for cross-tenant attribution).
    These can differ: that difference is the consumer-to-provider feedback flow.

    warnings is populated only when the PII scanner returns a 'warn' policy on
    a write that succeeded. DB rows do not carry warnings — they are emergent
    per-write, so from_record() always sets warnings=None.
    """

    annotation_id: uuid.UUID
    tenant_id: uuid.UUID
    capability_id: uuid.UUID
    author_actor_id: uuid.UUID
    author_tenant_id: uuid.UUID
    body: str
    triage_note: str | None
    category: str
    status: str
    version_target: str | None
    created_at: datetime
    updated_at: datetime
    warnings: list[dict[str, Any]] | None = None

    @classmethod
    def from_record(cls, record: AnnotationRecord) -> AnnotationRef:
        """Build an AnnotationRef from an ORM row.

        Body is read through the _serialize_body() accessor rather than record.body
        directly. That accessor is the ENC-phase handoff seam.
        warnings defaults to None — DB rows never carry per-write PII warnings.
        """
        return cls(
            annotation_id=record.annotation_id,
            tenant_id=record.tenant_id,
            capability_id=record.capability_id,
            author_actor_id=record.author_actor_id,
            author_tenant_id=record.author_tenant_id,
            body=record._serialize_body(),
            triage_note=record.triage_note,
            category=record.category,
            status=record.status,
            version_target=record.version_target,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


# ---------------------------------------------------------------------------
# AnnotationService
# ---------------------------------------------------------------------------


class AnnotationService:
    """Service for creating and retrieving capability annotations.

    Authorization boundary: visibility_svc.assert_visible() is the load-bearing
    check that fires before any write. Bypassing it is how cross-tenant data
    exposure happens, so it must run first in create_annotation.

    No EncryptionService parameter — annotations are plaintext-only in this phase.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        visibility_svc: VisibilityService,
        pii_scanner: PIIScanner,
        audit_writer: AuditWriter,
        clock: Clock,
    ) -> None:
        # NO encryption_service parameter — plaintext-only in this phase.
        self._session_factory = session_factory
        self._visibility_svc = visibility_svc
        self._pii_scanner = pii_scanner
        self._audit_writer = audit_writer
        self._clock = clock

    async def create_annotation(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID,
        body: str,
        category: str,
        version_target: str | None = None,
    ) -> AnnotationRef:
        """Create an annotation on a capability.

        Steps (in order):
        1. Assert the caller can see the capability — raises PermissionError (403) if not.
           This is the load-bearing authorization check; it must run before any write.
        2. Validate category is in the closed vocabulary — raises 422 if not.
        3. Validate body is non-empty — raises 422 if empty.
        4. PII scan the body. policy=block raises 422 (full three-outcome dispatch in T07).
        5. Resolve the capability's owner tenant_id for the annotation row.
        6. INSERT the annotation row.
        7. Emit audit event.
        8. Return AnnotationRef.

        The annotation's tenant_id is the capability's owner tenant (for visibility
        scoping on triage/list queries); author_tenant_id is the caller's tenant
        (for cross-tenant attribution). These intentionally differ for consumer→provider feedback.
        """
        # Step 1 — authorization: caller must be able to see the capability.
        await self._visibility_svc.assert_visible(ctx, capability_id)

        # Step 2 — validate category.
        if category not in VALID_CATEGORIES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid category {category!r}. "
                    f"Must be one of: {sorted(VALID_CATEGORIES)}."
                ),
            )

        # Step 3 — validate body is non-empty.
        if not body:
            raise HTTPException(status_code=422, detail="Annotation body must not be empty.")

        # Step 4 — PII scan the body. Three-outcome dispatch:
        #   block    → raise 422, do NOT insert row.
        #   warn     → proceed with INSERT, surface warning in returned AnnotationRef.
        #   advisory → proceed silently (no client-visible signal).
        pii_result = self._pii_scanner.scan(body, field_type="annotation.body")
        body_warnings: list[dict[str, Any]] | None = None
        if pii_result.action_taken == "block":
            categories = sorted({m.category for m in pii_result.matched_patterns})
            # TODO: pii_detection_log write — table may not exist yet
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "pii_detected",
                    "field": "annotation.body",
                    "categories": categories,
                },
            )
        elif pii_result.action_taken == "warn":
            categories = sorted({m.category for m in pii_result.matched_patterns})
            body_warnings = [{"field": "body", "categories": categories}]
        # advisory: proceed silently; no client-visible signal.
        # TODO: pii_detection_log write on advisory — table may not exist yet

        now = self._clock.now()
        annotation_id = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            # Step 5 — resolve the capability's owner tenant_id.
            # The annotation row's tenant_id is scoped to the capability's owner tenant
            # so that provider-path list queries can filter by tenant_id efficiently.
            cap_result = await session.execute(
                text("SELECT tenant_id FROM entities WHERE entity_id = :eid"),
                {"eid": capability_id},
            )
            cap_row = cap_result.first()
            if cap_row is None:
                # assert_visible already checked existence; this is a defensive guard.
                raise HTTPException(status_code=404, detail=f"Capability {capability_id} not found.")
            capability_tenant_id: uuid.UUID = cap_row.tenant_id

            # Step 6 — INSERT annotation row.
            await session.execute(
                text(
                    """
                    INSERT INTO capability_annotations (
                        annotation_id, tenant_id, capability_id,
                        author_actor_id, author_tenant_id,
                        body, category, status, version_target,
                        created_at, updated_at,
                        t_valid_from, t_ingested_at
                    ) VALUES (
                        :annotation_id, :tenant_id, :capability_id,
                        :author_actor_id, :author_tenant_id,
                        :body, :category, :status, :version_target,
                        :now, :now,
                        :now, :now
                    )
                    """
                ),
                {
                    "annotation_id": annotation_id,
                    "tenant_id": capability_tenant_id,
                    "capability_id": capability_id,
                    "author_actor_id": ctx.actor_id,
                    "author_tenant_id": ctx.tenant_id,
                    "body": body,
                    "category": category,
                    "status": "open",
                    "version_target": version_target,
                    "now": now,
                },
            )

        # Step 7 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.ANNOTATION_CREATED,
            target_type="annotation",
            target_id=annotation_id,
            after={
                "annotation_id": str(annotation_id),
                "capability_id": str(capability_id),
                "author_tenant_id": str(ctx.tenant_id),
                "author_actor_id": str(ctx.actor_id),
                "category": category,
                "status": "open",
            },
        )

        _log.info(
            "annotation.created annotation_id=%s capability_id=%s author_tenant=%s",
            annotation_id,
            capability_id,
            ctx.tenant_id,
        )

        # Step 8 — return AnnotationRef built from the values written.
        # warnings is populated when the body scan triggered a 'warn' policy.
        return AnnotationRef(
            annotation_id=annotation_id,
            tenant_id=capability_tenant_id,
            capability_id=capability_id,
            author_actor_id=ctx.actor_id,
            author_tenant_id=ctx.tenant_id,
            body=body,
            triage_note=None,
            category=category,
            status="open",
            version_target=version_target,
            created_at=now,
            updated_at=now,
            warnings=body_warnings,
        )

    async def triage_annotation(
        self,
        ctx: TenantContext,
        annotation_id: uuid.UUID,
        new_status: str,
        triage_note: str | None = None,
        version_target: str | None = None,
    ) -> AnnotationRef:
        """Update the status (and optionally triage_note) of an annotation.

        Authorization: the caller's tenant must own the capability the annotation
        belongs to. Capability ownership is stored on the annotation row itself
        (tenant_id == capability's owner tenant), set at create time.

        Self-transitions (new_status == current status) are documented no-ops:
        they return 200 with the unchanged ref and do not write an audit entry.
        This prevents audit log pollution from idempotent retries.

        Both forward and reverse transitions are explicitly allowed — there is no
        enforced state machine graph beyond membership in the valid-status set.

        PII scan on triage_note: if policy==block, raises 422. Full three-outcome
        dispatch (warn/advisory) is wired in a later task.
        """
        # Step 1 — load the annotation; 404 if missing or soft-deleted.
        annotation = await self.get_annotation(ctx, annotation_id)

        # Step 2 — authorization: caller's tenant must be the capability owner tenant.
        if ctx.tenant_id != annotation.tenant_id:
            raise HTTPException(
                status_code=403,
                detail=f"Tenant {ctx.tenant_id} does not own the capability for annotation {annotation_id}.",
            )

        # Step 3 — validate new_status against the closed vocabulary.
        if new_status not in VALID_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid status {new_status!r}. "
                    f"Must be one of: {sorted(VALID_STATUSES)}."
                ),
            )

        # Step 4 — self-transition short-circuit: no DB write, no audit entry.
        if new_status == annotation.status:
            return annotation

        # Step 5 — PII scan on triage_note if provided and non-empty.
        # Skip entirely when triage_note is None or empty — no scan, no warnings.
        triage_warnings: list[dict[str, Any]] | None = None
        if triage_note:
            pii_result = self._pii_scanner.scan(triage_note, field_type="annotation.triage_note")
            if pii_result.action_taken == "block":
                categories = sorted({m.category for m in pii_result.matched_patterns})
                # TODO: pii_detection_log write — table may not exist yet
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "pii_detected",
                        "field": "annotation.triage_note",
                        "categories": categories,
                    },
                )
            elif pii_result.action_taken == "warn":
                categories = sorted({m.category for m in pii_result.matched_patterns})
                triage_warnings = [{"field": "triage_note", "categories": categories}]
            # advisory: proceed silently.
            # TODO: pii_detection_log write on advisory — table may not exist yet

        now = self._clock.now()
        old_status = annotation.status

        # Step 6 — UPDATE status, updated_at, and optionally triage_note /
        # version_target. Both optional columns follow None-as-no-change
        # semantics: when the caller omits a value, the column is left out of
        # the SET clause so the stored value is preserved. The SET clause is
        # assembled from a fixed set of literal column-fragment strings — no
        # user input enters the SQL text.
        # TODO: clearing triage_note or version_target to NULL requires a
        # sentinel (e.g. empty string) — not yet supported.
        set_parts = ["status = :new_status", "updated_at = :now"]
        params: dict[str, Any] = {
            "new_status": new_status,
            "now": now,
            "annotation_id": annotation_id,
        }
        if triage_note is not None:
            set_parts.append("triage_note = :triage_note")
            params["triage_note"] = triage_note
        if version_target is not None:
            set_parts.append("version_target = :version_target")
            params["version_target"] = version_target

        update_sql = (
            "UPDATE capability_annotations "
            f"SET {', '.join(set_parts)} "
            "WHERE annotation_id = :annotation_id "
            "  AND t_invalidated_at IS NULL"
        )

        async with self._session_factory() as session, session.begin():
            await session.execute(text(update_sql), params)

        # Step 7 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.ANNOTATION_TRIAGED,
            target_type="annotation",
            target_id=annotation_id,
            after={
                "annotation_id": str(annotation_id),
                "old_status": old_status,
                "new_status": new_status,
                "triage_actor_id": str(ctx.actor_id),
                "triage_tenant_id": str(ctx.tenant_id),
                "triage_note_present": triage_note is not None and bool(triage_note),
            },
        )

        _log.info(
            "annotation.triaged annotation_id=%s old_status=%s new_status=%s triage_tenant=%s",
            annotation_id,
            old_status,
            new_status,
            ctx.tenant_id,
        )

        # Step 8 — return updated AnnotationRef constructed from in-memory record.
        # warnings is populated when the triage_note scan triggered a 'warn' policy.
        return AnnotationRef(
            annotation_id=annotation.annotation_id,
            tenant_id=annotation.tenant_id,
            capability_id=annotation.capability_id,
            author_actor_id=annotation.author_actor_id,
            author_tenant_id=annotation.author_tenant_id,
            body=annotation.body,
            triage_note=triage_note if triage_note is not None else annotation.triage_note,
            category=annotation.category,
            status=new_status,
            version_target=(
                version_target if version_target is not None else annotation.version_target
            ),
            created_at=annotation.created_at,
            updated_at=now,
            warnings=triage_warnings,
        )

    async def delete_annotation(
        self,
        ctx: TenantContext,
        annotation_id: uuid.UUID,
    ) -> None:
        """Soft-delete an annotation by setting t_invalidated_at.

        Authorization passes if the caller is the annotation's author OR belongs
        to the capability-owner tenant. Either condition is sufficient.

        Idempotent: if t_invalidated_at is already set the call returns without
        error and without emitting a second audit entry. This prevents duplicate
        audit noise from retried HTTP requests.

        Steps:
        1. Load the row INCLUDING already-invalidated rows (bypasses the IS NULL
           filter used by get_annotation) so the idempotent re-call can detect
           the already-deleted state rather than hitting a false 404.
           - Row missing entirely → 404.
           - Row present but already invalidated → return (no-op).
        2. Authorization: author OR capability-owner tenant → 403 otherwise.
        3. UPDATE t_invalidated_at and updated_at WHERE t_invalidated_at IS NULL.
        4. Emit audit event (only on actual delete, not on idempotent no-op).
        """
        async with self._session_factory() as session, session.begin():
            # Step 1 — load the annotation row including soft-deleted rows.
            result = await session.execute(
                text(
                    """
                    SELECT
                        annotation_id, tenant_id, capability_id,
                        author_actor_id, author_tenant_id,
                        body, triage_note, category, status, version_target,
                        created_at, updated_at, t_invalidated_at
                    FROM capability_annotations
                    WHERE annotation_id = :annotation_id
                    """
                ),
                {"annotation_id": annotation_id},
            )
            row = result.first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Annotation {annotation_id} not found.")

            # Idempotent re-call: already soft-deleted — return without a second audit entry.
            if row.t_invalidated_at is not None:
                return

            # Step 2 — authorization: author OR capability-owner tenant.
            is_author = ctx.actor_id == row.author_actor_id
            is_owner_tenant = ctx.tenant_id == row.tenant_id
            if not (is_author or is_owner_tenant):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Actor {ctx.actor_id} from tenant {ctx.tenant_id} "
                        f"is not authorized to delete annotation {annotation_id}."
                    ),
                )

            now = self._clock.now()

            # Step 3 — soft-delete: set t_invalidated_at and updated_at.
            # WHERE t_invalidated_at IS NULL guards against a concurrent delete racing
            # between the SELECT above and this UPDATE; the result is still correct.
            await session.execute(
                text(
                    """
                    UPDATE capability_annotations
                    SET t_invalidated_at = :now,
                        updated_at = :now
                    WHERE annotation_id = :annotation_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"now": now, "annotation_id": annotation_id},
            )

        # Step 4 — emit audit event.
        await self._audit_writer.emit(
            ctx,
            action=actions.ANNOTATION_DELETED,
            target_type="annotation",
            target_id=annotation_id,
            after={
                "annotation_id": str(annotation_id),
                "deleted_by": str(ctx.actor_id),
                "deleted_by_tenant_id": str(ctx.tenant_id),
            },
        )

        _log.info(
            "annotation.deleted annotation_id=%s deleted_by=%s tenant=%s",
            annotation_id,
            ctx.actor_id,
            ctx.tenant_id,
        )

    async def list_annotations(
        self,
        ctx: TenantContext,
        capability_id: uuid.UUID,
        status: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> tuple[list[AnnotationRef], str | None]:
        """List active annotations on a capability with keyset cursor pagination.

        Visibility is enforced at the service layer before any query executes:
        the call to ``visibility_svc.assert_visible`` raises ``NotFoundError``
        (→ 404) for capabilities that do not exist and ``PermissionError``
        (→ 403) for capabilities the caller cannot see. This prevents a
        cross-tenant probe that would otherwise distinguish private-but-
        existing from missing capabilities via the 200/404 response gap.

        Three access paths based on the caller's tenant relationship to the
        capability — applied as the annotation-level authorship filter on top
        of the visibility check above.

        Provider path (caller's tenant_id == capability's tenant_id):
            Returns ALL active annotations on the capability, optionally
            filtered by status. The provider can see every annotation
            submitted by any consumer tenant.

        Author path (caller's tenant_id != capability's tenant_id):
            Returns only annotations where author_tenant_id == ctx.tenant_id.
            A consumer tenant sees only their own annotations, even when they
            have visibility to the capability. This preserves annotation
            privacy between competing consumers.

        Third-tenant path (caller has no authored annotations and is not the
        provider):
            Returns ([], None) — not a 403. Returning an empty list rather
            than a permission error prevents the caller from inferring whether
            annotations from other tenants exist on the capability.

        Cursor encoding: base64(json({"t": "<iso8601>", "id": "<uuid>"})) on
        (t_ingested_at ASC, annotation_id ASC). Fetch page_size+1 rows; if the
        extra row arrives the last row's fields become the next cursor and only
        page_size rows are returned. Invalid cursor → 422.
        """
        # Visibility chokepoint — must run before any DB query so a caller
        # without visibility cannot distinguish a private capability (403) from
        # a missing capability (404) by an empty-list response.
        await self._visibility_svc.assert_visible(ctx, capability_id)

        page_size = min(max(1, page_size), _MAX_PAGE_SIZE)

        # Validate status when provided.
        if status is not None and status not in VALID_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid status {status!r}. "
                    f"Must be one of: {sorted(VALID_STATUSES)}."
                ),
            )

        # Decode cursor before hitting the DB so a bad cursor fails fast.
        cursor_t: datetime | None = None
        cursor_id: uuid.UUID | None = None
        if cursor is not None:
            cursor_t, cursor_id = _decode_cursor(cursor)

        async with self._session_factory() as session, session.begin():
            # Resolve the capability's owner tenant so we can pick the access path.
            # Capabilities are persisted in `entities` (the row's `kind='capability'`).
            cap_result = await session.execute(
                text("SELECT tenant_id FROM entities WHERE entity_id = :eid"),
                {"eid": capability_id},
            )
            cap_row = cap_result.first()
            if cap_row is None:
                raise HTTPException(status_code=404, detail=f"Capability {capability_id} not found.")
            capability_tenant_id: uuid.UUID = cap_row.tenant_id

            # Third-tenant path: caller is neither the provider nor an author.
            # Detected after the query so we can short-circuit before issuing the
            # annotation SELECT. The author path handles the case where the caller
            # has authored annotations; if not, the SELECT returns empty — which is
            # the same semantic result. Both collapse to ([], None) for non-authors.
            is_provider = ctx.tenant_id == capability_tenant_id

            # Build and issue the annotation SELECT.
            params: dict[str, Any] = {
                "capability_id": capability_id,
                "limit": page_size + 1,
            }

            # Filters assembled into a list of SQL clauses (always ANDed).
            # Always-on: only active (not soft-deleted) rows.
            where_clauses = ["t_invalidated_at IS NULL", "capability_id = :capability_id"]

            if not is_provider:
                # Author path: caller's tenant sees only their own annotations.
                where_clauses.append("author_tenant_id = :author_tenant_id")
                params["author_tenant_id"] = ctx.tenant_id

            if status is not None:
                where_clauses.append("status = :status")
                params["status"] = status

            if cursor_t is not None and cursor_id is not None:
                # Compound keyset predicate — stable total order across ties on t_ingested_at.
                where_clauses.append(
                    "(t_ingested_at, annotation_id) > (:cursor_t, :cursor_id)"
                )
                params["cursor_t"] = cursor_t
                params["cursor_id"] = cursor_id

            where_sql = " AND ".join(where_clauses)

            result = await session.execute(
                text(
                    f"""
                    SELECT
                        annotation_id, tenant_id, capability_id,
                        author_actor_id, author_tenant_id,
                        body, triage_note, category, status, version_target,
                        created_at, updated_at, t_ingested_at
                    FROM capability_annotations
                    WHERE {where_sql}
                    ORDER BY t_ingested_at ASC, annotation_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            rows = result.fetchall()

        # Determine whether there is a next page.
        has_next = len(rows) > page_size
        if has_next:
            rows = rows[:page_size]

        refs = [
            AnnotationRef(
                annotation_id=row.annotation_id,
                tenant_id=row.tenant_id,
                capability_id=row.capability_id,
                author_actor_id=row.author_actor_id,
                author_tenant_id=row.author_tenant_id,
                body=row.body,
                triage_note=row.triage_note,
                category=row.category,
                status=row.status,
                version_target=row.version_target,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

        next_cursor: str | None = None
        if has_next and rows:
            last_row = rows[-1]
            next_cursor = _encode_cursor(last_row.t_ingested_at, last_row.annotation_id)

        _log.info(
            "annotation.list capability_id=%s tenant=%s is_provider=%s count=%d has_next=%s",
            capability_id,
            ctx.tenant_id,
            is_provider,
            len(refs),
            has_next,
        )

        return refs, next_cursor

    async def get_annotation(
        self,
        ctx: TenantContext,
        annotation_id: uuid.UUID,
    ) -> AnnotationRef:
        """Fetch a single active annotation by annotation_id.

        No cross-tenant filtering here — this is the internal lookup used by
        triage_annotation and delete_annotation to load the row before the caller
        performs their own authorization check. The caller is responsible for
        any tenant scoping after this returns.

        Raises 404 if the annotation does not exist or has been soft-deleted
        (t_invalidated_at IS NOT NULL).
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    """
                    SELECT
                        annotation_id, tenant_id, capability_id,
                        author_actor_id, author_tenant_id,
                        body, triage_note, category, status, version_target,
                        created_at, updated_at
                    FROM capability_annotations
                    WHERE annotation_id = :annotation_id
                      AND t_invalidated_at IS NULL
                    """
                ),
                {"annotation_id": annotation_id},
            )
            row = result.first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Annotation {annotation_id} not found.")

            return AnnotationRef(
                annotation_id=row.annotation_id,
                tenant_id=row.tenant_id,
                capability_id=row.capability_id,
                author_actor_id=row.author_actor_id,
                author_tenant_id=row.author_tenant_id,
                body=row.body,
                triage_note=row.triage_note,
                category=row.category,
                status=row.status,
                version_target=row.version_target,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )


# ---------------------------------------------------------------------------
# Cursor helpers — keyset pagination on (t_ingested_at, annotation_id)
# ---------------------------------------------------------------------------

# Maximum page size. Callers that supply a larger value are clamped silently
# rather than rejected, so existing integrations that pass a large page_size
# don't break if the cap is ever lowered.
_MAX_PAGE_SIZE = 200


def _encode_cursor(t_ingested_at: datetime, annotation_id: uuid.UUID) -> str:
    """Encode a keyset cursor as a URL-safe base64 JSON blob.

    The cursor encodes the last row's (t_ingested_at, annotation_id) so that
    the next page query can use a compound keyset predicate:
      (t_ingested_at, annotation_id) > (cursor_t, cursor_id)
    Both columns sort ASC. annotation_id as the tie-breaker guarantees a stable
    total order even when multiple rows share the same t_ingested_at value.
    """
    payload = {"t": t_ingested_at.isoformat(), "id": str(annotation_id)}
    # Strip trailing '=' so the cursor survives URL normalization layers
    # (gateways, browsers, retry libraries) that drop padding from query
    # parameters. _decode_cursor restores the padding before decoding.
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Decode a cursor produced by _encode_cursor.

    Raises HTTP 422 on any parse failure so callers get an actionable error
    rather than a cryptic 500 — a corrupted or client-modified cursor is a
    validation failure, not a server error.
    """
    try:
        # Restore padding stripped by _encode_cursor (or by an upstream gateway
        # that normalized the query parameter); base64 requires a 4-byte
        # multiple.
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return datetime.fromisoformat(payload["t"]), uuid.UUID(payload["id"])
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid pagination cursor: {exc}",
        ) from exc


__all__ = ["AnnotationService", "AnnotationRef", "AuditWriter", "PIIScanner", "VALID_CATEGORIES", "VALID_STATUSES"]
