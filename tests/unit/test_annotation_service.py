"""Unit tests for AnnotationService and annotation REST router.

Covers create_annotation, get_annotation, list_annotations, triage_annotation,
delete_annotation, and the REST router layer (POST/GET/PATCH/DELETE endpoints).

All DB interaction is mocked at session.execute via an SQL-string-keyed router
so no Postgres is required. VisibilityService, PIIScanner, and AuditWriter are
each replaced with lightweight AsyncMock / MagicMock fixtures.

# Canonical mock-factory pattern: MagicMock whose __aenter__ returns the
# SQL-string-keyed AsyncMock session. session.begin() is separately mocked
# as an async context manager because the service uses compound async with:
#   async with self._session_factory() as session, session.begin(): ...
# Omitting the session.begin() mock causes AttributeError: __aenter__.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.api.routers.annotations import (
    AnnotationTriageRequest,
    _triage_annotation_handler,
)
from registry.service.annotations import (
    VALID_CATEGORIES,
    VALID_STATUSES,
    AnnotationRef,
    AnnotationService,
    _decode_cursor,
    _encode_cursor,
)
from registry.types import FakeClock, PiiMatchResult, PiiScanResponse, TenantContext

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()  # capability owner tenant
_TENANT_B = uuid.uuid4()  # author (consumer) tenant
_ACTOR_B = uuid.uuid4()
_CAPABILITY_ID = uuid.uuid4()
_ANNOTATION_ID = uuid.uuid4()


def _ctx(tenant: uuid.UUID = _TENANT_B, actor: uuid.UUID = _ACTOR_B) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=["consumer"])


def _pii_clean() -> MagicMock:
    """Return a PIIScanner mock that always reports no PII (advisory)."""
    scanner = MagicMock()
    scanner.scan = MagicMock(return_value=PiiScanResponse(matched_patterns=[], action_taken="advisory"))
    return scanner


def _pii_block() -> MagicMock:
    """Return a PIIScanner mock that reports a block-level PII hit."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[PiiMatchResult(name="email", offset=0, length=5, category="CONTACT")],
            action_taken="block",
        )
    )
    return scanner


def _audit_writer() -> AsyncMock:
    writer = MagicMock()
    writer.emit = AsyncMock(return_value=None)
    return writer


def _visibility(visible: bool = True) -> MagicMock:
    vis = MagicMock()
    if visible:
        vis.assert_visible = AsyncMock(return_value=None)
    else:
        vis.assert_visible = AsyncMock(side_effect=PermissionError("not visible"))
    return vis


def _make_session(
    *,
    capability_tenant_id: uuid.UUID = _TENANT_A,
    annotation_row: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build an AsyncMock session whose execute routes by SQL keywords.

    - SELECT FROM entities → returns a row with tenant_id=capability_tenant_id
    - INSERT INTO capability_annotations → no return value needed
    - SELECT FROM capability_annotations → returns annotation_row if provided, else None
    """
    executed: list[str] = []
    executed_params: list[dict[str, Any] | None] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        executed_params.append(params)
        result = MagicMock()

        if "FROM entities" in sql and "entity_id = :eid" in sql:
            row = MagicMock()
            row.tenant_id = capability_tenant_id
            result.first = MagicMock(return_value=row)
            return result

        if "FROM capability_annotations" in sql and "annotation_id = :annotation_id" in sql:
            if annotation_row is None:
                result.first = MagicMock(return_value=None)
            else:
                row = MagicMock()
                row.annotation_id = annotation_row["annotation_id"]
                row.tenant_id = annotation_row["tenant_id"]
                row.capability_id = annotation_row["capability_id"]
                row.author_actor_id = annotation_row["author_actor_id"]
                row.author_tenant_id = annotation_row["author_tenant_id"]
                row.body = annotation_row["body"]
                row.triage_note = annotation_row.get("triage_note")
                row.category = annotation_row["category"]
                row.status = annotation_row["status"]
                row.version_target = annotation_row.get("version_target")
                row.created_at = annotation_row["created_at"]
                row.updated_at = annotation_row["updated_at"]
                result.first = MagicMock(return_value=row)
            return result

        # INSERT / other — no row result needed.
        result.first = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = _execute
    session._executed = executed  # type: ignore[attr-defined]
    session._executed_params = executed_params  # type: ignore[attr-defined]
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Wrap a mock session in the two-level factory mock the service expects.

    The service calls: async with self._session_factory() as session, session.begin():
    This helper wires both async context manager levels so the compound with
    resolves to the provided session.

    factory() returns a MagicMock (cm) whose __aenter__ yields the mock session.
    session.begin() is wired as a MagicMock returning a second MagicMock with
    __aenter__/__aexit__ so the compound async-with resolves without TypeError.
    """
    # Outer context manager: entered when the service does `async with factory() as session`.
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    # Inner context manager: entered when the service does `session.begin()` in
    # the compound `async with factory() as session, session.begin():`.
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    factory = MagicMock()
    factory.return_value = cm
    return factory


def _make_service(
    *,
    session: AsyncMock | None = None,
    visibility_svc: MagicMock | None = None,
    pii_scanner: MagicMock | None = None,
    audit_writer: AsyncMock | None = None,
    clock: FakeClock | None = None,
    capability_tenant_id: uuid.UUID = _TENANT_A,
    annotation_row: dict[str, Any] | None = None,
) -> AnnotationService:
    if session is None:
        session = _make_session(
            capability_tenant_id=capability_tenant_id,
            annotation_row=annotation_row,
        )
    return AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=visibility_svc or _visibility(),
        pii_scanner=pii_scanner or _pii_clean(),
        audit_writer=audit_writer or _audit_writer(),
        clock=clock or FakeClock(_NOW),
    )


# ---------------------------------------------------------------------------
# create_annotation — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_succeeds() -> None:
    """Happy path: returns AnnotationRef with status='open' and expected fields."""
    ctx = _ctx()
    svc = _make_service(capability_tenant_id=_TENANT_A)

    ref = await svc.create_annotation(
        ctx,
        capability_id=_CAPABILITY_ID,
        body="This API is missing a retry header.",
        category="feedback",
    )

    assert isinstance(ref, AnnotationRef)
    assert ref.status == "open"
    assert ref.category == "feedback"
    assert ref.body == "This API is missing a retry header."
    assert ref.capability_id == _CAPABILITY_ID
    assert ref.author_tenant_id == ctx.tenant_id
    assert ref.author_actor_id == ctx.actor_id
    # capability owner tenant scopes the annotation row
    assert ref.tenant_id == _TENANT_A
    assert ref.triage_note is None
    assert ref.version_target is None
    assert ref.created_at == _NOW
    assert ref.updated_at == _NOW


@pytest.mark.asyncio
async def test_create_annotation_with_version_target() -> None:
    """version_target is stored and returned when provided."""
    ctx = _ctx()
    svc = _make_service(capability_tenant_id=_TENANT_A)

    ref = await svc.create_annotation(
        ctx,
        capability_id=_CAPABILITY_ID,
        body="Regression in v2.3.",
        category="bug",
        version_target="v2.3",
    )

    assert ref.version_target == "v2.3"


# ---------------------------------------------------------------------------
# create_annotation — authorization failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_403_when_not_visible() -> None:
    """When visibility_svc.assert_visible raises PermissionError, it propagates unchanged."""
    ctx = _ctx()
    svc = _make_service(visibility_svc=_visibility(visible=False))

    with pytest.raises(PermissionError):
        await svc.create_annotation(
            ctx,
            capability_id=_CAPABILITY_ID,
            body="Some feedback.",
            category="feedback",
        )


