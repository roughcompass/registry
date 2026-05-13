"""PII scan integration tests for AnnotationService create and triage paths.

Tests here pin the three-outcome dispatch (block / warn / advisory) for both
annotation.body (create_annotation) and annotation.triage_note (triage_annotation).
All tests use AsyncMock for DB and injected mock PIIScanner — no Postgres required.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.service.annotations import AnnotationRef, AnnotationService
from registry.types import FakeClock, PiiMatchResult, PiiScanResponse, TenantContext

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()  # capability owner tenant
_TENANT_B = uuid.uuid4()  # consumer / author tenant
_ACTOR_A = uuid.uuid4()
_ACTOR_B = uuid.uuid4()
_CAPABILITY_ID = uuid.uuid4()
_ANNOTATION_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ctx_consumer() -> TenantContext:
    return TenantContext(tenant_id=_TENANT_B, actor_id=_ACTOR_B, roles=["consumer"])


def _ctx_provider() -> TenantContext:
    return TenantContext(tenant_id=_TENANT_A, actor_id=_ACTOR_A, roles=["provider"])


def _pii_block(field: str = "email") -> MagicMock:
    """PIIScanner mock that always returns block action."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[
                PiiMatchResult(name=field, offset=0, length=10, category="CONTACT")
            ],
            action_taken="block",
        )
    )
    return scanner


def _pii_warn(field: str = "email") -> MagicMock:
    """PIIScanner mock that always returns warn action."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[
                PiiMatchResult(name=field, offset=0, length=10, category="CONTACT")
            ],
            action_taken="warn",
        )
    )
    return scanner


def _pii_advisory() -> MagicMock:
    """PIIScanner mock that always returns advisory action (no PII)."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(matched_patterns=[], action_taken="advisory")
    )
    return scanner


def _audit_writer() -> MagicMock:
    writer = MagicMock()
    writer.emit = AsyncMock(return_value=None)
    return writer


def _visibility(visible: bool = True) -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None) if visible else AsyncMock(
        side_effect=PermissionError("not visible")
    )
    return vis


def _make_create_session(*, capability_tenant_id: uuid.UUID = _TENANT_A) -> AsyncMock:
    """Session mock for create_annotation: routes SELECT FROM entities + INSERT."""
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()

        if "FROM entities" in sql and "entity_id = :eid" in sql:
            row = MagicMock()
            row.tenant_id = capability_tenant_id
            result.first = MagicMock(return_value=row)
            return result

        result.first = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = _execute
    session._executed = executed
    return session


