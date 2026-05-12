"""Reindex all facts under a new embedding model ID.

Usage::

    python scripts/reindex_embeddings.py \\
        --new-model-id <model_id> \\
        --database-url postgresql+asyncpg://...

Inserts new embeddings rows for every fact using the new model, without
removing old rows (old model_id remains queryable until the operator restarts
the API with EMBEDDING_MODEL=<new_model_id>).

Idempotent: the NOT EXISTS predicate skips facts already reindexed under
new_model_id.  Cursor state is persisted to /tmp/reindex_cursor_<new_model_id>.

Exit line: ``reindex complete: N facts reindexed to model <new_model_id>``
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import sys
import uuid
from pathlib import Path

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registry.config import Settings, get_settings  # noqa: E402
from registry.embedder import SentenceTransformerEmbedder, StubEmbedder  # noqa: E402
from registry.service.embedding_drain import make_chunk_plan  # noqa: E402

_log = logging.getLogger(__name__)

_NULL_CURSOR = "00000000-0000-0000-0000-000000000000"


def _cursor_path(new_model_id: str) -> Path:
    # Sanitize model_id so it is safe as a filename component.
    safe = new_model_id.replace("/", "_").replace("\\", "_")
    return Path(f"/tmp/reindex_cursor_{safe}")


def _load_cursor(new_model_id: str) -> str:
    path = _cursor_path(new_model_id)
    if path.exists():
        value = path.read_text().strip()
        if value:
            return value
    return _NULL_CURSOR


def _save_cursor(new_model_id: str, cursor: str) -> None:
    _cursor_path(new_model_id).write_text(cursor)


async def _run_reindex(settings: Settings, embedder: object, new_model_id: str) -> int:
    """Core reindex loop. Returns total facts reindexed."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    batch_size = settings.backfill_batch_size
    cursor = _load_cursor(new_model_id)
    total = 0

    try:
        while True:
            # --- Fetch one page of facts not yet reindexed under new_model_id --
            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT fact_id, body
                                FROM   facts
                                WHERE  fact_id > :cursor
                                  AND  NOT EXISTS (
                                      SELECT 1 FROM embeddings
                                      WHERE  claim_id = facts.fact_id
                                        AND  model_id = :new_model_id
                                  )
                                ORDER  BY fact_id
                                LIMIT  :batch_size
                                """
                            ),
                            {
                                "cursor": cursor,
                                "new_model_id": new_model_id,
                                "batch_size": batch_size,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )

            if not rows:
                break

            # --- Embed and insert ---------------------------------------------
            now = datetime.datetime.now(tz=datetime.UTC)
            for row in rows:
                fact_id: uuid.UUID = row["fact_id"]
                body: str = row["body"] or ""

                chunk_plan = make_chunk_plan(body)
                chunks = [str(entry["text"]) for entry in chunk_plan]
                vectors = embedder.encode(chunks)  # type: ignore[attr-defined]

                async with session_factory() as session, session.begin():
                    for i, (chunk_text, vector) in enumerate(zip(chunks, vectors, strict=False)):
                        idx_val = chunk_plan[i].get("index", i)
                        chunk_idx = int(idx_val) if isinstance(idx_val, int | float | str) else i
                        await session.execute(
                            text(
                                """
                                INSERT INTO embeddings
                                    (embedding_id, tenant_id, claim_type, claim_id,
                                     chunk_index, model_id, vector, text_chunk,
                                     ts_fact, created_at)
                                SELECT gen_random_uuid(),
                                       f.tenant_id,
                                       'fact',
                                       f.fact_id,
                                       :chunk_index,
                                       :new_model_id,
                                       :vector,
                                       :text_chunk,
                                       NULL,
                                       :created_at
                                FROM   facts f
                                WHERE  f.fact_id = :fact_id
                                  AND  NOT EXISTS (
                                      SELECT 1 FROM embeddings
                                      WHERE  claim_id   = :fact_id
                                        AND  model_id   = :new_model_id
                                        AND  chunk_index = :chunk_index
                                  )
                                """
                            ),
                            {
                                "fact_id": str(fact_id),
                                "chunk_index": chunk_idx,
                                "new_model_id": new_model_id,
                                "vector": np.asarray(vector).tolist(),
                                "text_chunk": chunk_text,
                                "created_at": now,
                            },
                        )

                total += 1
                cursor = str(fact_id)

            _save_cursor(new_model_id, cursor)
            _log.info(
                "reindex: processed up to cursor=%s total=%d model=%s",
                cursor,
                total,
                new_model_id,
            )

    finally:
        await engine.dispose()

    return total


def _build_embedder(model_name: str, stub: bool) -> SentenceTransformerEmbedder | StubEmbedder:
    if stub:
        e = StubEmbedder()
        e.model_version = model_name
        return e
    return SentenceTransformerEmbedder()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reindex all facts under a new embedding model ID.")
    parser.add_argument(
        "--new-model-id",
        required=True,
        help="The new model_id to use for newly inserted embedding rows",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="asyncpg database URL (overrides DATABASE_URL env var)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Page size (overrides BACKFILL_BATCH_SIZE / settings default of 64)",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Use StubEmbedder (zero vectors) — for testing without model download",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)

    # Settings is the single env-var reader. If --database-url is
    # passed it overrides; otherwise get_settings() reads DATABASE_URL and
    # raises a clear error when unset.
    if args.database_url:
        settings = Settings(
            database_url=args.database_url,
            pgbouncer_url=args.database_url,
            scheduler_jobstore_url=args.database_url,
        )
    else:
        settings = get_settings()
    if args.batch_size is not None:
        object.__setattr__(settings, "backfill_batch_size", args.batch_size)

    new_model_id: str = args.new_model_id
    embedder = _build_embedder(new_model_id, stub=args.stub)
    total = asyncio.run(_run_reindex(settings, embedder, new_model_id))
    print(f"reindex complete: {total} facts reindexed to model {new_model_id}")


if __name__ == "__main__":
    main()