@pytest.mark.asyncio
async def test_create_annotation_assert_visible_called_before_insert() -> None:
    """assert_visible fires before any DB write — critical ordering invariant."""
    vis = _visibility(visible=False)
    svc = _make_service(visibility_svc=vis)

    with pytest.raises(PermissionError):
        await svc.create_annotation(
            _ctx(),
            capability_id=_CAPABILITY_ID,
            body="Feedback",
            category="feedback",
        )

    vis.assert_visible.assert_called_once()


# ---------------------------------------------------------------------------
# create_annotation — input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_422_invalid_category() -> None:
    """An unrecognized category raises HTTP 422."""
    ctx = _ctx()
    svc = _make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            ctx,
            capability_id=_CAPABILITY_ID,
            body="Valid body.",
            category="nonsense",
        )

    assert exc_info.value.status_code == 422
    assert "nonsense" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_create_annotation_422_empty_body() -> None:
    """An empty body string raises HTTP 422."""
    ctx = _ctx()
    svc = _make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            ctx,
            capability_id=_CAPABILITY_ID,
            body="",
            category="feedback",
        )

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("category", sorted(VALID_CATEGORIES))
async def test_create_annotation_accepts_all_valid_categories(category: str) -> None:
    """All five valid categories are accepted without raising."""
    ctx = _ctx()
    svc = _make_service()
    ref = await svc.create_annotation(ctx, capability_id=_CAPABILITY_ID, body="Test body.", category=category)
    assert ref.category == category


# ---------------------------------------------------------------------------
# create_annotation — PII scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_pii_block_raises() -> None:
    """PII scan result with policy=block raises HTTP 422 (placeholder dispatch for T07)."""
    ctx = _ctx()
    svc = _make_service(pii_scanner=_pii_block())

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            ctx,
            capability_id=_CAPABILITY_ID,
            body="Contact me at user@example.com",
            category="feedback",
        )

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "annotation.body"
    assert "categories" in detail


@pytest.mark.asyncio
async def test_create_annotation_pii_scan_called_with_correct_field_type() -> None:
    """The PII scanner is called with field_type='annotation.body'."""
    scanner = _pii_clean()
    svc = _make_service(pii_scanner=scanner)

    await svc.create_annotation(_ctx(), capability_id=_CAPABILITY_ID, body="Clean text.", category="feedback")

    scanner.scan.assert_called_once()
    call_kwargs = scanner.scan.call_args.kwargs
    assert call_kwargs["field_type"] == "annotation.body"


# ---------------------------------------------------------------------------
# create_annotation — audit emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_emits_audit_event() -> None:
    """audit_writer.emit is called once with action='annotation.created' and expected keys."""
    ctx = _ctx()
    writer = _audit_writer()
    svc = _make_service(audit_writer=writer)

    await svc.create_annotation(
        ctx,
        capability_id=_CAPABILITY_ID,
        body="Audit test.",
        category="suggestion",
    )

    writer.emit.assert_called_once()
    call_kwargs = writer.emit.call_args.kwargs
    assert call_kwargs["action"] == "annotation.created"
    after = call_kwargs["after"]
    assert "annotation_id" in after
    assert str(_CAPABILITY_ID) == after["capability_id"]
    assert str(ctx.tenant_id) == after["author_tenant_id"]
    assert str(ctx.actor_id) == after["author_actor_id"]
    assert after["category"] == "suggestion"
    assert after["status"] == "open"


@pytest.mark.asyncio
async def test_create_annotation_no_audit_on_pii_block() -> None:
    """When PII scan blocks, no audit event is emitted and no row is inserted."""
    writer = _audit_writer()
    svc = _make_service(pii_scanner=_pii_block(), audit_writer=writer)

    with pytest.raises(HTTPException):
        await svc.create_annotation(_ctx(), capability_id=_CAPABILITY_ID, body="pii body", category="feedback")

    writer.emit.assert_not_called()


# ---------------------------------------------------------------------------
# get_annotation — happy path
# ---------------------------------------------------------------------------


def _sample_annotation_row() -> dict[str, Any]:
    return {
        "annotation_id": _ANNOTATION_ID,
        "tenant_id": _TENANT_A,
        "capability_id": _CAPABILITY_ID,
        "author_actor_id": _ACTOR_B,
        "author_tenant_id": _TENANT_B,
        "body": "This is a sample annotation.",
        "triage_note": None,
        "category": "feedback",
        "status": "open",
        "version_target": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


@pytest.mark.asyncio
async def test_get_annotation_returns_existing() -> None:
    """get_annotation returns an AnnotationRef when the row exists and is active."""
    svc = _make_service(annotation_row=_sample_annotation_row())

    ref = await svc.get_annotation(_ctx(), _ANNOTATION_ID)

    assert ref.annotation_id == _ANNOTATION_ID
    assert ref.tenant_id == _TENANT_A
    assert ref.capability_id == _CAPABILITY_ID
    assert ref.author_actor_id == _ACTOR_B
    assert ref.author_tenant_id == _TENANT_B
    assert ref.body == "This is a sample annotation."
    assert ref.status == "open"
    assert ref.category == "feedback"


@pytest.mark.asyncio
async def test_get_annotation_returns_triage_note_when_set() -> None:
    """triage_note is propagated into the returned AnnotationRef."""
    row = _sample_annotation_row()
    row["triage_note"] = "Acknowledged by provider team."
    row["status"] = "triaged"
    svc = _make_service(annotation_row=row)

    ref = await svc.get_annotation(_ctx(), _ANNOTATION_ID)

    assert ref.triage_note == "Acknowledged by provider team."
    assert ref.status == "triaged"


# ---------------------------------------------------------------------------
# get_annotation — not found / invalidated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_annotation_404_when_missing() -> None:
    """get_annotation raises HTTP 404 when no active row matches annotation_id."""
    svc = _make_service(annotation_row=None)  # query returns None

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_annotation(_ctx(), _ANNOTATION_ID)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_annotation_404_when_invalidated() -> None:
    """get_annotation raises 404 when t_invalidated_at IS NOT NULL (soft-deleted row).

    The SQL query includes WHERE t_invalidated_at IS NULL so the DB returns no row
    for a soft-deleted annotation. The mock simulates this by returning None.
    """
    svc = _make_service(annotation_row=None)  # simulates DB returning 0 rows

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_annotation(_ctx(), _ANNOTATION_ID)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_annotation_uses_invalidated_at_filter() -> None:
    """The SQL issued by get_annotation includes the t_invalidated_at IS NULL predicate."""
    session = _make_session(annotation_row=None)
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )

    with pytest.raises(HTTPException):
        await svc.get_annotation(_ctx(), _ANNOTATION_ID)

    executed_sql = " ".join(session._executed)
    assert "t_invalidated_at IS NULL" in executed_sql


# ---------------------------------------------------------------------------
# triage_annotation — helpers
# ---------------------------------------------------------------------------


def _open_annotation_row() -> dict[str, Any]:
    """Annotation owned by _TENANT_A, authored by _TENANT_B, status='open'."""
    return {
        "annotation_id": _ANNOTATION_ID,
        "tenant_id": _TENANT_A,  # capability owner tenant
        "capability_id": _CAPABILITY_ID,
        "author_actor_id": _ACTOR_B,
        "author_tenant_id": _TENANT_B,
        "body": "Initial annotation body.",
        "triage_note": None,
        "category": "feedback",
        "status": "open",
        "version_target": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _annotation_row_with_status(status: str) -> dict[str, Any]:
    row = _open_annotation_row()
    row["status"] = status
    return row


def _provider_ctx() -> TenantContext:
    """Context where the caller IS the capability owner (Tenant A)."""
    return TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])


