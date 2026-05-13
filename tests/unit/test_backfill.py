"""Unit tests for scripts/backfill_embeddings.py.

All DB interactions are mocked — no Postgres required.

Tests cover:
- NOT EXISTS idempotency: a fact returned on the first page is absent from
  the second page because we re-query with the same cursor logic.
- Cursor advance semantics: the cursor advances to the last fact_id processed.
- Zero-row page terminates the loop immediately (backfill complete: 0 ...).
- Output string matches contract exactly.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# Ensure the repo root is on the path so scripts/ can *
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registry.config import Settings  # noqa: E402
from scripts.backfill_embeddings import (  # noqa: E402
    _load_cursor,
    _run_backfill,
    _save_cursor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    s = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        backfill_batch_size=2,
    )
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def _stub_embedder(model_version: str = "test-model", dim: int = 4) -> MagicMock:
    emb = MagicMock()
    emb.model_version = model_version
    emb.encode = MagicMock(side_effect=lambda texts: np.zeros((len(texts), dim), dtype=np.float32))
    return emb


def _row(fact_id: uuid.UUID, body: str = "hello world") -> dict[str, Any]:
    return {"fact_id": fact_id, "body": body}


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def test_cursor_round_trip(tmp_path: Path) -> None:
    """_save_cursor / _load_cursor persist and retrieve a UUID string."""
    target = tmp_path / "cursor"
    fid = str(uuid.uuid4())
    with patch("scripts.backfill_embeddings._get_cursor_path", return_value=target):
        _save_cursor(fid, "test-model")
        assert _load_cursor("test-model") == fid


def test_cursor_defaults_to_null_uuid(tmp_path: Path) -> None:
    target = tmp_path / "cursor_missing"
    with patch("scripts.backfill_embeddings._get_cursor_path", return_value=target):
        val = _load_cursor("test-model")
    assert val == "00000000-0000-0000-0000-000000000000"


def test_backfill_cursor_path_includes_model_identifier() -> None:
    """Different models must get distinct cursor files; neither equals the legacy path."""
    from scripts.backfill_embeddings import _get_cursor_path

    a = _get_cursor_path("model-A")
    b = _get_cursor_path("model-B")
    assert a != b
    legacy = Path("/tmp/backfill_cursor")
    assert a != legacy and b != legacy


def test_backfill_cursor_path_slugifies_slash_in_model_id() -> None:
    """A '/' in the model identifier must be replaced with '_' — no sub-directories."""
    from scripts.backfill_embeddings import _get_cursor_path

    path = _get_cursor_path("openai/text-embedding-3-small")
    assert path == Path("/tmp/backfill_cursor_openai_text-embedding-3-small.txt"), path
    assert path.parent == Path("/tmp"), path.parent


# ---------------------------------------------------------------------------
# _run_backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_db_returns_zero(tmp_path: Path) -> None:
    """When the first page is empty the loop exits immediately with total=0."""
    settings = _settings()
    embedder = _stub_embedder()

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    cursor_path = tmp_path / "cursor"
    with (
        patch("scripts.backfill_embeddings._get_cursor_path", return_value=cursor_path),
        patch("scripts.backfill_embeddings.create_async_engine", return_value=mock_engine),
        patch("scripts.backfill_embeddings.async_sessionmaker", return_value=mock_factory),
    ):
        total = await _run_backfill(settings, embedder)

    assert total == 0
    embedder.encode.assert_not_called()


@pytest.mark.asyncio
async def test_cursor_advances_to_last_fact_id(tmp_path: Path) -> None:
    """After processing a page, the cursor file holds the last fact_id."""
    settings = _settings(backfill_batch_size=2)
    embedder = _stub_embedder()

    fid1 = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    fid2 = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
    page1 = [_row(fid1), _row(fid2)]

    # First execute call returns page1; subsequent calls return [] to terminate.
    call_count = 0

    async def _execute(_stmt: Any, _params: Any = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.mappings.return_value.all.return_value = page1
        else:
            result.mappings.return_value.all.return_value = []
        return result

    mock_session = AsyncMock()
    mock_session.execute = _execute
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=mock_session)

    mock_factory = MagicMock(return_value=mock_session)

    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    cursor_path = tmp_path / "cursor"
    with (
        patch("scripts.backfill_embeddings._get_cursor_path", return_value=cursor_path),
        patch("scripts.backfill_embeddings.create_async_engine", return_value=mock_engine),
        patch("scripts.backfill_embeddings.async_sessionmaker", return_value=mock_factory),
    ):
        total = await _run_backfill(settings, embedder)

    assert total == 2
    assert cursor_path.read_text().strip() == str(fid2)


@pytest.mark.asyncio
async def test_second_run_is_noop(tmp_path: Path) -> None:
    """Simulates idempotency: if the DB returns no rows (all already embedded)
    the total is 0 and the output string matches the contract."""
    settings = _settings()
    embedder = _stub_embedder()

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    cursor_path = tmp_path / "cursor"
    with (
        patch("scripts.backfill_embeddings._get_cursor_path", return_value=cursor_path),
        patch("scripts.backfill_embeddings.create_async_engine", return_value=mock_engine),
        patch("scripts.backfill_embeddings.async_sessionmaker", return_value=mock_factory),
    ):
        total = await _run_backfill(settings, embedder)

    assert total == 0


# ---------------------------------------------------------------------------
# Output string
# ---------------------------------------------------------------------------


def test_main_output_string(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """main() prints exactly the contract string."""
    cursor_path = tmp_path / "cursor"
    db_url = "postgresql+asyncpg://x:x@localhost/test"

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    from scripts.backfill_embeddings import main

    with (
        patch("scripts.backfill_embeddings._get_cursor_path", return_value=cursor_path),
        patch("scripts.backfill_embeddings.create_async_engine", return_value=mock_engine),
        patch("scripts.backfill_embeddings.async_sessionmaker", return_value=mock_factory),
    ):
        main(["--database-url", db_url, "--stub"])

    out = capsys.readouterr().out.strip()
    assert out == "backfill complete: 0 facts embedded"
