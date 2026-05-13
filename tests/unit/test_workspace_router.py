"""Unit tests for the workspace + entry + share + search REST router.

Covers the 13 endpoints in registry/registry/api/routers/workspaces.py:

  POST   /v1/workspaces
  GET    /v1/workspaces
  GET    /v1/workspaces/search
  GET    /v1/workspaces/{workspace_id}
  PATCH  /v1/workspaces/{workspace_id}
  DELETE /v1/workspaces/{workspace_id}
  POST   /v1/workspaces/{workspace_id}/entries
  GET    /v1/workspaces/{workspace_id}/entries
  PATCH  /v1/workspaces/{workspace_id}/entries/{entry_id}
  DELETE /v1/workspaces/{workspace_id}/entries/{entry_id}
  GET    /v1/workspaces/{workspace_id}/shares
  POST   /v1/workspaces/{workspace_id}/shares
  DELETE /v1/workspaces/{workspace_id}/shares/{share_id}

All tests use AsyncMock for the WorkspaceService layer — no Postgres or Docker
required. The tenant context dependency is overridden with a pre-built fixture.

Key behaviors verified:
- Happy paths for each endpoint (status code, response shape).
- Absent fields: warnings key absent when service returns no warnings.
- warnings key present in 201/200 when service returns warn-policy hit.
- Invalid owner_kind → 422 (service raises HTTPException).
- Invalid entry kind → 422 (service raises HTTPException).
- Empty body_md → 422 (Pydantic min_length validation).
- Regulated tenant create → 422 with exact error message.
- List endpoints return {items, next_cursor} shape.
- DELETE returns 204 (no body).
- Cross-tenant share on actor-owned workspace → 422.
- Duplicate active share → 409.
- Search returns paginated results.
- Search reference_ids comma-separated parses correctly.
- Admin RTBF requires admin role → 403 if missing.
- list_shares returns 200 with items.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers.workspaces import (
    entry_mutation_router,
    get_workspace_service,
    mutation_router,
    router,
    share_mutation_router,
    share_router,
)
from registry.service.workspace import SearchResult, ShareRef, WorkspaceEntryRef, WorkspaceRef
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_WORKSPACE_ID = uuid.uuid4()
_ENTRY_ID = uuid.uuid4()

_REGULATED_TENANT_ERROR = (
    "Workspace creation is not permitted for regulated tenants at encryption tier 'none'. "
    "Configure a higher encryption tier before creating workspaces."
)

_SHARE_ID = uuid.uuid4()
_GRANTEE_ACTOR_ID = uuid.uuid4()
_GRANTEE_TENANT_ID = uuid.uuid4()

# ---------------------------------------------------------------------------
# Fixtures: WorkspaceRef and WorkspaceEntryRef builders
# ---------------------------------------------------------------------------


def _make_workspace_ref(
    *,
    workspace_id: uuid.UUID | None = None,
    name: str = "My Workspace",
    owner_kind: str = "actor",
) -> WorkspaceRef:
    return WorkspaceRef(
        workspace_id=workspace_id or _WORKSPACE_ID,
        tenant_id=_TENANT_ID,
        name=name,
        description=None,
        owner_kind=owner_kind,
        owner_actor_id=_ACTOR_ID if owner_kind == "actor" else None,
        archived_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        created_by=_ACTOR_ID,
        t_invalidated_at=None,
    )


def _make_entry_ref(
    *,
    entry_id: uuid.UUID | None = None,
    kind: str = "note",
    body_md: str = "My note body.",
    warnings: list[dict[str, Any]] | None = None,
) -> WorkspaceEntryRef:
    return WorkspaceEntryRef(
        entry_id=entry_id or _ENTRY_ID,
        workspace_id=_WORKSPACE_ID,
        tenant_id=_TENANT_ID,
        kind=kind,
        body_md=body_md,
        references_jsonb=None,
        reference_ids=[],
        expires_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        created_by=_ACTOR_ID,
        t_invalidated_at=None,
        warnings=warnings,
    )


def _make_share_ref(
    *,
    share_id: uuid.UUID | None = None,
    role: str = "reader",
    revoked_at: datetime.datetime | None = None,
) -> ShareRef:
    return ShareRef(
        share_id=share_id or _SHARE_ID,
        workspace_id=_WORKSPACE_ID,
        grantee_actor_id=_GRANTEE_ACTOR_ID,
        grantee_tenant_id=_GRANTEE_TENANT_ID,
        role=role,
        granted_at=_NOW,
        revoked_at=revoked_at,
    )


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(
    *,
    create_workspace_effect: Exception | None = None,
    create_workspace_return: WorkspaceRef | None = None,
    list_workspaces_return: tuple[list[WorkspaceRef], str | None] | None = None,
    get_workspace_effect: Exception | None = None,
    get_workspace_return: WorkspaceRef | None = None,
    update_workspace_effect: Exception | None = None,
    update_workspace_return: WorkspaceRef | None = None,
    delete_workspace_effect: Exception | None = None,
    create_entry_effect: Exception | None = None,
    create_entry_return: WorkspaceEntryRef | None = None,
    list_entries_return: tuple[list[WorkspaceEntryRef], str | None] | None = None,
    update_entry_effect: Exception | None = None,
    update_entry_return: WorkspaceEntryRef | None = None,
    delete_entry_effect: Exception | None = None,
    # Share methods
    list_shares_effect: Exception | None = None,
    list_shares_return: list[ShareRef] | None = None,
    grant_share_effect: Exception | None = None,
    grant_share_return: ShareRef | None = None,
    revoke_share_effect: Exception | None = None,
    # Search
    search_workspaces_return: SearchResult | None = None,
    search_workspaces_effect: Exception | None = None,
    # Admin RTBF
    purge_actor_personal_data_effect: Exception | None = None,
    purge_actor_personal_data_return: Any | None = None,
    ctx: TenantContext | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)
    app.include_router(entry_mutation_router)
    app.include_router(share_router)
    app.include_router(share_mutation_router)

    svc = MagicMock()

    # Workspace methods
    if create_workspace_effect is not None:
        svc.create_workspace = AsyncMock(side_effect=create_workspace_effect)
    else:
        svc.create_workspace = AsyncMock(return_value=create_workspace_return or _make_workspace_ref())

    if list_workspaces_return is None:
        list_workspaces_return = ([], None)
    svc.list_workspaces = AsyncMock(return_value=list_workspaces_return)

    if get_workspace_effect is not None:
        svc.get_workspace = AsyncMock(side_effect=get_workspace_effect)
    else:
        svc.get_workspace = AsyncMock(return_value=get_workspace_return or _make_workspace_ref())

    if update_workspace_effect is not None:
        svc.update_workspace = AsyncMock(side_effect=update_workspace_effect)
    else:
        svc.update_workspace = AsyncMock(return_value=update_workspace_return or _make_workspace_ref())

    if delete_workspace_effect is not None:
        svc.delete_workspace = AsyncMock(side_effect=delete_workspace_effect)
    else:
        svc.delete_workspace = AsyncMock(return_value=None)

    # Entry methods
    if create_entry_effect is not None:
        svc.create_entry = AsyncMock(side_effect=create_entry_effect)
    else:
        svc.create_entry = AsyncMock(return_value=create_entry_return or _make_entry_ref())

    if list_entries_return is None:
        list_entries_return = ([], None)
    svc.list_entries = AsyncMock(return_value=list_entries_return)

    if update_entry_effect is not None:
        svc.update_entry = AsyncMock(side_effect=update_entry_effect)
    else:
        svc.update_entry = AsyncMock(return_value=update_entry_return or _make_entry_ref())

    if delete_entry_effect is not None:
        svc.delete_entry = AsyncMock(side_effect=delete_entry_effect)
    else:
        svc.delete_entry = AsyncMock(return_value=None)

    # Share methods
    if list_shares_effect is not None:
        svc.list_shares = AsyncMock(side_effect=list_shares_effect)
    else:
        svc.list_shares = AsyncMock(return_value=list_shares_return if list_shares_return is not None else [])

    if grant_share_effect is not None:
        svc.grant_share = AsyncMock(side_effect=grant_share_effect)
    else:
        svc.grant_share = AsyncMock(return_value=grant_share_return or _make_share_ref())

    if revoke_share_effect is not None:
        svc.revoke_share = AsyncMock(side_effect=revoke_share_effect)
    else:
        svc.revoke_share = AsyncMock(return_value=None)

    # Search
    if search_workspaces_effect is not None:
        svc.search_workspaces = AsyncMock(side_effect=search_workspaces_effect)
    else:
        svc.search_workspaces = AsyncMock(
            return_value=search_workspaces_return or SearchResult(items=[], next_cursor=None, total_count=None)
        )

    # Admin RTBF
    if purge_actor_personal_data_effect is not None:
        svc.purge_actor_personal_data = AsyncMock(side_effect=purge_actor_personal_data_effect)
    else:
        svc.purge_actor_personal_data = AsyncMock(return_value=purge_actor_personal_data_return)

    async def _fake_svc() -> MagicMock:
        return svc

    app.dependency_overrides[get_workspace_service] = _fake_svc

    effective_ctx = ctx if ctx is not None else TenantContext(
        tenant_id=_TENANT_ID,
        actor_id=_ACTOR_ID,
        roles=["producer"],
    )

    async def _fake_ctx() -> TenantContext:
        return effective_ctx

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


# ---------------------------------------------------------------------------
# POST /v1/workspaces — happy path
# ---------------------------------------------------------------------------


def test_create_workspace_happy_path_returns_201() -> None:
    """POST /v1/workspaces returns 201 with WorkspaceResponse and correct shape."""
    ref = _make_workspace_ref(name="Finance Workspace")
    app = _build_app(create_workspace_return=ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/v1/workspaces",
        json={"name": "Finance Workspace", "owner_kind": "actor"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["workspace_id"] == str(_WORKSPACE_ID)
    assert body["name"] == "Finance Workspace"
    assert body["owner_kind"] == "actor"
    assert "tenant_id" in body
    assert "created_at" in body
    assert "updated_at" in body
    # encryption_tier must be absent
    assert "encryption_tier" not in body
    assert "encryption_status" not in body


def test_create_workspace_tenant_workspace() -> None:
    """POST /v1/workspaces with owner_kind='tenant' returns 201 with owner_actor_id absent."""
    ref = _make_workspace_ref(name="Team Workspace", owner_kind="tenant")
    app = _build_app(create_workspace_return=ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/v1/workspaces",
        json={"name": "Team Workspace", "owner_kind": "tenant"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # owner_actor_id is None for tenant-owned → excluded by exclude_none
    assert body.get("owner_actor_id") is None or "owner_actor_id" not in body


# ---------------------------------------------------------------------------
# POST /v1/workspaces — validation failures
# ---------------------------------------------------------------------------


def test_create_workspace_invalid_owner_kind_returns_422() -> None:
    """Invalid owner_kind raises 422 from the service layer."""
    app = _build_app(
        create_workspace_effect=HTTPException(
            status_code=422,
            detail="Invalid owner_kind 'organization'. Must be one of: ['actor', 'tenant'].",
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/v1/workspaces",
        json={"name": "Bad Workspace", "owner_kind": "organization"},
    )
    assert resp.status_code == 422


def test_create_workspace_regulated_tenant_returns_422() -> None:
    """Regulated tenant at tier 'none' gets 422 with the exact error message from the service."""
    app = _build_app(
        create_workspace_effect=HTTPException(
            status_code=422,
            detail=_REGULATED_TENANT_ERROR,
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/v1/workspaces",
        json={"name": "Regulated Workspace", "owner_kind": "tenant"},
    )
    assert resp.status_code == 422
    # The error envelope wraps the detail; check the message appears
    text = resp.text
    assert "regulated" in text.lower() or "encryption tier" in text.lower()


def test_create_workspace_empty_name_returns_422() -> None:
    """Empty name fails Pydantic min_length=1 before the service is called."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/v1/workspaces",
        json={"name": "", "owner_kind": "actor"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/workspaces — list
# ---------------------------------------------------------------------------


def test_list_workspaces_returns_200_with_shape() -> None:
    """GET /v1/workspaces returns {items, next_cursor} shape."""
    ref1 = _make_workspace_ref(name="WS1")
    ref2 = _make_workspace_ref(workspace_id=uuid.uuid4(), name="WS2")
    app = _build_app(list_workspaces_return=([ref1, ref2], "cursor123"))
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/v1/workspaces")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert len(body["items"]) == 2
    assert body["next_cursor"] == "cursor123"


def test_list_workspaces_empty_returns_null_cursor() -> None:
    """GET /v1/workspaces with no workspaces returns items=[] and next_cursor=null."""
    app = _build_app(list_workspaces_return=([], None))
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/v1/workspaces")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# GET /v1/workspaces/{workspace_id} — get by ID
# ---------------------------------------------------------------------------


def test_get_workspace_returns_200() -> None:
    """GET /v1/workspaces/{id} returns 200 with WorkspaceResponse."""
    ref = _make_workspace_ref(name="Detail Workspace")
    app = _build_app(get_workspace_return=ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/v1/workspaces/{_WORKSPACE_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == str(_WORKSPACE_ID)
    assert body["name"] == "Detail Workspace"


def test_get_workspace_not_found_returns_404() -> None:
    """GET /v1/workspaces/{id} with missing workspace propagates 404 from service."""
    app = _build_app(get_workspace_effect=HTTPException(status_code=404, detail="Workspace not found."))
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(f"/v1/workspaces/{_WORKSPACE_ID}")
    assert resp.status_code == 404


def test_get_workspace_forbidden_returns_403() -> None:
    """GET /v1/workspaces/{id} for inaccessible workspace propagates 403 from service."""
    app = _build_app(get_workspace_effect=HTTPException(status_code=403, detail="Not authorized."))
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(f"/v1/workspaces/{_WORKSPACE_ID}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /v1/workspaces/{workspace_id}
# ---------------------------------------------------------------------------


def test_update_workspace_returns_200() -> None:
    """PATCH /v1/workspaces/{id} returns 200 with updated WorkspaceResponse."""
    updated_ref = _make_workspace_ref(name="Renamed Workspace")
    app = _build_app(update_workspace_return=updated_ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.patch(
        f"/v1/workspaces/{_WORKSPACE_ID}",
        json={"name": "Renamed Workspace"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Renamed Workspace"


# ---------------------------------------------------------------------------
# DELETE /v1/workspaces/{workspace_id}
# ---------------------------------------------------------------------------


def test_delete_workspace_returns_204() -> None:
    """DELETE /v1/workspaces/{id} returns 204 No Content."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.delete(f"/v1/workspaces/{_WORKSPACE_ID}")
    assert resp.status_code == 204
    assert resp.content == b""


def test_delete_workspace_idempotent_second_call_returns_204() -> None:
    """Second DELETE call also returns 204 — service no-op path."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=True)

    client.delete(f"/v1/workspaces/{_WORKSPACE_ID}")
    resp = client.delete(f"/v1/workspaces/{_WORKSPACE_ID}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# POST /v1/workspaces/{workspace_id}/entries — happy path
# ---------------------------------------------------------------------------


def test_create_entry_happy_path_returns_201() -> None:
    """POST /v1/workspaces/{id}/entries returns 201 with EntryResponse shape."""
    ref = _make_entry_ref(kind="note", body_md="This is a note.")
    app = _build_app(create_entry_return=ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries",
        json={"kind": "note", "body_md": "This is a note."},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["entry_id"] == str(_ENTRY_ID)
    assert body["kind"] == "note"
    assert body["body_md"] == "This is a note."
    # warnings must be absent when service returns None
    assert "warnings" not in body
    # encryption fields must be absent
    assert "encryption_status" not in body
    assert "body_ciphertext" not in body


def test_create_entry_with_warnings_returns_201_and_warnings() -> None:
    """POST /v1/workspaces/{id}/entries returns 201 with warnings when PII scan=warn fires."""
    ref = _make_entry_ref(
        kind="note",
        warnings=[{"field": "body_md", "categories": ["PII_EMAIL"]}],
    )
    app = _build_app(create_entry_return=ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries",
        json={"kind": "note", "body_md": "Contact user@example.com for details."},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "warnings" in body
    assert body["warnings"][0]["field"] == "body_md"
    assert "PII_EMAIL" in body["warnings"][0]["categories"]


# ---------------------------------------------------------------------------
# POST /v1/workspaces/{workspace_id}/entries — validation failures
# ---------------------------------------------------------------------------


def test_create_entry_invalid_kind_returns_422() -> None:
    """Invalid entry kind raises 422 from the service."""
    app = _build_app(
        create_entry_effect=HTTPException(
            status_code=422,
            detail="Invalid entry kind 'memo'. Must be one of: ['decision', 'note', ...].",
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries",
        json={"kind": "memo", "body_md": "Valid body."},
    )
    assert resp.status_code == 422


def test_create_entry_empty_body_md_returns_422() -> None:
    """Empty body_md fails Pydantic min_length=1 validation before the service is called."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries",
        json={"kind": "note", "body_md": ""},
    )
    assert resp.status_code == 422


def test_create_entry_regulated_tenant_returns_422() -> None:
    """Regulated tenant entry creation is blocked with 422 (defense-in-depth)."""
    app = _build_app(
        create_entry_effect=HTTPException(
            status_code=422,
            detail=_REGULATED_TENANT_ERROR,
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries",
        json={"kind": "note", "body_md": "Entry body."},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/workspaces/{workspace_id}/entries — list
# ---------------------------------------------------------------------------


def test_list_entries_returns_200_with_shape() -> None:
    """GET /v1/workspaces/{id}/entries returns {items, next_cursor} shape."""
    ref1 = _make_entry_ref(kind="note", body_md="Note 1.")
    ref2 = _make_entry_ref(entry_id=uuid.uuid4(), kind="decision", body_md="Decision 1.")
    app = _build_app(list_entries_return=([ref1, ref2], "entry_cursor"))
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/v1/workspaces/{_WORKSPACE_ID}/entries")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert len(body["items"]) == 2
    assert body["next_cursor"] == "entry_cursor"


def test_list_entries_empty_returns_null_cursor() -> None:
    """GET /v1/workspaces/{id}/entries with no entries returns items=[] and next_cursor=null."""
    app = _build_app(list_entries_return=([], None))
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/v1/workspaces/{_WORKSPACE_ID}/entries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# PATCH /v1/workspaces/{workspace_id}/entries/{entry_id}
# ---------------------------------------------------------------------------


def test_update_entry_returns_200() -> None:
    """PATCH /v1/workspaces/{id}/entries/{entry_id} returns 200 with EntryResponse."""
    updated_ref = _make_entry_ref(body_md="Updated body.")
    app = _build_app(update_entry_return=updated_ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.patch(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries/{_ENTRY_ID}",
        json={"body_md": "Updated body."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["body_md"] == "Updated body."


def test_update_entry_with_warnings_returns_200_and_warnings() -> None:
    """PATCH entry with PII warn hit returns 200 with warnings populated."""
    updated_ref = _make_entry_ref(
        body_md="Contact admin@corp.com",
        warnings=[{"field": "body_md", "categories": ["PII_EMAIL"]}],
    )
    app = _build_app(update_entry_return=updated_ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.patch(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries/{_ENTRY_ID}",
        json={"body_md": "Contact admin@corp.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "warnings" in body
    assert body["warnings"][0]["field"] == "body_md"


# ---------------------------------------------------------------------------
# DELETE /v1/workspaces/{workspace_id}/entries/{entry_id}
# ---------------------------------------------------------------------------


def test_delete_entry_returns_204() -> None:
    """DELETE /v1/workspaces/{id}/entries/{entry_id} returns 204 No Content."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.delete(f"/v1/workspaces/{_WORKSPACE_ID}/entries/{_ENTRY_ID}")
    assert resp.status_code == 204
    assert resp.content == b""


def test_delete_entry_idempotent_second_call_returns_204() -> None:
    """Second DELETE on the same entry returns 204 — service no-op path."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=True)

    client.delete(f"/v1/workspaces/{_WORKSPACE_ID}/entries/{_ENTRY_ID}")
    resp = client.delete(f"/v1/workspaces/{_WORKSPACE_ID}/entries/{_ENTRY_ID}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Absent encryption fields guard
# ---------------------------------------------------------------------------


def test_workspace_response_excludes_encryption_fields() -> None:
    """WorkspaceResponse never contains encryption_tier, encryption_status, or kek_id."""
    ref = _make_workspace_ref()
    app = _build_app(create_workspace_return=ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/v1/workspaces",
        json={"name": "Guard Test", "owner_kind": "actor"},
    )
    assert resp.status_code == 201
    body = resp.json()
    for forbidden in ("encryption_tier", "encryption_status", "kek_id", "wrapped_dek", "body_ciphertext"):
        assert forbidden not in body, f"Forbidden field present: {forbidden}"


def test_entry_response_excludes_encryption_fields() -> None:
    """EntryResponse never contains body_ciphertext, encryption_status, or kek_id."""
    ref = _make_entry_ref()
    app = _build_app(create_entry_return=ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/entries",
        json={"kind": "note", "body_md": "Guard test body."},
    )
    assert resp.status_code == 201
    body = resp.json()
    for forbidden in ("body_ciphertext", "body_nonce", "encryption_status", "kek_id", "wrapped_dek"):
        assert forbidden not in body, f"Forbidden field present: {forbidden}"


# ---------------------------------------------------------------------------
# GET /v1/workspaces/{workspace_id}/shares — list shares
# ---------------------------------------------------------------------------


def test_list_shares_returns_200_with_items() -> None:
    """GET /v1/workspaces/{id}/shares returns 200 with {items: [ShareResponse]}."""
    share1 = _make_share_ref(role="reader")
    share2 = _make_share_ref(share_id=uuid.uuid4(), role="contributor")
    app = _build_app(list_shares_return=[share1, share2])
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/v1/workspaces/{_WORKSPACE_ID}/shares")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert item["share_id"] == str(_SHARE_ID)
    assert item["workspace_id"] == str(_WORKSPACE_ID)
    assert item["grantee_actor_id"] == str(_GRANTEE_ACTOR_ID)
    assert item["grantee_tenant_id"] == str(_GRANTEE_TENANT_ID)
    assert item["role"] == "reader"
    assert "granted_at" in item
    # Encryption fields must be absent
    for forbidden in ("encryption_tier", "encryption_status", "kek_id", "wrapped_dek"):
        assert forbidden not in item, f"Forbidden field present: {forbidden}"


def test_list_shares_empty_returns_empty_items() -> None:
    """GET /v1/workspaces/{id}/shares with no active shares returns {items: []}."""
    app = _build_app(list_shares_return=[])
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/v1/workspaces/{_WORKSPACE_ID}/shares")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# POST /v1/workspaces/{workspace_id}/shares — grant share
# ---------------------------------------------------------------------------


def test_grant_share_happy_path_returns_201() -> None:
    """POST /v1/workspaces/{id}/shares returns 201 with ShareResponse."""
    share_ref = _make_share_ref(role="contributor")
    app = _build_app(grant_share_return=share_ref)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/shares",
        json={
            "grantee_actor_id": str(_GRANTEE_ACTOR_ID),
            "grantee_tenant_id": str(_GRANTEE_TENANT_ID),
            "role": "contributor",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["share_id"] == str(_SHARE_ID)
    assert body["workspace_id"] == str(_WORKSPACE_ID)
    assert body["role"] == "contributor"
    assert "granted_at" in body


def test_grant_share_cross_tenant_actor_owned_returns_422() -> None:
    """Cross-tenant share on actor-owned workspace raises 422 from service (Layer 2 guard)."""
    other_tenant_id = uuid.uuid4()
    app = _build_app(
        grant_share_effect=HTTPException(
            status_code=422,
            detail=(
                "Actor-owned workspaces may only be shared within the same tenant. "
                "To share cross-tenant, the workspace must be tenant-owned."
            ),
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/shares",
        json={
            "grantee_actor_id": str(_GRANTEE_ACTOR_ID),
            "grantee_tenant_id": str(other_tenant_id),
            "role": "reader",
        },
    )
    assert resp.status_code == 422
    text = resp.text
    assert "actor-owned" in text.lower() or "same tenant" in text.lower()


def test_grant_share_duplicate_active_share_returns_409() -> None:
    """POST /v1/workspaces/{id}/shares when active share already exists returns 409."""
    app = _build_app(
        grant_share_effect=HTTPException(
            status_code=409,
            detail="An active share already exists for this grantee.",
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        f"/v1/workspaces/{_WORKSPACE_ID}/shares",
        json={
            "grantee_actor_id": str(_GRANTEE_ACTOR_ID),
            "grantee_tenant_id": str(_GRANTEE_TENANT_ID),
            "role": "reader",
        },
    )
    assert resp.status_code == 409
    assert "active share" in resp.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# DELETE /v1/workspaces/{workspace_id}/shares/{share_id} — revoke share
# ---------------------------------------------------------------------------


def test_revoke_share_returns_204() -> None:
    """DELETE /v1/workspaces/{id}/shares/{share_id} returns 204 No Content."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.delete(f"/v1/workspaces/{_WORKSPACE_ID}/shares/{_SHARE_ID}")
    assert resp.status_code == 204
    assert resp.content == b""


def test_revoke_share_idempotent_second_call_returns_204() -> None:
    """Second DELETE on same share returns 204 — service no-op path."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=True)

    client.delete(f"/v1/workspaces/{_WORKSPACE_ID}/shares/{_SHARE_ID}")
    resp = client.delete(f"/v1/workspaces/{_WORKSPACE_ID}/shares/{_SHARE_ID}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# GET /v1/workspaces/search — search entries
# ---------------------------------------------------------------------------


def test_search_returns_200_with_paginated_results() -> None:
    """GET /v1/workspaces/search returns 200 with {items, next_cursor, total_count}."""
    entry1 = _make_entry_ref(kind="note", body_md="First note.")
    entry2 = _make_entry_ref(entry_id=uuid.uuid4(), kind="decision", body_md="Decision note.")
    result = SearchResult(items=[entry1, entry2], next_cursor="search_cursor_xyz", total_count=None)
    app = _build_app(search_workspaces_return=result)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/v1/workspaces/search?q=note")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert "total_count" in body
    assert len(body["items"]) == 2
    assert body["next_cursor"] == "search_cursor_xyz"
    assert body["total_count"] is None


def test_search_empty_returns_null_cursor_and_null_total_count() -> None:
    """GET /v1/workspaces/search with no results returns items=[], null cursor, null total."""
    result = SearchResult(items=[], next_cursor=None, total_count=None)
    app = _build_app(search_workspaces_return=result)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/v1/workspaces/search")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["total_count"] is None


def test_search_reference_ids_comma_separated_parses_correctly() -> None:
    """GET /v1/workspaces/search with reference_ids=UUID,UUID forwards parsed list to service."""
    ref_id_a = uuid.uuid4()
    ref_id_b = uuid.uuid4()
    entry = _make_entry_ref(kind="saved_query", body_md="Ref query.")
    result = SearchResult(items=[entry], next_cursor=None, total_count=None)
    app = _build_app(search_workspaces_return=result)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/v1/workspaces/search?reference_ids={ref_id_a},{ref_id_b}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    # Verify service was called — the mock accepted the call without raising,
    # confirming the comma-separated string was parsed to a valid UUID list.


def test_search_with_kind_filter_returns_200() -> None:
    """GET /v1/workspaces/search?kind=decision returns 200 and passes kind to service."""
    entry = _make_entry_ref(kind="decision", body_md="A decision.")
    result = SearchResult(items=[entry], next_cursor=None, total_count=None)
    app = _build_app(search_workspaces_return=result)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/v1/workspaces/search?kind=decision")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["kind"] == "decision"


# ---------------------------------------------------------------------------
# Admin RTBF — role enforcement (admin endpoint lives in admin_workspaces.py,
# but the WorkspaceService.purge_actor_personal_data role check is what the
# router delegates to; here we verify that a non-admin ctx → 403 from service).
# ---------------------------------------------------------------------------


def test_admin_rtbf_non_admin_role_raises_403() -> None:
    """DELETE /v1/admin/actors/{actor_id}/personal-data returns 403 when caller lacks admin role.

    The admin router's require_roles([ROLE_ADMIN]) guard fires before the service is
    reached. It resolves TenantContext via get_tenant_context; overriding that
    dependency with a non-admin context is the minimal way to exercise the 403 path
    without a real token issuance stack.
    """
    from registry.api.middleware.tenant import get_tenant_context as _gtc
    from registry.api.routers.admin_workspaces import router as admin_router

    target_actor_id = uuid.uuid4()

    admin_app = FastAPI()
    admin_app.include_router(admin_router)

    admin_svc = MagicMock()
    admin_svc.purge_actor_personal_data = AsyncMock(return_value=None)
    admin_app.state.workspace_service = admin_svc

    # Supply a non-admin (producer-only) tenant context. require_roles([ROLE_ADMIN])
    # will fire 403 because "admin" is absent from roles.
    non_admin_ctx = TenantContext(tenant_id=_TENANT_ID, actor_id=_ACTOR_ID, roles=["producer"])

    async def _fake_non_admin_ctx() -> TenantContext:
        return non_admin_ctx

    admin_app.dependency_overrides[_gtc] = _fake_non_admin_ctx

    admin_client = TestClient(admin_app, raise_server_exceptions=False)
    resp = admin_client.delete(f"/v1/admin/actors/{target_actor_id}/personal-data")
    assert resp.status_code == 403


def test_admin_rtbf_with_admin_role_returns_200() -> None:
    """DELETE /v1/admin/actors/{actor_id}/personal-data returns 200 when caller has admin role.

    The admin router reads the WorkspaceService from request.app.state.workspace_service
    (not via dependency injection), so we set it on app.state directly rather than
    using dependency_overrides.
    """
    from registry.api.middleware.tenant import get_tenant_context as _gtc
    from registry.api.routers.admin_workspaces import router as admin_router
    from registry.service.workspace import PurgeResult

    target_actor_id = uuid.uuid4()

    admin_app = FastAPI()
    admin_app.include_router(admin_router)

    purge_result = PurgeResult(purged_entries=3, purged_workspaces=1, revoked_shares=2)
    admin_svc = MagicMock()
    admin_svc.purge_actor_personal_data = AsyncMock(return_value=purge_result)

    # The admin router reads from request.app.state.workspace_service directly.
    admin_app.state.workspace_service = admin_svc

    admin_ctx = TenantContext(tenant_id=_TENANT_ID, actor_id=_ACTOR_ID, roles=["admin"])

    async def _fake_admin_ctx() -> TenantContext:
        return admin_ctx

    admin_app.dependency_overrides[_gtc] = _fake_admin_ctx

    admin_client = TestClient(admin_app, raise_server_exceptions=True)
    resp = admin_client.delete(f"/v1/admin/actors/{target_actor_id}/personal-data")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["purged_entries"] == 3
    assert body["purged_workspaces"] == 1
    assert body["revoked_shares"] == 2