# ---------------------------------------------------------------------------
# triage_annotation — forward transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_forward_transition_succeeds() -> None:
    """Forward transition open→triaged returns the updated AnnotationRef and emits audit."""
    writer = _audit_writer()
    svc = _make_service(annotation_row=_open_annotation_row(), audit_writer=writer)
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged")

    assert ref.status == "triaged"
    assert ref.annotation_id == _ANNOTATION_ID
    # Audit must be emitted exactly once.
    writer.emit.assert_called_once()
    call_kwargs = writer.emit.call_args.kwargs
    assert call_kwargs["action"] == "annotation.triaged"
    after = call_kwargs["after"]
    assert after["old_status"] == "open"
    assert after["new_status"] == "triaged"
    assert after["triage_tenant_id"] == str(ctx.tenant_id)


@pytest.mark.asyncio
async def test_triage_forward_stores_triage_note() -> None:
    """Providing a triage_note stores it on the returned ref."""
    svc = _make_service(annotation_row=_open_annotation_row())
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged", triage_note="Under review.")

    assert ref.triage_note == "Under review."


# ---------------------------------------------------------------------------
# triage_annotation — version_target (None-as-no-change semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_sets_version_target() -> None:
    """Supplying version_target writes it via the SET clause and surfaces it on the ref."""
    session = _make_session(annotation_row=_open_annotation_row())
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(
        ctx,
        _ANNOTATION_ID,
        new_status="triaged",
        version_target="v2.3",
    )

    assert ref.version_target == "v2.3"

    # The UPDATE binds version_target='v2.3'.
    update_idx = next(i for i, sql in enumerate(session._executed) if sql.upper().lstrip().startswith("UPDATE"))
    update_sql = session._executed[update_idx]
    update_params = session._executed_params[update_idx]
    assert update_params is not None
    assert update_params.get("version_target") == "v2.3"
    assert "version_target = :version_target" in update_sql


@pytest.mark.asyncio
async def test_rest_handler_forwards_version_target() -> None:
    """REST PATCH body with version_target='v1.0' reaches the service as version_target='v1.0'."""
    fake_ref = AnnotationRef(
        annotation_id=_ANNOTATION_ID,
        tenant_id=_TENANT_A,
        capability_id=_CAPABILITY_ID,
        author_actor_id=uuid.uuid4(),
        author_tenant_id=_TENANT_B,
        body="b",
        triage_note=None,
        category="feedback",
        status="triaged",
        version_target="v1.0",
        created_at=_NOW,
        updated_at=_NOW,
        warnings=None,
    )
    svc = MagicMock()
    svc.triage_annotation = AsyncMock(return_value=fake_ref)
    ctx = _provider_ctx()
    body = AnnotationTriageRequest(status="triaged", version_target="v1.0")

    await _triage_annotation_handler(
        annotation_id=_ANNOTATION_ID,
        body=body,
        svc=svc,
        ctx=ctx,
    )

    call_kwargs = svc.triage_annotation.call_args.kwargs
    assert call_kwargs["version_target"] == "v1.0"
    assert call_kwargs["new_status"] == "triaged"


@pytest.mark.asyncio
async def test_triage_preserves_version_target_when_omitted() -> None:
    """Omitting version_target leaves the stored value intact; SET clause does not name it."""
    pre_existing = _open_annotation_row()
    pre_existing["version_target"] = "v1.0"
    session = _make_session(annotation_row=pre_existing)
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged")

    # Pre-triage value is preserved on the returned ref.
    assert ref.version_target == "v1.0"

    # The UPDATE's SET clause does not mention version_target — the column is
    # omitted entirely so the stored value is left unchanged.
    update_sqls = [sql for sql in session._executed if sql.upper().lstrip().startswith("UPDATE")]
    assert len(update_sqls) == 1
    assert "version_target" not in update_sqls[0]


@pytest.mark.asyncio
async def test_triage_without_triage_note_preserves_existing_note_in_db() -> None:
    """Omitting triage_note leaves the stored note intact; SET clause does not name it."""
    pre_existing = _open_annotation_row()
    pre_existing["triage_note"] = "Awaiting vendor response."
    session = _make_session(annotation_row=pre_existing)
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged")

    # Pre-triage value is preserved on the returned ref.
    assert ref.triage_note == "Awaiting vendor response."

    # The UPDATE's SET clause does not mention triage_note — the column is
    # omitted entirely so the stored value is left unchanged. Bind params do
    # not include triage_note either.
    update_idx = next(i for i, sql in enumerate(session._executed) if sql.upper().lstrip().startswith("UPDATE"))
    update_sql = session._executed[update_idx]
    update_params = session._executed_params[update_idx]
    assert "triage_note" not in update_sql
    assert update_params is not None
    assert "triage_note" not in update_params


# ---------------------------------------------------------------------------
# triage_annotation — reverse transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_reverse_transition_succeeds() -> None:
    """Reverse transition closed→triaged is explicitly allowed and emits audit."""
    writer = _audit_writer()
    row = _annotation_row_with_status("closed")
    svc = _make_service(annotation_row=row, audit_writer=writer)
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged")

    assert ref.status == "triaged"
    writer.emit.assert_called_once()
    call_kwargs = writer.emit.call_args.kwargs
    after = call_kwargs["after"]
    assert after["old_status"] == "closed"
    assert after["new_status"] == "triaged"


# ---------------------------------------------------------------------------
# triage_annotation — self-transition no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_self_transition_is_noop() -> None:
    """Self-transition returns 200 with unchanged ref; no audit entry is written."""
    writer = _audit_writer()
    row = _annotation_row_with_status("triaged")
    svc = _make_service(annotation_row=row, audit_writer=writer)
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged")

    assert ref.status == "triaged"
    # No audit must be emitted for a self-transition.
    writer.emit.assert_not_called()


@pytest.mark.asyncio
async def test_triage_self_transition_does_not_issue_update() -> None:
    """Self-transition short-circuits before issuing any UPDATE statement."""
    session = _make_session(annotation_row=_annotation_row_with_status("acknowledged"))
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )
    ctx = _provider_ctx()

    await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="acknowledged")

    # Only the SELECT (get_annotation) should appear — no UPDATE.
    update_calls = [sql for sql in session._executed if sql.upper().lstrip().startswith("UPDATE")]
    assert update_calls == [], f"Expected no UPDATE, got: {update_calls}"