def _open_annotation_row() -> dict[str, Any]:
    return {
        "annotation_id": _ANNOTATION_ID,
        "tenant_id": _TENANT_A,
        "capability_id": _CAPABILITY_ID,
        "author_actor_id": _ACTOR_B,
        "author_tenant_id": _TENANT_B,
        "body": "Sample annotation body.",
        "triage_note": None,
        "category": "feedback",
        "status": "open",
        "version_target": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _make_triage_session(*, annotation_row: dict[str, Any] | None = None) -> AsyncMock:
    """Session mock for triage_annotation: routes SELECT + UPDATE."""
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()

        if "FROM capability_annotations" in sql and "annotation_id = :annotation_id" in sql:
            if annotation_row is None:
                result.first = MagicMock(return_value=None)
            else:
                row = MagicMock()
                for k, v in annotation_row.items():
                    setattr(row, k, v)
                result.first = MagicMock(return_value=row)
            return result

        result.first = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = _execute
    session._executed = executed
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Wrap a mock session in the two-level factory mock the service expects.

    factory() returns a MagicMock (cm) whose __aenter__ yields the mock session.
    session.begin() is wired as a MagicMock returning a second MagicMock with
    __aenter__/__aexit__ so the compound async-with resolves without TypeError.
    """
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    factory = MagicMock()
    factory.return_value = cm
    return factory


def _make_create_service(
    *,
    pii_scanner: MagicMock | None = None,
    audit_writer: MagicMock | None = None,
) -> tuple[AnnotationService, AsyncMock]:
    session = _make_create_session()
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=pii_scanner or _pii_advisory(),
        audit_writer=audit_writer or _audit_writer(),
        clock=FakeClock(_NOW),
    )
    return svc, session


def _make_triage_service(
    *,
    annotation_row: dict[str, Any] | None = None,
    pii_scanner: MagicMock | None = None,
    audit_writer: MagicMock | None = None,
) -> tuple[AnnotationService, AsyncMock]:
    row = annotation_row or _open_annotation_row()
    session = _make_triage_session(annotation_row=row)
    svc = AnnotationService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=pii_scanner or _pii_advisory(),
        audit_writer=audit_writer or _audit_writer(),
        clock=FakeClock(_NOW),
    )
    return svc, session


# ---------------------------------------------------------------------------
# create_annotation — block path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_block_raises_422() -> None:
    """block policy on body scan raises 422 with structured detail."""
    svc, _ = _make_create_service(pii_scanner=_pii_block())

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            _ctx_consumer(),
            capability_id=_CAPABILITY_ID,
            body="Contact me at user@example.com",
            category="feedback",
        )

    exc = exc_info.value
    assert exc.status_code == 422
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "annotation.body"
    assert isinstance(detail["categories"], list)
    assert len(detail["categories"]) > 0


@pytest.mark.asyncio
async def test_create_block_does_not_insert_row() -> None:
    """block policy must not issue an INSERT — no annotation row created."""
    writer = _audit_writer()
    svc, session = _make_create_service(pii_scanner=_pii_block(), audit_writer=writer)

    with pytest.raises(HTTPException):
        await svc.create_annotation(
            _ctx_consumer(),
            capability_id=_CAPABILITY_ID,
            body="SSN: 123-45-6789",
            category="bug",
        )

    insert_calls = [s for s in session._executed if "INSERT INTO capability_annotations" in s]
    assert insert_calls == [], "No INSERT should be issued on a block"
    writer.emit.assert_not_called()


@pytest.mark.asyncio
async def test_create_block_categories_list() -> None:
    """block detail['categories'] is a non-empty list of category strings."""
    svc, _ = _make_create_service(pii_scanner=_pii_block("credit_card"))

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            _ctx_consumer(),
            capability_id=_CAPABILITY_ID,
            body="Card: 4111111111111111",
            category="bug",
        )

    categories = exc_info.value.detail["categories"]
    assert isinstance(categories, list)
    assert all(isinstance(c, str) for c in categories)


# ---------------------------------------------------------------------------
# triage_annotation — block path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_block_raises_422() -> None:
    """block policy on triage_note scan raises 422 with structured detail."""
    svc, _ = _make_triage_service(pii_scanner=_pii_block())

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(
            _ctx_provider(),
            annotation_id=_ANNOTATION_ID,
            new_status="triaged",
            triage_note="Contact: user@example.com",
        )

    exc = exc_info.value
    assert exc.status_code == 422
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "annotation.triage_note"
    assert isinstance(detail["categories"], list)
    assert len(detail["categories"]) > 0


@pytest.mark.asyncio
async def test_triage_block_does_not_update_row() -> None:
    """block policy must not issue an UPDATE — annotation status unchanged."""
    writer = _audit_writer()
    svc, session = _make_triage_service(pii_scanner=_pii_block(), audit_writer=writer)

    with pytest.raises(HTTPException):
        await svc.triage_annotation(
            _ctx_provider(),
            annotation_id=_ANNOTATION_ID,
            new_status="triaged",
            triage_note="SSN: 123-45-6789",
        )

    update_calls = [s for s in session._executed if s.upper().lstrip().startswith("UPDATE")]
    assert update_calls == [], "No UPDATE should be issued on a block"
    writer.emit.assert_not_called()


# ---------------------------------------------------------------------------
# create_annotation — advisory path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_advisory_returns_201_shape() -> None:
    """Advisory policy produces a valid AnnotationRef with warnings=None."""
    svc, _ = _make_create_service(pii_scanner=_pii_advisory())

    ref = await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Great integration, no issues.",
        category="feedback",
    )

    assert isinstance(ref, AnnotationRef)
    assert ref.status == "open"
    assert ref.warnings is None


@pytest.mark.asyncio
async def test_create_advisory_scanner_called_once() -> None:
    """Advisory policy still invokes the scanner exactly once on the body."""
    scanner = _pii_advisory()
    svc, _ = _make_create_service(pii_scanner=scanner)

    await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Clean feedback text.",
        category="feedback",
    )

    scanner.scan.assert_called_once()
    call_kwargs = scanner.scan.call_args
    # First positional arg is the body text
    assert call_kwargs.args[0] == "Clean feedback text."


@pytest.mark.asyncio
async def test_create_advisory_insert_is_written() -> None:
    """Advisory policy allows the INSERT to proceed."""
    svc, session = _make_create_service(pii_scanner=_pii_advisory())

    await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Solid documentation.",
        category="doc_gap",
    )

    insert_calls = [s for s in session._executed if "INSERT INTO capability_annotations" in s]
    assert len(insert_calls) == 1, "INSERT must be issued on advisory"


# ---------------------------------------------------------------------------
# create_annotation — warn path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_warn_returns_201_shape() -> None:
    """Warn policy produces a valid AnnotationRef (write succeeds)."""
    svc, _ = _make_create_service(pii_scanner=_pii_warn())

    ref = await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Email user@example.com needs access.",
        category="feedback",
    )

    assert isinstance(ref, AnnotationRef)
    assert ref.status == "open"


@pytest.mark.asyncio
async def test_create_warn_warnings_populated() -> None:
    """Warn policy populates warnings list in returned AnnotationRef."""
    svc, _ = _make_create_service(pii_scanner=_pii_warn())

    ref = await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Email user@example.com needs access.",
        category="feedback",
    )

    assert ref.warnings is not None
    assert len(ref.warnings) == 1
    assert ref.warnings[0]["field"] == "body"
    assert isinstance(ref.warnings[0]["categories"], list)
    assert len(ref.warnings[0]["categories"]) > 0


@pytest.mark.asyncio
async def test_create_warn_categories_present_in_warning() -> None:
    """Warn entry includes the matched categories from the scanner result."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[
                PiiMatchResult(name="email", offset=0, length=10, category="CONTACT"),
                PiiMatchResult(name="phone", offset=20, length=12, category="CONTACT"),
            ],
            action_taken="warn",
        )
    )
    svc, _ = _make_create_service(pii_scanner=scanner)

    ref = await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Call 555-0100 or email user@example.com",
        category="feedback",
    )

    assert ref.warnings is not None
    # categories is a sorted deduplicated set built from matched_patterns
    assert "CONTACT" in ref.warnings[0]["categories"]


