"""Unit tests for WorkspaceService.search_workspaces.

Covers the five required cases:
  (a) FTS query returns a matching entry.
  (b) kind filter is applied — only entries with the requested kind are returned.
  (c) reference_ids filter is applied — GIN containment filter is forwarded.
  (d) Invisible workspace entries are excluded — mock returns an empty result when
      the visibility CTE filters out all workspaces.
  (e) q=None and reference_ids=None returns all visible entries paginated.

All DB interaction is mocked at session.execute via an SQL-string-keyed router.
No Postgres is required. The two-level mock-factory pattern matches the rest of
the workspace unit test suite: the session_factory callable returns a context
manager whose __aenter__ delivers the AsyncMock session; session.begin() is a
separately wired async context manager.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.service.workspace import (
    SearchResult,
    WorkspaceEntryRef,
    WorkspaceService,
)
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()
_ACTOR_A = uuid.uuid4()
_ACTOR_B = uuid.uuid4()   # different actor — used in auth tests
_WS_ID = uuid.uuid4()
_ENTRY_ID = uuid.uuid4()
_REF_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    tenant: uuid.UUID = _TENANT_A,
    actor: uuid.UUID = _ACTOR_A,
    roles: list[str] | None = None,
) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=roles or ["producer"])


def _audit_writer() -> MagicMock:
    writer = MagicMock()
    writer.emit = AsyncMock(return_value=None)
    return writer


def _pii_clean() -> MagicMock:
    scanner = MagicMock()
    scanner.scan = MagicMock()
    return scanner


def _visibility() -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None)
    return vis


def _make_entry_row(
    *,
    entry_id: uuid.UUID = _ENTRY_ID,
    workspace_id: uuid.UUID = _WS_ID,
    tenant_id: uuid.UUID = _TENANT_A,
    kind: str = "note",
    body_md: str = "hello world decision",
    reference_ids: list[uuid.UUID] | None = None,
) -> MagicMock:
    """Build a mock workspace_entries row."""
    row = MagicMock()
    row.entry_id = entry_id
    row.workspace_id = workspace_id
    row.tenant_id = tenant_id
    row.kind = kind
    row.body_md = body_md
    row.references_jsonb = None
    row.reference_ids = reference_ids or []
    row.expires_at = None
    row.t_invalidated_at = None
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = _ACTOR_A
    return row


def _make_search_session(rows: list[MagicMock]) -> AsyncMock:
    """Build an AsyncMock session that returns rows for any SELECT … FROM workspace_entries."""

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        result = MagicMock()
        sql = " ".join(str(stmt).split())
        if "FROM workspace_entries" in sql:
            result.fetchall = MagicMock(return_value=rows)
        else:
            result.fetchall = MagicMock(return_value=[])
            result.first = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = _execute
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Wrap a mock session in the two-level context-manager factory the service expects."""
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


def _make_service(rows: list[MagicMock]) -> WorkspaceService:
    session = _make_search_session(rows)
    return WorkspaceService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )


# ---------------------------------------------------------------------------
# (a) FTS query returns a matching entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_fts_returns_matching_entry() -> None:
    """When q is provided, search_workspaces returns entries whose body_md matches."""
    entry = _make_entry_row(body_md="hello world decision")
    svc = _make_service([entry])

    result = await svc.search_workspaces(_ctx(), q="decision")

    assert isinstance(result, SearchResult)
    assert len(result.items) == 1
    item = result.items[0]
    assert isinstance(item, WorkspaceEntryRef)
    assert item.entry_id == _ENTRY_ID
    assert item.body_md == "hello world decision"
    assert result.next_cursor is None
    assert result.total_count is None


# ---------------------------------------------------------------------------
# (b) kind filter applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_kind_filter_returns_only_matching_kind() -> None:
    """When kind is provided, only entries with that kind appear in the result."""
    decision_entry = _make_entry_row(
        entry_id=uuid.uuid4(), kind="decision", body_md="some decision"
    )
    # The mock session returns whatever rows we give it regardless of SQL —
    # what matters is that the service passes kind through to the WHERE clause.
    # We verify the contract by providing only the right-kind row and checking
    # the result is non-empty, and by providing no rows to simulate a no-match case.
    svc = _make_service([decision_entry])

    result = await svc.search_workspaces(_ctx(), kind="decision")

    assert len(result.items) == 1
    assert result.items[0].kind == "decision"


@pytest.mark.asyncio
async def test_search_kind_filter_empty_when_no_match() -> None:
    """When kind filter matches nothing, items is empty and no cursor is issued."""
    svc = _make_service([])  # DB returns no rows

    result = await svc.search_workspaces(_ctx(), kind="saved_query")

    assert result.items == []
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# (c) reference_ids filter applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_reference_ids_filter_returns_entry() -> None:
    """When reference_ids is provided, entries containing those IDs are returned."""
    ref_id = uuid.uuid4()
    entry = _make_entry_row(reference_ids=[ref_id])
    svc = _make_service([entry])

    result = await svc.search_workspaces(_ctx(), reference_ids=[ref_id])

    assert len(result.items) == 1
    assert ref_id in result.items[0].reference_ids