# ---------------------------------------------------------------------------
# triage_annotation — authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_non_owner_tenant_403() -> None:
    """A caller whose tenant does not own the capability receives HTTP 403."""
    svc = _make_service(annotation_row=_open_annotation_row())
    # _TENANT_B is the author, not the capability owner (_TENANT_A).
    consumer_ctx = _ctx(tenant=_TENANT_B)

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(consumer_ctx, _ANNOTATION_ID, new_status="triaged")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_triage_third_tenant_403() -> None:
    """A caller from an unrelated tenant (neither owner nor author) receives HTTP 403."""
    svc = _make_service(annotation_row=_open_annotation_row())
    third_tenant = uuid.uuid4()
    third_ctx = TenantContext(tenant_id=third_tenant, actor_id=uuid.uuid4(), roles=["consumer"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(third_ctx, _ANNOTATION_ID, new_status="triaged")

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# triage_annotation — status validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_invalid_status_422() -> None:
    """An unrecognized new_status raises HTTP 422."""
    svc = _make_service(annotation_row=_open_annotation_row())
    ctx = _provider_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="invalid_value")

    assert exc_info.value.status_code == 422
    assert "invalid_value" in str(exc_info.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
async def test_triage_accepts_all_valid_statuses(status: str) -> None:
    """All four valid statuses are accepted from 'open' base status."""
    row = _open_annotation_row()
    # Ensure open→open self-transition is the only case that returns early.
    # Use a different base status for the 'open' param to avoid false no-op.
    if status == "open":
        row["status"] = "triaged"  # triaged→open is a reverse transition — also valid.
    svc = _make_service(annotation_row=row)
    ctx = _provider_ctx()

    ref = await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status=status)

    assert ref.status == status


# ---------------------------------------------------------------------------
# triage_annotation — 404 when annotation missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_404_when_annotation_missing() -> None:
    """triage_annotation raises 404 when the annotation does not exist."""
    svc = _make_service(annotation_row=None)
    ctx = _provider_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged")

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# triage_annotation — PII scan on triage_note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_pii_block_on_triage_note() -> None:
    """PII block on triage_note raises HTTP 422 before any UPDATE is issued."""
    writer = _audit_writer()
    session = _make_session(annotation_row=_open_annotation_row())
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_block(),
        audit_writer=writer,
        clock=FakeClock(_NOW),
    )
    ctx = _provider_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged", triage_note="Contact: user@example.com")

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "annotation.triage_note"
    # No UPDATE and no audit when PII blocks.
    update_calls = [sql for sql in session._executed if sql.upper().lstrip().startswith("UPDATE")]
    assert update_calls == []
    writer.emit.assert_not_called()


@pytest.mark.asyncio
async def test_triage_pii_scan_skipped_when_triage_note_none() -> None:
    """PII scanner is not called when triage_note is None."""
    scanner = _pii_clean()
    svc = _make_service(annotation_row=_open_annotation_row(), pii_scanner=scanner)
    ctx = _provider_ctx()

    await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged", triage_note=None)

    # Scanner should not have been called for triage_note (only for body in create).
    # In triage_annotation without create, scanner.scan must not be called at all.
    scanner.scan.assert_not_called()


# ---------------------------------------------------------------------------
# delete_annotation — helpers
# ---------------------------------------------------------------------------


def _make_delete_session(
    *,
    row_exists: bool = True,
    already_invalidated: bool = False,
) -> AsyncMock:
    """Build an AsyncMock session for delete_annotation tests.

    The delete path issues a SELECT without t_invalidated_at IS NULL so we route
    on the broader 'FROM capability_annotations' match and distinguish by whether
    the row exists and whether it is already soft-deleted.
    """
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()

        if "FROM capability_annotations" in sql and "annotation_id = :annotation_id" in sql:
            if not row_exists:
                result.first = MagicMock(return_value=None)
            else:
                row = MagicMock()
                row.annotation_id = _ANNOTATION_ID
                row.tenant_id = _TENANT_A
                row.capability_id = _CAPABILITY_ID
                row.author_actor_id = _ACTOR_B
                row.author_tenant_id = _TENANT_B
                row.body = "A sample annotation."
                row.triage_note = None
                row.category = "feedback"
                row.status = "open"
                row.version_target = None
                row.created_at = _NOW
                row.updated_at = _NOW
                row.t_invalidated_at = _NOW if already_invalidated else None
                result.first = MagicMock(return_value=row)
            return result

        # UPDATE and other statements — no result row needed.
        result.first = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = _execute
    session._executed = executed  # type: ignore[attr-defined]
    return session


def _make_delete_service(
    *,
    row_exists: bool = True,
    already_invalidated: bool = False,
    audit_writer: AsyncMock | None = None,
) -> tuple[AnnotationService, AsyncMock, AsyncMock]:
    """Return (service, session, audit_writer) ready for delete tests."""
    session = _make_delete_session(row_exists=row_exists, already_invalidated=already_invalidated)
    writer = audit_writer or _audit_writer()
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=writer,
        clock=FakeClock(_NOW),
    )
    return svc, session, writer


# ---------------------------------------------------------------------------
# delete_annotation — tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_annotation_author_can_delete_own() -> None:
    """Author (ctx.actor_id == annotation.author_actor_id) may delete their own annotation."""
    svc, session, writer = _make_delete_service()
    # _ACTOR_B is the author; use a tenant that is NOT the owner to isolate the author path.
    ctx = TenantContext(tenant_id=uuid.uuid4(), actor_id=_ACTOR_B, roles=["consumer"])

    await svc.delete_annotation(ctx, _ANNOTATION_ID)

    # UPDATE must have been issued.
    update_calls = [s for s in session._executed if s.upper().lstrip().startswith("UPDATE")]
    assert len(update_calls) == 1
    assert "t_invalidated_at" in update_calls[0]

    # Audit must be emitted once with the correct action.
    writer.emit.assert_called_once()
    call_kwargs = writer.emit.call_args.kwargs
    assert call_kwargs["action"] == "annotation.deleted"
    after = call_kwargs["after"]
    assert after["annotation_id"] == str(_ANNOTATION_ID)
    assert after["deleted_by"] == str(ctx.actor_id)


@pytest.mark.asyncio
async def test_delete_annotation_capability_owner_can_delete() -> None:
    """Capability-owner tenant (ctx.tenant_id == annotation.tenant_id) may delete any annotation."""
    svc, session, writer = _make_delete_service()
    # _TENANT_A is the capability owner; actor differs from author (_ACTOR_B).
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    await svc.delete_annotation(ctx, _ANNOTATION_ID)

    update_calls = [s for s in session._executed if s.upper().lstrip().startswith("UPDATE")]
    assert len(update_calls) == 1
    writer.emit.assert_called_once()
    call_kwargs = writer.emit.call_args.kwargs
    assert call_kwargs["action"] == "annotation.deleted"