@pytest.mark.asyncio
async def test_create_warn_insert_is_written() -> None:
    """Warn policy still issues the INSERT — write is allowed."""
    svc, session = _make_create_service(pii_scanner=_pii_warn())

    await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Email user@example.com for details.",
        category="feedback",
    )

    insert_calls = [s for s in session._executed if "INSERT INTO capability_annotations" in s]
    assert len(insert_calls) == 1, "INSERT must be issued on warn"


@pytest.mark.asyncio
async def test_create_warn_two_distinct_categories_both_appear() -> None:
    """Two distinct PII categories both surface in the single warnings entry."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[
                PiiMatchResult(name="email", offset=0, length=10, category="CONTACT"),
                PiiMatchResult(name="aws_key", offset=20, length=20, category="CREDENTIALS"),
            ],
            action_taken="warn",
        )
    )
    svc, _ = _make_create_service(pii_scanner=scanner)

    ref = await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="Email and key both present.",
        category="feedback",
    )

    assert ref.warnings is not None
    categories = ref.warnings[0]["categories"]
    assert "CONTACT" in categories
    assert "CREDENTIALS" in categories


@pytest.mark.asyncio
async def test_create_block_multiple_categories_all_surface() -> None:
    """Block with multiple PII categories: all appear in error detail."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[
                PiiMatchResult(name="email", offset=0, length=10, category="CONTACT"),
                PiiMatchResult(name="ssn", offset=20, length=11, category="GOVERNMENT_ID"),
            ],
            action_taken="block",
        )
    )
    svc, _ = _make_create_service(pii_scanner=scanner)

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            _ctx_consumer(),
            capability_id=_CAPABILITY_ID,
            body="Email and SSN in body.",
            category="feedback",
        )

    categories = exc_info.value.detail["categories"]
    assert "CONTACT" in categories
    assert "GOVERNMENT_ID" in categories