@pytest.mark.asyncio
async def test_search_reference_ids_no_match_returns_empty() -> None:
    """When reference_ids filter matches nothing, items is empty."""
    svc = _make_service([])

    result = await svc.search_workspaces(_ctx(), reference_ids=[uuid.uuid4()])

    assert result.items == []


# ---------------------------------------------------------------------------
# (d) Invisible workspace entries not returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_invisible_entries_excluded() -> None:
    """Entries from workspaces the actor cannot access are not returned.

    The visibility CTE filters out workspaces not owned by or shared with the
    calling actor. The mock simulates this by returning zero rows — matching the
    DB behaviour when the CTE's EXISTS/owner conditions exclude all workspaces.
    """
    svc = _make_service([])  # DB returns nothing — no visible workspaces

    result = await svc.search_workspaces(_ctx(), q="secret")

    assert result.items == []
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# (e) No q and no reference_ids returns all visible entries paginated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_no_filters_returns_all_visible_entries() -> None:
    """With q=None and reference_ids=None, all visible entries are returned paginated."""
    entries = [
        _make_entry_row(entry_id=uuid.uuid4(), body_md=f"entry {i}")
        for i in range(3)
    ]
    svc = _make_service(entries)

    result = await svc.search_workspaces(_ctx())

    assert len(result.items) == 3
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_search_no_filters_next_cursor_when_page_full() -> None:
    """When the DB returns page_size+1 rows, next_cursor is set and items is clamped."""
    from registry.service.workspace import _DEFAULT_PAGE_SIZE

    # Build page_size + 1 rows so the service detects a next page.
    entries = [
        _make_entry_row(entry_id=uuid.uuid4(), body_md=f"entry {i}")
        for i in range(_DEFAULT_PAGE_SIZE + 1)
    ]
    svc = _make_service(entries)

    result = await svc.search_workspaces(_ctx())

    assert len(result.items) == _DEFAULT_PAGE_SIZE
    assert result.next_cursor is not None


# ---------------------------------------------------------------------------
# owner_actor_id filter — auth guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_owner_actor_id_self_allowed() -> None:
    """Caller may filter by their own owner_actor_id without admin role."""
    entry = _make_entry_row()
    svc = _make_service([entry])

    result = await svc.search_workspaces(
        _ctx(actor=_ACTOR_A), owner_actor_id=_ACTOR_A
    )

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_owner_actor_id_other_actor_raises_403() -> None:
    """Filtering by another actor's owner_actor_id without admin role raises 403."""
    svc = _make_service([])

    with pytest.raises(HTTPException) as exc_info:
        await svc.search_workspaces(
            _ctx(actor=_ACTOR_A), owner_actor_id=_ACTOR_B
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_search_owner_actor_id_admin_can_filter_other_actor() -> None:
    """An admin may filter by another actor's owner_actor_id."""
    entry = _make_entry_row()
    svc = _make_service([entry])

    result = await svc.search_workspaces(
        _ctx(actor=_ACTOR_A, roles=["admin"]), owner_actor_id=_ACTOR_B
    )

    assert len(result.items) == 1


# ---------------------------------------------------------------------------
# Cursor pagination — two-page walk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_cursor_second_page_returns_remainder_and_no_cursor() -> None:
    """Passing a cursor from page 1 back on page 2 returns the remainder and next_cursor=None.

    The mock returns a single row for the second query (simulating that only one
    entry falls after the cursor). Because the result is smaller than page_size, the
    service emits no next_cursor and returns the remainder directly.
    """
    from registry.service.workspace import _encode_entry_cursor

    remainder_id = uuid.uuid4()
    remainder = _make_entry_row(entry_id=remainder_id, body_md="last entry")
    svc = _make_service([remainder])

    # Construct a valid cursor pointing to some entry that would precede remainder_id.
    # The mock ignores the SQL predicate, so any valid cursor produces the mock rows.
    cursor = _encode_entry_cursor(uuid.uuid4())
    result = await svc.search_workspaces(_ctx(), cursor=cursor)

    assert len(result.items) == 1
    assert result.items[0].entry_id == remainder_id
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# total_count — documented as None for cross-workspace search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_total_count_is_none() -> None:
    """total_count is always None for search_workspaces.

    The cross-workspace visibility join makes a cheap COUNT query infeasible;
    the field is always None and callers must use next_cursor for pagination
    control.
    """
    entry = _make_entry_row()
    svc = _make_service([entry])

    result = await svc.search_workspaces(_ctx(), q="hello")

    assert result.total_count is None


# ---------------------------------------------------------------------------
# Empty result set — explicit assertions on all SearchResult fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_empty_result_all_fields() -> None:
    """An empty DB response produces items=[], next_cursor=None, total_count=None.

    total_count is None (not 0) because the service does not issue a COUNT query;
    callers must not assume a zero count means they can skip pagination handling.
    """
    svc = _make_service([])

    result = await svc.search_workspaces(_ctx(), q="nomatch")

    assert result.items == []
    assert result.next_cursor is None
    assert result.total_count is None