@pytest.mark.asyncio
async def test_delete_annotation_third_tenant_403() -> None:
    """A caller who is neither the author nor the capability-owner tenant receives HTTP 403."""
    svc, session, writer = _make_delete_service()
    third_tenant = uuid.uuid4()
    third_actor = uuid.uuid4()
    ctx = TenantContext(tenant_id=third_tenant, actor_id=third_actor, roles=["consumer"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.delete_annotation(ctx, _ANNOTATION_ID)

    assert exc_info.value.status_code == 403
    # No UPDATE and no audit on 403.
    update_calls = [s for s in session._executed if s.upper().lstrip().startswith("UPDATE")]
    assert update_calls == []
    writer.emit.assert_not_called()


@pytest.mark.asyncio
async def test_delete_annotation_idempotent_no_op() -> None:
    """Calling delete on an already-invalidated row returns without error and emits no audit."""
    svc, session, writer = _make_delete_service(already_invalidated=True)
    # Use the capability-owner tenant so authorization would pass if we reached that step.
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    # Must not raise.
    await svc.delete_annotation(ctx, _ANNOTATION_ID)

    # No audit emitted on idempotent no-op.
    writer.emit.assert_not_called()


@pytest.mark.asyncio
async def test_delete_annotation_404_when_missing() -> None:
    """delete_annotation raises HTTP 404 when the row does not exist at all."""
    svc, _session, writer = _make_delete_service(row_exists=False)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.delete_annotation(ctx, _ANNOTATION_ID)

    assert exc_info.value.status_code == 404
    writer.emit.assert_not_called()


# ---------------------------------------------------------------------------
# list_annotations — session mock helpers
# ---------------------------------------------------------------------------

_TENANT_C = uuid.uuid4()  # third tenant — no authored annotations


def _make_list_row(
    *,
    annotation_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID = _TENANT_A,
    capability_id: uuid.UUID = _CAPABILITY_ID,
    author_actor_id: uuid.UUID = _ACTOR_B,
    author_tenant_id: uuid.UUID = _TENANT_B,
    body: str = "Sample body.",
    triage_note: str | None = None,
    category: str = "feedback",
    status: str = "open",
    version_target: str | None = None,
    t_ingested_at: datetime.datetime | None = None,
    t_invalidated_at: datetime.datetime | None = None,
) -> MagicMock:
    row = MagicMock()
    row.annotation_id = annotation_id or uuid.uuid4()
    row.tenant_id = tenant_id
    row.capability_id = capability_id
    row.author_actor_id = author_actor_id
    row.author_tenant_id = author_tenant_id
    row.body = body
    row.triage_note = triage_note
    row.category = category
    row.status = status
    row.version_target = version_target
    row.created_at = _NOW
    row.updated_at = _NOW
    row.t_ingested_at = t_ingested_at or _NOW
    row.t_invalidated_at = t_invalidated_at
    return row


def _make_list_session(
    *,
    capability_tenant_id: uuid.UUID = _TENANT_A,
    annotation_rows: list[MagicMock] | None = None,
    capability_missing: bool = False,
) -> AsyncMock:
    """Build an AsyncMock session that routes list_annotations queries.

    Routing:
      - SELECT ... FROM entities WHERE entity_id = :eid  → capability row
      - SELECT ... FROM capability_annotations WHERE ... LIMIT :limit  → list rows
      - All other queries (INSERT, UPDATE, single-annotation SELECT) → empty result
    """
    rows_to_return: list[MagicMock] = annotation_rows if annotation_rows is not None else []
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()

        if "FROM entities" in sql and "entity_id = :eid" in sql:
            if capability_missing:
                result.first = MagicMock(return_value=None)
            else:
                cap_row = MagicMock()
                cap_row.tenant_id = capability_tenant_id
                result.first = MagicMock(return_value=cap_row)
            return result

        if "FROM capability_annotations" in sql and "LIMIT" in sql.upper():
            # Respect the LIMIT parameter to simulate DB-side truncation.
            limit = (params or {}).get("limit", len(rows_to_return))
            result.fetchall = MagicMock(return_value=rows_to_return[:limit])
            return result

        # Other queries (e.g. single-row SELECT, INSERT) — no rows.
        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _execute
    session._executed = executed  # type: ignore[attr-defined]
    return session


def _make_list_service(
    *,
    annotation_rows: list[MagicMock] | None = None,
    capability_tenant_id: uuid.UUID = _TENANT_A,
    capability_missing: bool = False,
) -> tuple[AnnotationService, AsyncMock]:
    """Return (service, session) ready for list_annotations tests."""
    session = _make_list_session(
        capability_tenant_id=capability_tenant_id,
        annotation_rows=annotation_rows,
        capability_missing=capability_missing,
    )
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )
    return svc, session


# ---------------------------------------------------------------------------
# list_annotations — provider path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_provider_path_returns_all() -> None:
    """Provider tenant (capability owner) sees ALL active annotations on the capability."""
    rows = [
        _make_list_row(author_tenant_id=_TENANT_B, status="open"),
        _make_list_row(author_tenant_id=_TENANT_C, status="triaged"),
    ]
    svc, _session = _make_list_service(annotation_rows=rows, capability_tenant_id=_TENANT_A)
    # Provider context: caller IS the capability owner.
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    refs, next_cursor = await svc.list_annotations(ctx, _CAPABILITY_ID)

    assert len(refs) == 2
    assert next_cursor is None
    statuses = {r.status for r in refs}
    assert statuses == {"open", "triaged"}


# ---------------------------------------------------------------------------
# list_annotations — author path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_author_path_returns_own_only() -> None:
    """Author tenant (ctx.tenant_id == author_tenant_id) sees only their own annotations.

    The mock returns only the caller's rows because the SQL carries the
    author_tenant_id filter — we pre-filter rows to simulate DB behavior.
    """
    caller_actor = uuid.uuid4()
    # Only the row authored by TENANT_B is in the result set — DB filters the rest.
    row_b = _make_list_row(author_tenant_id=_TENANT_B, author_actor_id=caller_actor, status="open")
    svc, _session = _make_list_service(annotation_rows=[row_b], capability_tenant_id=_TENANT_A)
    ctx = TenantContext(tenant_id=_TENANT_B, actor_id=caller_actor, roles=["consumer"])

    refs, next_cursor = await svc.list_annotations(ctx, _CAPABILITY_ID)

    assert len(refs) == 1
    assert refs[0].author_tenant_id == _TENANT_B
    assert next_cursor is None


# ---------------------------------------------------------------------------
# list_annotations — third-tenant empty-list path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_third_tenant_returns_empty() -> None:
    """Third tenant (not provider, not author) receives ([], None) — NOT a 403.

    The DB returns no rows because the author_tenant_id filter matches nothing;
    the service must return an empty list without raising.
    """
    svc, _session = _make_list_service(annotation_rows=[], capability_tenant_id=_TENANT_A)
    # TENANT_C has no authored annotations and is not the provider.
    ctx = TenantContext(tenant_id=_TENANT_C, actor_id=uuid.uuid4(), roles=["consumer"])

    refs, next_cursor = await svc.list_annotations(ctx, _CAPABILITY_ID)

    assert refs == []
    assert next_cursor is None


# ---------------------------------------------------------------------------
# list_annotations — status filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_status_filter() -> None:
    """When status is provided the service passes it through and returns only matching rows.

    The mock simulates the DB having already applied the status filter by
    returning only the triaged row — confirming the service propagates the
    filter without raising and returns what the DB sends back.
    """
    triaged_row = _make_list_row(author_tenant_id=_TENANT_B, status="triaged")
    # Provider context — provider sees all, filtered by status.
    svc, _session = _make_list_service(annotation_rows=[triaged_row], capability_tenant_id=_TENANT_A)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    refs, next_cursor = await svc.list_annotations(ctx, _CAPABILITY_ID, status="triaged")

    assert len(refs) == 1
    assert refs[0].status == "triaged"
    assert next_cursor is None


# ---------------------------------------------------------------------------
# list_annotations — invalid status → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_invalid_status_422() -> None:
    """An unrecognized status value raises HTTP 422 before any DB query."""
    svc, _session = _make_list_service()
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.list_annotations(ctx, _CAPABILITY_ID, status="invalid_value")

    assert exc_info.value.status_code == 422
    assert "invalid_value" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# list_annotations — cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_cursor_pagination() -> None:
    """Cursor pagination: 25 rows, page_size=10 yields pages of 10, 10, 5 with correct cursors.

    Each page's last row's (t_ingested_at, annotation_id) becomes the next cursor.
    The third page returns next_cursor=None because it has fewer than page_size rows.
    """
    # Build 25 rows with distinct annotation_ids and monotonically increasing t_ingested_at.
    all_rows = [
        _make_list_row(
            annotation_id=uuid.uuid4(),
            t_ingested_at=_NOW + datetime.timedelta(seconds=i),
        )
        for i in range(25)
    ]

    # Page 1: no cursor → mock returns first 11 rows (page_size+1 = 11).
    page1_rows = all_rows[:11]
    session1 = _make_list_session(
        capability_tenant_id=_TENANT_A,
        annotation_rows=page1_rows,
    )
    svc1 = AnnotationService(
        session_factory=_make_factory(session1),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    refs1, cursor1 = await svc1.list_annotations(ctx, _CAPABILITY_ID, page_size=10)
    assert len(refs1) == 10
    assert cursor1 is not None

    # Page 2: cursor from page 1 → mock returns rows 11-21 (page_size+1 = 11).
    page2_rows = all_rows[10:21]
    session2 = _make_list_session(
        capability_tenant_id=_TENANT_A,
        annotation_rows=page2_rows,
    )
    svc2 = AnnotationService(
        session_factory=_make_factory(session2),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )

    refs2, cursor2 = await svc2.list_annotations(ctx, _CAPABILITY_ID, cursor=cursor1, page_size=10)
    assert len(refs2) == 10
    assert cursor2 is not None

    # Page 3: cursor from page 2 → mock returns rows 21-25 (5 rows, no extra).
    page3_rows = all_rows[20:25]
    session3 = _make_list_session(
        capability_tenant_id=_TENANT_A,
        annotation_rows=page3_rows,
    )
    svc3 = AnnotationService(
        session_factory=_make_factory(session3),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )

    refs3, cursor3 = await svc3.list_annotations(ctx, _CAPABILITY_ID, cursor=cursor2, page_size=10)
    assert len(refs3) == 5
    assert cursor3 is None


# ---------------------------------------------------------------------------
# list_annotations — bonus: invalid cursor → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_invalid_cursor_422() -> None:
    """A corrupted cursor string raises HTTP 422 before any DB annotation query."""
    svc, _session = _make_list_service()
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.list_annotations(ctx, _CAPABILITY_ID, cursor="!!!not-valid-base64!!!")

    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# list_annotations — bonus: soft-deleted rows excluded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_soft_deleted_excluded() -> None:
    """Soft-deleted annotations (t_invalidated_at IS NOT NULL) are excluded from all list results.

    The SQL WHERE clause includes t_invalidated_at IS NULL. The mock simulates the
    DB having applied this filter by returning only the active row — verifying that
    the service doesn't re-include deleted rows from the response.
    """
    active_row = _make_list_row(
        annotation_id=uuid.uuid4(),
        author_tenant_id=_TENANT_B,
        status="open",
        t_invalidated_at=None,
    )
    # The invalidated row is never returned by the mock (DB filtering already applied).
    # We only include the active row in the list so we can assert length == 1.
    svc, session = _make_list_service(annotation_rows=[active_row], capability_tenant_id=_TENANT_A)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    refs, _ = await svc.list_annotations(ctx, _CAPABILITY_ID)

    assert len(refs) == 1
    # Confirm the SQL issued always includes the invalidated_at IS NULL predicate.
    assert any("t_invalidated_at IS NULL" in sql for sql in session._executed)


# ---------------------------------------------------------------------------
# triage_annotation — deleted annotation returns 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_deleted_annotation_returns_404() -> None:
    """triage_annotation on a soft-deleted annotation raises HTTP 404.

    get_annotation filters WHERE t_invalidated_at IS NULL, so a deleted
    row is invisible to the triage caller — same as a missing row. The
    mock simulates this by returning None (DB returned zero rows).
    """
    svc = _make_service(annotation_row=None)
    ctx = _provider_ctx()

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="triaged")

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_annotations — empty result set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_empty_result_no_error() -> None:
    """When the DB returns no rows, list_annotations returns ([], None) without raising."""
    svc, _session = _make_list_service(annotation_rows=[], capability_tenant_id=_TENANT_A)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    refs, next_cursor = await svc.list_annotations(ctx, _CAPABILITY_ID)

    assert refs == []
    assert next_cursor is None


# ---------------------------------------------------------------------------
# list_annotations — cursor boundary: exactly page_size rows → cursor present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_exactly_page_size_rows_yields_cursor() -> None:
    """When the DB returns exactly page_size+1 rows (the over-fetch sentinel), a
    next_cursor is included and only page_size rows are returned to the caller.

    The service fetches page_size+1 rows to detect whether more pages exist.
    If the extra row arrives, it becomes the cursor anchor and is NOT returned in
    the result list.
    """
    page_size = 5
    # Provide page_size+1 rows so the service detects has_next=True.
    rows = [
        _make_list_row(
            annotation_id=uuid.uuid4(),
            t_ingested_at=_NOW + datetime.timedelta(seconds=i),
        )
        for i in range(page_size + 1)
    ]
    svc, _session = _make_list_service(annotation_rows=rows, capability_tenant_id=_TENANT_A)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    refs, next_cursor = await svc.list_annotations(ctx, _CAPABILITY_ID, page_size=page_size)

    assert len(refs) == page_size
    assert next_cursor is not None


# ---------------------------------------------------------------------------
# list_annotations — cursor boundary: page_size - 1 rows → no cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_fewer_than_page_size_rows_no_cursor() -> None:
    """When the DB returns fewer than page_size+1 rows, next_cursor is None.

    The service only sets next_cursor when it receives page_size+1 rows from the DB.
    Receiving page_size-1 rows means this is the last page.
    """
    page_size = 5
    # Provide only page_size-1 rows — the over-fetch sentinel is absent.
    rows = [
        _make_list_row(
            annotation_id=uuid.uuid4(),
            t_ingested_at=_NOW + datetime.timedelta(seconds=i),
        )
        for i in range(page_size - 1)
    ]
    svc, _session = _make_list_service(annotation_rows=rows, capability_tenant_id=_TENANT_A)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    refs, next_cursor = await svc.list_annotations(ctx, _CAPABILITY_ID, page_size=page_size)

    assert len(refs) == page_size - 1
    assert next_cursor is None


# ---------------------------------------------------------------------------
# Cursor helpers — encode/decode round-trip
# ---------------------------------------------------------------------------


def test_cursor_encode_decode_round_trip() -> None:
    """_encode_cursor followed by _decode_cursor reproduces the original values exactly.

    The cursor is a base64-JSON blob on (t_ingested_at, annotation_id). Any drift
    in serialisation format would corrupt pagination across restarts or deployments.
    """
    original_t = datetime.datetime(2026, 5, 12, 9, 30, 0, tzinfo=datetime.UTC)
    original_id = uuid.UUID("12345678-1234-5678-1234-567812345678")

    encoded = _encode_cursor(original_t, original_id)
    decoded_t, decoded_id = _decode_cursor(encoded)

    assert decoded_t == original_t
    assert decoded_id == original_id


def test_annotation_cursor_round_trips_with_stripped_padding() -> None:
    """A cursor with trailing '=' stripped by a URL normaliser must still decode.

    Gateways and HTTP clients commonly strip trailing '=' from base64 query
    parameters; the codec must tolerate the stripped form. _encode_cursor
    already strips on encode (so callers receive a clean string), and
    _decode_cursor restores padding before decoding — this test verifies
    both halves by stripping all trailing '=' from a freshly-encoded cursor
    and asserting it still decodes losslessly.
    """
    original_t = datetime.datetime(2026, 5, 13, 18, 14, 7, 123456, tzinfo=datetime.UTC)
    original_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    encoded = _encode_cursor(original_t, original_id)
    # _encode_cursor already strips '=' but be explicit so the test would
    # fail if a future refactor reintroduced padding on the wire.
    stripped = encoded.rstrip("=")
    assert "=" not in stripped, "cursor must not carry base64 padding on the wire"

    decoded_t, decoded_id = _decode_cursor(stripped)
    assert decoded_t == original_t
    assert decoded_id == original_id


# ---------------------------------------------------------------------------
# list_annotations — capability missing → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_capability_missing_404() -> None:
    """list_annotations raises HTTP 404 when the capability row does not exist."""
    svc, _session = _make_list_service(capability_missing=True)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.list_annotations(ctx, _CAPABILITY_ID)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_annotations — page_size clamped to _MAX_PAGE_SIZE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_page_size_clamped() -> None:
    """Supplying page_size > 200 is clamped to 200 without raising.

    The service never rejects a large page_size — it silently clamps so that
    existing integrations passing large values don't break if the cap is lowered.
    """
    rows = [_make_list_row(annotation_id=uuid.uuid4()) for _ in range(3)]
    svc, _session = _make_list_service(annotation_rows=rows, capability_tenant_id=_TENANT_A)
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    # Should not raise; the clamping happens inside the service.
    refs, _ = await svc.list_annotations(ctx, _CAPABILITY_ID, page_size=9999)

    assert len(refs) == 3


# ---------------------------------------------------------------------------
# Audit event pinning — exact action names and payload shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_audit_target_type() -> None:
    """audit_writer.emit is called with target_type='annotation' on create."""
    writer = _audit_writer()
    svc = _make_service(audit_writer=writer)

    await svc.create_annotation(
        _ctx(), capability_id=_CAPABILITY_ID, body="Audit target type check.", category="feedback"
    )

    call_kwargs = writer.emit.call_args.kwargs
    assert call_kwargs["target_type"] == "annotation"


@pytest.mark.asyncio
async def test_delete_annotation_audit_payload_fields() -> None:
    """delete_annotation audit payload carries annotation_id, deleted_by, and deleted_by_tenant_id."""
    svc, _session, writer = _make_delete_service()
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["provider"])

    await svc.delete_annotation(ctx, _ANNOTATION_ID)

    writer.emit.assert_called_once()
    call_kwargs = writer.emit.call_args.kwargs
    assert call_kwargs["action"] == "annotation.deleted"
    assert call_kwargs["target_type"] == "annotation"
    after = call_kwargs["after"]
    assert "annotation_id" in after
    assert "deleted_by" in after
    assert "deleted_by_tenant_id" in after
    assert after["deleted_by"] == str(ctx.actor_id)
    assert after["deleted_by_tenant_id"] == str(ctx.tenant_id)