# ---------------------------------------------------------------------------
# create_annotation — empty / whitespace body edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_empty_body_raises_422_before_pii_scan() -> None:
    """Empty body raises 422 at the validation step before the PII scanner is called."""
    scanner = _pii_advisory()
    svc, _ = _make_create_service(pii_scanner=scanner)

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            _ctx_consumer(),
            capability_id=_CAPABILITY_ID,
            body="",
            category="feedback",
        )

    assert exc_info.value.status_code == 422
    # Validation fires before the scanner, so scan must not have been called.
    scanner.scan.assert_not_called()


@pytest.mark.asyncio
async def test_create_whitespace_body_proceeds_to_pii_scan() -> None:
    """Whitespace-only body passes the non-empty check and reaches the PII scanner.

    The service uses `if not body:` which is falsy for '' but truthy for '   '.
    A whitespace body is treated as non-empty — PII scan fires normally.
    """
    scanner = _pii_advisory()
    svc, _ = _make_create_service(pii_scanner=scanner)

    # Whitespace body passes the `if not body` guard, so this should not raise
    # a 422 from the body-empty check. The scanner IS invoked.
    ref = await svc.create_annotation(
        _ctx_consumer(),
        capability_id=_CAPABILITY_ID,
        body="   ",
        category="feedback",
    )

    scanner.scan.assert_called_once()
    assert ref.body == "   "


# ---------------------------------------------------------------------------
# triage_annotation — advisory path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_advisory_returns_200_shape() -> None:
    """Advisory policy on triage_note produces valid AnnotationRef, warnings=None."""
    svc, _ = _make_triage_service(pii_scanner=_pii_advisory())

    ref = await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="triaged",
        triage_note="Acknowledged and routing to platform team.",
    )

    assert isinstance(ref, AnnotationRef)
    assert ref.status == "triaged"
    assert ref.warnings is None


@pytest.mark.asyncio
async def test_triage_advisory_scanner_called_with_note() -> None:
    """Advisory policy: scanner is invoked with the triage_note text."""
    scanner = _pii_advisory()
    svc, _ = _make_triage_service(pii_scanner=scanner)

    await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="triaged",
        triage_note="Clean triage note.",
    )

    scanner.scan.assert_called_once()
    assert scanner.scan.call_args.args[0] == "Clean triage note."


# ---------------------------------------------------------------------------
# triage_annotation — warn path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_warn_returns_200_shape() -> None:
    """Warn policy on triage_note produces valid AnnotationRef (write succeeds)."""
    svc, _ = _make_triage_service(pii_scanner=_pii_warn())

    ref = await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="triaged",
        triage_note="Note user@example.com for follow-up.",
    )

    assert isinstance(ref, AnnotationRef)
    assert ref.status == "triaged"


@pytest.mark.asyncio
async def test_triage_warn_warnings_populated() -> None:
    """Warn policy on triage_note populates warnings in returned AnnotationRef."""
    svc, _ = _make_triage_service(pii_scanner=_pii_warn())

    ref = await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="triaged",
        triage_note="Note user@example.com for follow-up.",
    )

    assert ref.warnings is not None
    assert len(ref.warnings) == 1
    assert ref.warnings[0]["field"] == "triage_note"
    assert isinstance(ref.warnings[0]["categories"], list)


@pytest.mark.asyncio
async def test_triage_warn_update_is_written() -> None:
    """Warn policy still issues the UPDATE — triage write is allowed."""
    svc, session = _make_triage_service(pii_scanner=_pii_warn())

    await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="triaged",
        triage_note="Note user@example.com",
    )

    update_calls = [s for s in session._executed if s.upper().lstrip().startswith("UPDATE")]
    assert len(update_calls) == 1, "UPDATE must be issued on warn"


