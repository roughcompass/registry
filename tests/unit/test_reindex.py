"""Unit tests for scripts/reindex_embeddings.py.

All DB interactions are mocked — no Postgres required.

Tests cover:
- NOT EXISTS idempotency: zero rows on first page → total=0.
- Cursor advance semantics: cursor file updated to last fact_id processed.
- Per-model cursor isolation: different model_ids write to different paths.
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

_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registry.config import Settings  # noqa: E402
from scripts.reindex_embeddings import (  # noqa: E402
    _cursor_path,
    _load_cursor,
    _run_reindex,
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


def _stub_embedder(dim: int = 4) -> MagicMock:
    emb = MagicMock()
    emb.model_version = "new-model"
    emb.encode = MagicMock(side_effect=lambda texts: np.zeros((len(texts), dim), dtype=np.float32))
    return emb


def _row(fact_id: uuid.UUID, body: str = "hello world") -> dict[str, Any]:
    return {"fact_id": fact_id, "body": body}


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def test_cursor_path_per_model() -> None:
    """Each model_id gets a distinct cursor file path."""
    p1 = _cursor_path("model-a")
    p2 = _cursor_path("model-b")
    assert p1 != p2
    assert "model-a" in str(p1)
    assert "model-b" in str(p2)


def test_cursor_round_trip(tmp_path: Path) -> None:
    fid = str(uuid.uuid4())
    model_id = "test-model"
    with patch("scripts.reindex_embeddings._cursor_path", return_value=tmp_path / "cur"):
        _save_cursor(model_id, fid)
        assert _load_cursor(model_id) == fid


def test_cursor_defaults_to_null_uuid(tmp_path: Path) -> None:
    with patch(
        "scripts.reindex_embeddings._cursor_path",
        return_value=tmp_path / "missing",
    ):
        val = _load_cursor("no-model")
    assert val == "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# _run_reindex
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_db_returns_zero(tmp_path: Path) -> None:
    """Empty first page → total=0, no encode called."""
    settings = _settings()
    embedder = _stub_embedder()
    new_model_id = "new-model-v2"

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    cursor_file = tmp_path / f"reindex_cursor_{new_model_id}"
    with (
        patch(
            "scripts.reindex_embeddings._cursor_path",
            return_value=cursor_file,
        ),
        patch(
            "scripts.reindex_embeddings.create_async_engine",
            return_value=mock_engine,
        ),
        patch(
            "scripts.reindex_embeddings.async_sessionmaker",
            return_value=mock_factory,
        ),
    ):
        total = await _run_reindex(settings, embedder, new_model_id)

    assert total == 0
    embedder.encode.assert_not_called()


@pytest.mark.asyncio
async def test_cursor_advances_to_last_fact_id(tmp_path: Path) -> None:
    """After one page the cursor file holds the last fact_id."""
    settings = _settings(backfill_batch_size=2)
    embedder = _stub_embedder()
    new_model_id = "new-model-v3"

    fid1 = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
    fid2 = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
    page1 = [_row(fid1), _row(fid2)]

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

    cursor_file = tmp_path / f"reindex_cursor_{new_model_id}"
    with (
        patch(
            "scripts.reindex_embeddings._cursor_path",
            return_value=cursor_file,
        ),
        patch(
            "scripts.reindex_embeddings.create_async_engine",
            return_value=mock_engine,
        ),
        patch(
            "scripts.reindex_embeddings.async_sessionmaker",
            return_value=mock_factory,
        ),
    ):
        total = await _run_reindex(settings, embedder, new_model_id)

    assert total == 2
    assert cursor_file.read_text().strip() == str(fid2)


@pytest.mark.asyncio
async def test_second_run_is_noop(tmp_path: Path) -> None:
    """Idempotency: DB returns zero rows (all already indexed) → total=0."""
    settings = _settings()
    embedder = _stub_embedder()
    new_model_id = "new-model-v4"

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    cursor_file = tmp_path / f"reindex_cursor_{new_model_id}"
    with (
        patch(
            "scripts.reindex_embeddings._cursor_path",
            return_value=cursor_file,
        ),
        patch(
            "scripts.reindex_embeddings.create_async_engine",
            return_value=mock_engine,
        ),
        patch(
            "scripts.reindex_embeddings.async_sessionmaker",
            return_value=mock_factory,
        ),
    ):
        total = await _run_reindex(settings, embedder, new_model_id)

    assert total == 0


# ---------------------------------------------------------------------------
# Output string
# ---------------------------------------------------------------------------


def test_main_output_string(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """main() prints exactly the contract string."""
    new_model_id = "my-new-model"
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

    cursor_file = tmp_path / f"reindex_cursor_{new_model_id}"
    from scripts.reindex_embeddings import main

    with (
        patch(
            "scripts.reindex_embeddings._cursor_path",
            return_value=cursor_file,
        ),
        patch(
            "scripts.reindex_embeddings.create_async_engine",
            return_value=mock_engine,
        ),
        patch(
            "scripts.reindex_embeddings.async_sessionmaker",
            return_value=mock_factory,
        ),
    ):
        main(["--new-model-id", new_model_id, "--database-url", db_url, "--stub"])

    out = capsys.readouterr().out.strip()
    assert out == f"reindex complete: 0 facts reindexed to model {new_model_id}"