@pytest.mark.asyncio
async def test_triage_audit_payload_actor_fields() -> None:
    """triage_annotation audit payload carries triage_actor_id and triage_tenant_id."""
    writer = _audit_writer()
    svc = _make_service(annotation_row=_open_annotation_row(), audit_writer=writer)
    ctx = _provider_ctx()

    await svc.triage_annotation(ctx, _ANNOTATION_ID, new_status="acknowledged")

    writer.emit.assert_called_once()
    after = writer.emit.call_args.kwargs["after"]
    assert "triage_actor_id" in after
    assert "triage_tenant_id" in after
    assert after["triage_actor_id"] == str(ctx.actor_id)
    assert after["triage_tenant_id"] == str(ctx.tenant_id)


# ---------------------------------------------------------------------------
# Router-level tests — AnnotationService mocked at the FastAPI dependency
# ---------------------------------------------------------------------------
#
# These tests exercise the HTTP translation layer: status codes, request
# validation, response shape, and warnings propagation. The AnnotationService
# is replaced with an AsyncMock so no database is needed.
#
# The build_app() helper creates a minimal FastAPI app with both annotation
# routers included and overrides the get_annotation_service dependency so
# every test controls exactly what the service returns or raises.
# ---------------------------------------------------------------------------


def _build_annotation_app(
    *,
    create_return: AnnotationRef | None = None,
    create_effect: Exception | None = None,
    list_return: tuple | None = None,
    triage_return: AnnotationRef | None = None,
    triage_effect: Exception | None = None,
    delete_effect: Exception | None = None,
    ctx: TenantContext | None = None,
) -> object:
    """Build a minimal FastAPI app with annotation routers and mocked service."""
    from fastapi import FastAPI  # noqa: PLC0415

    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.annotations import (  # noqa: PLC0415
        get_annotation_service,
        mutation_router,
        router,
    )

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    svc = MagicMock()

    # create_annotation
    if create_effect is not None:
        svc.create_annotation = AsyncMock(side_effect=create_effect)
    else:
        svc.create_annotation = AsyncMock(return_value=create_return or _make_annotation_ref())

    # list_annotations
    if list_return is None:
        list_return = ([], None)
    svc.list_annotations = AsyncMock(return_value=list_return)

    # triage_annotation
    if triage_effect is not None:
        svc.triage_annotation = AsyncMock(side_effect=triage_effect)
    else:
        svc.triage_annotation = AsyncMock(return_value=triage_return or _make_annotation_ref())

    # delete_annotation
    if delete_effect is not None:
        svc.delete_annotation = AsyncMock(side_effect=delete_effect)
    else:
        svc.delete_annotation = AsyncMock(return_value=None)

    async def _fake_svc() -> MagicMock:
        return svc

    app.dependency_overrides[get_annotation_service] = _fake_svc

    effective_ctx = (
        ctx
        if ctx is not None
        else TenantContext(
            tenant_id=_TENANT_B,
            actor_id=_ACTOR_B,
            roles=["consumer"],
        )
    )

    async def _fake_ctx() -> TenantContext:
        return effective_ctx

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