@pytest.mark.asyncio
async def test_triage_warn_reverse_transition_still_warns() -> None:
    """Warn surfaces even on a reverse status transition (e.g. triaged → open)."""
    row = _open_annotation_row()
    row["status"] = "triaged"  # Start from triaged
    svc, _ = _make_triage_service(annotation_row=row, pii_scanner=_pii_warn())

    ref = await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="open",  # Reverse transition
        triage_note="Reopening — contact user@example.com for details.",
    )

    assert ref.status == "open"
    assert ref.warnings is not None
    assert ref.warnings[0]["field"] == "triage_note"


# ---------------------------------------------------------------------------
# triage_annotation — triage_note=None skips PII scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_none_note_skips_pii_scan() -> None:
    """When triage_note is None, the PII scanner is not called at all."""
    scanner = _pii_advisory()
    svc, _ = _make_triage_service(pii_scanner=scanner)

    await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="triaged",
        triage_note=None,
    )

    scanner.scan.assert_not_called()


@pytest.mark.asyncio
async def test_triage_none_note_warnings_is_none() -> None:
    """When triage_note is None, returned AnnotationRef.warnings is None."""
    svc, _ = _make_triage_service(pii_scanner=_pii_block())

    ref = await svc.triage_annotation(
        _ctx_provider(),
        annotation_id=_ANNOTATION_ID,
        new_status="triaged",
        triage_note=None,
    )

    assert ref.warnings is None


# ---------------------------------------------------------------------------
# triage_annotation — validation-before-scan ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_invalid_status_raises_422_before_pii_scan() -> None:
    """Status vocabulary check fires before PII scan in triage_annotation."""
    scanner = _pii_block()
    svc, _ = _make_triage_service(pii_scanner=scanner)

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(
            _ctx_provider(),
            annotation_id=_ANNOTATION_ID,
            new_status="invalid_status",
            triage_note="Note user@example.com — should not reach scan.",
        )

    assert exc_info.value.status_code == 422
    # Scanner must not have been reached because status validation fired first.
    scanner.scan.assert_not_called()


# ---------------------------------------------------------------------------
# Error detail shape — pin exact structure for callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_block_detail_shape_is_dict() -> None:
    """Block error detail is a dict with code, field, and categories keys."""
    svc, _ = _make_create_service(pii_scanner=_pii_block())

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_annotation(
            _ctx_consumer(),
            capability_id=_CAPABILITY_ID,
            body="PII body.",
            category="feedback",
        )

    detail = exc_info.value.detail
    assert set(detail.keys()) >= {"code", "field", "categories"}
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "annotation.body"


@pytest.mark.asyncio
async def test_triage_block_detail_shape_is_dict() -> None:
    """Triage block error detail is a dict with code, field, and categories keys."""
    svc, _ = _make_triage_service(pii_scanner=_pii_block())

    with pytest.raises(HTTPException) as exc_info:
        await svc.triage_annotation(
            _ctx_provider(),
            annotation_id=_ANNOTATION_ID,
            new_status="triaged",
            triage_note="PII triage note.",
        )

    detail = exc_info.value.detail
    assert set(detail.keys()) >= {"code", "field", "categories"}
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "annotation.triage_note"


# ---------------------------------------------------------------------------
# PII scanner exception propagates (scanner internal failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_scanner_exception_propagates() -> None:
    """If the PII scanner raises, the exception bubbles out of create_annotation.

    The service does not swallow scanner failures — they surface to the caller
    as 500-class errors rather than silently proceeding with unscanned content.
    """
    scanner = MagicMock()
    scanner.scan = MagicMock(side_effect=RuntimeError("scanner internal failure"))
    svc, _ = _make_create_service(pii_scanner=scanner)

    with pytest.raises(RuntimeError, match="scanner internal failure"):
        await svc.create_annotation(
            _ctx_consumer(),
            capability_id=_CAPABILITY_ID,
            body="Some body text.",
            category="feedback",
        )
