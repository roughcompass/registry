"""Unit tests for keyset cursor pagination on the artifact list endpoint.

Covers:
- cursor= returns the next page of results.
- ?page=N is rejected with 422 and code ``page_param_deprecated``.
- next_cursor=None when the result fits in one page.
- ArtifactListResponse no longer exposes page/page_size fields.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()


def _make_tenant_ctx() -> Any:
    from registry.types import TenantContext  # noqa: PLC0415

    return TenantContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=["consumer"],
    )


def _make_fact(*, fact_id: uuid.UUID | None = None, ts: datetime.datetime | None = None) -> Any:
    """Build a minimal Fact-like object usable by the router."""
    from registry.storage.models import Fact  # noqa: PLC0415

    f = MagicMock(spec=Fact)
    f.fact_id = fact_id or uuid.uuid4()
    f.tenant_id = _TENANT
    f.entity_id = _ENTITY_ID
    f.category = "overview"
    f.title = "Test artifact"
    f.body = "body text"
    f.body_format = "markdown"
    f.is_authoritative = True
    f.is_authoritative_superseded = False
    f.sync_run_id = None
    f.t_valid_from = ts or _NOW
    f.t_valid_to = None
    f.t_ingested_at = ts or _NOW
    f.t_invalidated_at = None
    f.created_by = None
    return f


def _build_app(*, facts: list[Any]) -> tuple[FastAPI, MagicMock]:
    """Build a FastAPI test app that returns ``facts`` from the DB session.

    The artifact list handler calls ``list(rows.scalars())`` against the
    SQLAlchemy async session.  We wire the mock so that the first
    ``execute()`` call returns the requested facts (list call), and subsequent
    calls (actor name lookup) return an empty result.
    """
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.artifacts import mutation_router, router  # noqa: PLC0415
    from registry.types import EntityRef  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    # Service mock — only resolve_entity_handle is needed for list.
    catalog_svc = MagicMock()
    catalog_svc.resolve_entity_handle = AsyncMock(
        return_value=EntityRef(
            entity_id=_ENTITY_ID,
            tenant_id=_TENANT,
            entity_type="capability",
            name="cap",
            external_id=None,
            is_active=True,
            created_at=_NOW,
        )
    )
    app.state.catalog = catalog_svc

    # Build a DB session that returns ``facts`` from the first execute() call
    # and an empty scalars() from the second (actor name bulk-load).
    def _make_scalars_result(rows: list[Any]) -> MagicMock:
        r = MagicMock()
        # list() calls __iter__ on the scalars result; make it iterable.
        r.__iter__ = MagicMock(return_value=iter(rows))
        # all() is also used in some paths; provide it for completeness.
        r.all = MagicMock(return_value=rows)
        return r

    def _make_execute_result(rows: list[Any]) -> MagicMock:
        r = MagicMock()
        r.scalars = MagicMock(return_value=_make_scalars_result(rows))
        return r

    execute_results = [
        _make_execute_result(facts),  # first call: fact rows
        _make_execute_result([]),  # second call: actor name lookup
    ]

    session_mock = MagicMock()
    session_mock.execute = AsyncMock(side_effect=execute_results)
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session_mock)
    app.state.session_factory = session_factory

    async def _fake_ctx() -> Any:
        return _make_tenant_ctx()

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app, catalog_svc


class TestArtifactListResponseShape:
    """ArtifactListResponse schema contract."""

    def test_model_has_items_and_next_cursor(self) -> None:
        from registry.api.schemas import ArtifactListResponse  # noqa: PLC0415

        resp = ArtifactListResponse(
            items=[],
            next_cursor=None,
        )
        assert resp.items == []
        assert resp.next_cursor is None

    def test_model_has_no_page_field(self) -> None:
        from registry.api.schemas import ArtifactListResponse  # noqa: PLC0415

        fields = set(ArtifactListResponse.model_fields.keys())
        assert "page" not in fields, "page field must be removed from ArtifactListResponse"

    def test_model_has_no_page_size_field(self) -> None:
        from registry.api.schemas import ArtifactListResponse  # noqa: PLC0415

        fields = set(ArtifactListResponse.model_fields.keys())
        assert "page_size" not in fields, "page_size field must be removed from ArtifactListResponse"


class TestArtifactListPageParamRejected:
    """?page=N must be rejected with 422 / page_param_deprecated.

    The test app does not install main.py's global HTTPException handler,
    so the error envelope arrives in the raw ``detail`` list that
    ``build_error`` puts there.  Check the code from that list directly.
    """

    def _error_codes(self, body: dict) -> list[str]:
        # Without the global handler: detail is the list of ErrorItem-shaped dicts.
        detail = body.get("detail", [])
        if isinstance(detail, list):
            return [e.get("code", "") for e in detail if isinstance(e, dict)]
        return []

    def _first_message(self, body: dict) -> str:
        detail = body.get("detail", [])
        if isinstance(detail, list) and detail:
            return str(detail[0].get("message", ""))
        return ""

    def test_page_param_returns_422(self) -> None:
        app, _ = _build_app(facts=[])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts?page=2")
        assert resp.status_code == 422
        body = resp.json()
        assert "page_param_deprecated" in self._error_codes(body)

    def test_page_param_error_message_mentions_cursor(self) -> None:
        app, _ = _build_app(facts=[])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts?page=1")
        assert resp.status_code == 422
        body = resp.json()
        msg = self._first_message(body).lower()
        assert "cursor" in msg


class TestArtifactListNextCursorNoneOnSinglePage:
    """next_cursor must be None when result fits in one page."""

    def test_next_cursor_is_none_when_fewer_than_page_size(self) -> None:
        facts = [_make_fact() for _ in range(3)]
        app, _ = _build_app(facts=facts)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts?page_size=20")
        assert resp.status_code == 200
        body = resp.json()
        assert body["next_cursor"] is None
        assert len(body["items"]) == 3

    def test_empty_result_has_null_next_cursor(self) -> None:
        app, _ = _build_app(facts=[])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["next_cursor"] is None
        assert body["items"] == []


class TestArtifactListCursorPagination:
    """cursor= parameter wires into the keyset query."""

    def test_next_cursor_present_when_more_pages(self) -> None:
        # Return page_size + 1 facts so the handler knows there is a next page.
        ts_base = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        facts = [
            _make_fact(fact_id=uuid.uuid4(), ts=ts_base - datetime.timedelta(seconds=i))
            for i in range(21)  # page_size=20 + 1 sentinel
        ]
        app, _ = _build_app(facts=facts)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts?page_size=20")
        assert resp.status_code == 200
        body = resp.json()
        # Has more pages → next_cursor is not None.
        assert body["next_cursor"] is not None
        # Only page_size items returned, not the sentinel.
        assert len(body["items"]) == 20

    def test_cursor_param_is_accepted(self) -> None:
        """A valid cursor token must be accepted without 422."""
        from registry.api.cursor import encode_cursor  # noqa: PLC0415

        ts = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        token = encode_cursor({"ts": ts.isoformat(), "id": str(uuid.uuid4())})
        # Return fewer than page_size rows so next_cursor=None
        facts = [_make_fact()]
        app, _ = _build_app(facts=facts)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts?cursor={token}")
        assert resp.status_code == 200

    def test_malformed_cursor_returns_422(self) -> None:
        app, _ = _build_app(facts=[])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts?cursor=notbase64!!!")
        assert resp.status_code == 422
        body = resp.json()
        # Without the global handler, code is in detail (list of ErrorItem dicts).
        detail = body.get("detail", [])
        codes = [e.get("code", "") for e in detail if isinstance(e, dict)]
        assert "invalid_cursor" in codes

    def test_response_envelope_has_no_page_or_page_size(self) -> None:
        app, _ = _build_app(facts=[_make_fact()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_ENTITY_ID}/artifacts")
        assert resp.status_code == 200
        body = resp.json()
        assert "page" not in body, "page must not appear in response body"
        assert "page_size" not in body, "page_size must not appear in response body"
        assert "next_cursor" in body
        assert "items" in body