def _make_annotation_ref(
    *,
    warnings: list[dict] | None = None,
    status: str = "open",
    triage_note: str | None = None,
) -> AnnotationRef:
    """Build a minimal AnnotationRef for router test fixtures."""
    return AnnotationRef(
        annotation_id=_ANNOTATION_ID,
        tenant_id=_TENANT_A,
        capability_id=_CAPABILITY_ID,
        author_actor_id=_ACTOR_B,
        author_tenant_id=_TENANT_B,
        body="Test annotation body.",
        triage_note=triage_note,
        category="feedback",
        status=status,
        version_target=None,
        created_at=_NOW,
        updated_at=_NOW,
        warnings=warnings,
    )


# ---- POST /v1/capabilities/{capability_id}/annotations ----


def test_router_post_happy_path_returns_201() -> None:
    """POST happy path: 201 response with AnnotationResponse shape, no warnings key."""
    ref = _make_annotation_ref()
    app = _build_annotation_app(create_return=ref)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/v1/capabilities/{_CAPABILITY_ID}/annotations",
        json={"body": "This API is missing a retry header.", "category": "feedback"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["annotation_id"] == str(_ANNOTATION_ID)
    assert body["category"] == "feedback"
    assert body["status"] == "open"
    # warnings must be absent (not null) when service returns AnnotationRef(warnings=None)
    assert "warnings" not in body


def test_router_post_empty_body_returns_422() -> None:
    """POST with empty body string fails Pydantic min_length=1 validation → 422."""
    app = _build_annotation_app()
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/v1/capabilities/{_CAPABILITY_ID}/annotations",
        json={"body": "", "category": "feedback"},
    )
    assert resp.status_code == 422


def test_router_post_invalid_category_returns_422() -> None:
    """POST with unknown category is rejected by the service with 422."""
    from fastapi import HTTPException  # noqa: PLC0415

    app = _build_annotation_app(
        create_effect=HTTPException(
            status_code=422,
            detail=f"Invalid category 'nope'. Must be one of: {sorted(VALID_CATEGORIES)}.",
        )
    )
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/v1/capabilities/{_CAPABILITY_ID}/annotations",
        json={"body": "valid body", "category": "nope"},
    )
    assert resp.status_code == 422


def test_router_post_warn_policy_includes_warnings_in_response() -> None:
    """POST with warn-policy PII hit: 201 response includes warnings field."""
    ref = _make_annotation_ref(warnings=[{"field": "body", "categories": ["CONTACT"]}])
    app = _build_annotation_app(create_return=ref)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/v1/capabilities/{_CAPABILITY_ID}/annotations",
        json={"body": "Please email me at test@example.com", "category": "feedback"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "warnings" in body
    assert len(body["warnings"]) == 1
    assert body["warnings"][0]["field"] == "body"
    assert "CONTACT" in body["warnings"][0]["categories"]


def test_router_post_block_policy_returns_422() -> None:
    """POST with block-policy PII hit: service raises 422 with structured detail."""
    from fastapi import HTTPException  # noqa: PLC0415

    app = _build_annotation_app(
        create_effect=HTTPException(
            status_code=422,
            detail={"code": "pii_detected", "field": "annotation.body", "categories": ["CONTACT"]},
        )
    )
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/v1/capabilities/{_CAPABILITY_ID}/annotations",
        json={"body": "Call me at 555-1234", "category": "feedback"},
    )
    assert resp.status_code == 422


# ---- GET /v1/capabilities/{capability_id}/annotations ----


def test_router_get_list_returns_200_with_items_and_next_cursor() -> None:
    """GET list: 200 with {items, next_cursor}; cursor query param forwarded to service."""
    refs = [_make_annotation_ref(), _make_annotation_ref()]
    next_cursor = "abc123"
    app = _build_annotation_app(list_return=(refs, next_cursor))
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get(f"/v1/capabilities/{_CAPABILITY_ID}/annotations?cursor=prev_cursor")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert body["next_cursor"] == next_cursor
    assert len(body["items"]) == 2


def test_router_get_list_empty_returns_200() -> None:
    """GET list with no annotations: 200 with {items: [], next_cursor: null}."""
    app = _build_annotation_app(list_return=([], None))
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get(f"/v1/capabilities/{_CAPABILITY_ID}/annotations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_router_get_list_cursor_passed_to_service() -> None:
    """GET with cursor: service is called with the cursor value."""
    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.annotations import (  # noqa: PLC0415
        get_annotation_service,
        mutation_router,
        router,
    )

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    svc = MagicMock()
    svc.list_annotations = AsyncMock(return_value=([], None))

    async def _fake_svc() -> MagicMock:
        return svc

    async def _fake_ctx() -> TenantContext:
        return TenantContext(tenant_id=_TENANT_B, actor_id=_ACTOR_B, roles=["consumer"])

    app.dependency_overrides[get_annotation_service] = _fake_svc
    app.dependency_overrides[get_tenant_context] = _fake_ctx

    client = TestClient(app, raise_server_exceptions=True)
    client.get(f"/v1/capabilities/{_CAPABILITY_ID}/annotations?cursor=my_cursor&status=open")

    call_kwargs = svc.list_annotations.call_args.kwargs
    assert call_kwargs.get("cursor") == "my_cursor"
    assert call_kwargs.get("status") == "open"


# ---- PATCH /v1/annotations/{annotation_id} ----


def test_router_patch_happy_path_returns_200() -> None:
    """PATCH happy path: 200 with updated AnnotationResponse."""
    ref = _make_annotation_ref(status="triaged", triage_note="Acknowledged, will fix in v3.")
    # PATCH requires producer or admin role — consumer role returns 403 before the service.
    app = _build_annotation_app(
        triage_return=ref,
        ctx=TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["producer"]),
    )
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.patch(
        f"/v1/annotations/{_ANNOTATION_ID}",
        json={"status": "triaged", "triage_note": "Acknowledged, will fix in v3."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "triaged"
    assert body["triage_note"] == "Acknowledged, will fix in v3."


def test_router_patch_invalid_status_returns_422() -> None:
    """PATCH with unknown status raises 422 from the service vocabulary check."""
    from fastapi import HTTPException  # noqa: PLC0415

    app = _build_annotation_app(
        triage_effect=HTTPException(
            status_code=422,
            detail=f"Invalid status 'unknown'. Must be one of: {sorted(VALID_STATUSES)}.",
        ),
        ctx=TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["producer"]),
    )
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(
        f"/v1/annotations/{_ANNOTATION_ID}",
        json={"status": "unknown"},
    )
    assert resp.status_code == 422


def test_router_patch_triage_note_block_returns_422() -> None:
    """PATCH where triage_note triggers PII block: 422 from service."""
    from fastapi import HTTPException  # noqa: PLC0415

    app = _build_annotation_app(
        triage_effect=HTTPException(
            status_code=422,
            detail={"code": "pii_detected", "field": "annotation.triage_note", "categories": ["CONTACT"]},
        ),
        ctx=TenantContext(tenant_id=_TENANT_A, actor_id=uuid.uuid4(), roles=["producer"]),
    )
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(
        f"/v1/annotations/{_ANNOTATION_ID}",
        json={"status": "triaged", "triage_note": "Call 555-1234"},
    )
    assert resp.status_code == 422


def test_router_patch_non_owner_tenant_returns_403() -> None:
    """PATCH by non-owner tenant: service raises 403 which the router propagates."""
    from fastapi import HTTPException  # noqa: PLC0415

    app = _build_annotation_app(
        triage_effect=HTTPException(status_code=403, detail="Tenant does not own this capability."),
        ctx=TenantContext(tenant_id=_TENANT_B, actor_id=uuid.uuid4(), roles=["producer"]),
    )
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(
        f"/v1/annotations/{_ANNOTATION_ID}",
        json={"status": "triaged"},
    )
    assert resp.status_code == 403


# ---- DELETE /v1/annotations/{annotation_id} ----


def test_router_delete_happy_path_returns_204() -> None:
    """DELETE happy path: 204 No Content, empty body."""
    app = _build_annotation_app()
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.delete(f"/v1/annotations/{_ANNOTATION_ID}")
    assert resp.status_code == 204
    assert resp.content == b""


def test_router_delete_already_deleted_returns_204() -> None:
    """DELETE on already-deleted annotation: service returns None (idempotent no-op) → 204."""
    # delete_annotation returns None on idempotent re-call (no exception raised).
    app = _build_annotation_app(delete_effect=None)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.delete(f"/v1/annotations/{_ANNOTATION_ID}")
    assert resp.status_code == 204


def test_router_delete_unauthorized_returns_403() -> None:
    """DELETE by unauthorized actor: service raises 403 which the router propagates."""
    from fastapi import HTTPException  # noqa: PLC0415

    app = _build_annotation_app(
        delete_effect=HTTPException(status_code=403, detail="Not authorized to delete this annotation.")
    )
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.delete(f"/v1/annotations/{_ANNOTATION_ID}")
    assert resp.status_code == 403
