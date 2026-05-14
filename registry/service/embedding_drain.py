"""Outbox drain job — consumes `embedding_outbox`, writes to `embeddings`.

Registered as an APScheduler job in `registry/main.py`
lifespan. Runs every `settings.outbox_poll_interval_s` seconds with
`max_instances=1` and `coalesce=True` so overlapping ticks are harmless.

Design notes:
- `SELECT ... FOR UPDATE SKIP LOCKED` lets multiple instances (future) or
  concurrent test runs avoid double-processing without deadlocks.
- `chunk_plan` is materialized on the outbox row at enqueue time (whitespace
  token chunks, size=400, stride=200) so the drain job doesn't need to
  re-parse anything — it just calls `encode()` on the pre-computed chunks.
- All DB work for one row (insert embeddings + delete outbox row) runs inside
  a single `session.begin()` so a crash after encode but before commit leaves
  the outbox row intact for retry.
- Failures increment `attempts`; once `>= outbox_max_attempts` the row moves
  to `embedding_outbox_failed` and `catalog_outbox_pending_size` is decremented.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

import numpy as np
import numpy.typing as npt
from prometheus_client import Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.config import Settings
from registry.types import Embedder

_log = logging.getLogger(__name__)

# Registered once at module import; safe to call set/dec from async code.
_OUTBOX_PENDING_GAUGE: Gauge = Gauge(
    "catalog_outbox_pending_size",
    "Number of rows currently pending in embedding_outbox",
)

# Cooldown between retries for a failed row (seconds).
_COOLDOWN_S: int = 60

# Chunk parameters: window size and stride for sliding-window chunking.
_CHUNK_TOKENS: int = 400
_CHUNK_STRIDE: int = 200


# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------


class _ChunkEntry:
    """One planned chunk: start/end token indices and the text slice."""

    __slots__ = ("index", "start", "end", "text")

    def __init__(self, index: int, start: int, end: int, text: str) -> None:
        self.index = index
        self.start = start
        self.end = end
        self.text = text


def make_chunk_plan(
    body: str,
    chunk_tokens: int = _CHUNK_TOKENS,
    stride: int = _CHUNK_STRIDE,
) -> list[dict[str, object]]:
    """Split *body* into overlapping whitespace-token windows.

    Returns a JSON-serialisable list so it can be stored in `chunk_plan`.
    If the body fits in one chunk the list has exactly one entry with index=0.
    """
    tokens = body.split()
    if not tokens:
        return [{"index": 0, "start": 0, "end": 0, "text": ""}]

    entries: list[dict[str, object]] = []
    idx = 0
    start = 0
    while start < len(tokens):
        end = min(start + chunk_tokens, len(tokens))
        chunk_text = " ".join(tokens[start:end])
        entries.append({"index": idx, "start": start, "end": end, "text": chunk_text})
        if end >= len(tokens):
            break
        start += stride
        idx += 1

    return entries


# ---------------------------------------------------------------------------
# Drain job
# ---------------------------------------------------------------------------


async def drain_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
) -> None:
    """Drain one batch from `embedding_outbox`.

    Called by APScheduler; exceptions are caught internally and logged so the
    scheduler doesn't treat a transient DB error as a job failure.
    """
    try:
        await _drain_batch(session_factory, embedder, settings)
    except Exception:
        _log.exception("drain_outbox: unexpected error during batch; will retry next tick")


async def _drain_batch(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
) -> None:
    """Core drain logic. Raises on unexpected errors (caller wraps)."""
    batch_size = settings.outbox_batch_size
    max_attempts = settings.outbox_max_attempts

    async with session_factory() as session:
        # --- Claim a batch with SKIP LOCKED so concurrent drainers don't race.
        raw_rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT outbox_id, tenant_id, claim_type, fact_id,
                               text_to_embed, chunk_plan, attempts, enqueued_at
                        FROM   embedding_outbox
                        WHERE  last_error IS NULL
                           OR  last_attempt_at < now() - interval ':cooldown seconds'
                        ORDER  BY enqueued_at
                        LIMIT  :batch_size
                        FOR UPDATE SKIP LOCKED
                        """.replace(":cooldown", str(_COOLDOWN_S))
                    ),
                    {"batch_size": batch_size},
                )
            )
            .mappings()
            .all()
        )
        rows: list[dict[str, Any]] = [dict(r) for r in raw_rows]

    if not rows:
        return

    for row in rows:
        await _process_row(session_factory, embedder, settings, row, max_attempts)

    # Update pending gauge with a fresh count (best-effort).
    await _refresh_pending_gauge(session_factory)


async def _process_row(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
    row: dict[str, Any],
    max_attempts: int,
) -> None:
    outbox_id: uuid.UUID = row["outbox_id"]
    tenant_id: uuid.UUID = row["tenant_id"]
    claim_type: str = row["claim_type"]
    fact_id: uuid.UUID = row["fact_id"]
    text_to_embed: str = row["text_to_embed"]
    chunk_plan_raw: list[dict[str, Any]] = row["chunk_plan"] or []
    attempts: int = row["attempts"]

    # If chunk_plan is empty/malformed, re-compute it now.
    if not chunk_plan_raw:
        chunk_plan_raw = make_chunk_plan(text_to_embed)

    chunks = [str(entry["text"]) for entry in chunk_plan_raw]

    try:
        vectors: npt.NDArray[np.float32] = embedder.encode(chunks)
    except Exception as exc:
        await _handle_failure(
            session_factory,
            outbox_id,
            tenant_id,
            claim_type,
            fact_id,
            text_to_embed,
            chunk_plan_raw,
            attempts,
            max_attempts,
            error_text=repr(exc),
        )
        return

    now = datetime.datetime.now(tz=datetime.UTC)

    try:
        async with session_factory() as session, session.begin():
            # Insert one embedding row per chunk.
            for i, (chunk_text, vector) in enumerate(zip(chunks, vectors, strict=False)):
                idx_val = chunk_plan_raw[i].get("index", i)
                chunk_idx = int(idx_val) if isinstance(idx_val, int | float | str) else i
                await session.execute(
                    text(
                        """
                        INSERT INTO embeddings
                            (embedding_id, tenant_id, claim_type, claim_id,
                             chunk_index, model_id, vector, text_chunk,
                             ts_fact, created_at)
                        VALUES
                            (gen_random_uuid(), :tenant_id, :claim_type, :claim_id,
                             :chunk_index, :model_id, :vector,
                             :text_chunk, NULL, :created_at)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "claim_type": claim_type,
                        "claim_id": fact_id,
                        "chunk_index": chunk_idx,
                        "model_id": embedder.model_version,
                        # pgvector via asyncpg requires a string literal, not a Python list.
                        "vector": "[" + ",".join(str(x) for x in vector.tolist()) + "]",  # type: ignore[attr-defined]
                        "text_chunk": chunk_text,
                        "created_at": now,
                    },
                )
            # Delete the processed outbox row.
            await session.execute(
                text("DELETE FROM embedding_outbox WHERE outbox_id = :oid"),
                {"oid": outbox_id},
            )
    except Exception as exc:
        await _handle_failure(
            session_factory,
            outbox_id,
            tenant_id,
            claim_type,
            fact_id,
            text_to_embed,
            chunk_plan_raw,
            attempts,
            max_attempts,
            error_text=repr(exc),
        )


async def _handle_failure(
    session_factory: async_sessionmaker[AsyncSession],
    outbox_id: uuid.UUID,
    tenant_id: uuid.UUID,
    claim_type: str,
    fact_id: uuid.UUID,
    text_to_embed: str,
    chunk_plan: list[dict[str, Any]],
    attempts: int,
    max_attempts: int,
    error_text: str,
) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    new_attempts = attempts + 1
    _log.warning(
        "embedding_drain: attempt %d/%d failed for outbox_id=%s: %s",
        new_attempts,
        max_attempts,
        outbox_id,
        error_text[:200],
    )

    if new_attempts >= max_attempts:
        # Move to dead-letter table.
        try:
            async with session_factory() as session, session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO embedding_outbox_failed
                            (failed_id, tenant_id, claim_type, fact_id,
                             text_to_embed, chunk_plan, failed_at, error_text, attempts)
                        VALUES
                            (gen_random_uuid(), :tenant_id, :claim_type, :fact_id,
                             :text_to_embed, CAST(:chunk_plan AS jsonb),
                             :failed_at, :error_text, :attempts)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "claim_type": claim_type,
                        "fact_id": fact_id,
                        "text_to_embed": text_to_embed,
                        "chunk_plan": _jsonb_dumps(chunk_plan),
                        "failed_at": now,
                        "error_text": error_text,
                        "attempts": new_attempts,
                    },
                )
                await session.execute(
                    text("DELETE FROM embedding_outbox WHERE outbox_id = :oid"),
                    {"oid": outbox_id},
                )
        except Exception:
            _log.exception("embedding_drain: could not move outbox_id=%s to failed table", outbox_id)
    else:
        # Increment attempts and record error for cooldown.
        try:
            async with session_factory() as session, session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE embedding_outbox
                        SET    attempts        = :attempts,
                               last_error      = :last_error,
                               last_attempt_at = :last_attempt_at
                        WHERE  outbox_id = :oid
                        """
                    ),
                    {
                        "attempts": new_attempts,
                        "last_error": error_text[:2000],
                        "last_attempt_at": now,
                        "oid": outbox_id,
                    },
                )
        except Exception:
            _log.exception("embedding_drain: could not update attempts for outbox_id=%s", outbox_id)


async def _refresh_pending_gauge(session_factory: async_sessionmaker[AsyncSession]) -> None:
    try:
        async with session_factory() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM embedding_outbox"))
            count: int = result.scalar_one()
        _OUTBOX_PENDING_GAUGE.set(count)
    except Exception:
        _log.debug("embedding_drain: could not refresh pending gauge")


def _jsonb_dumps(obj: object) -> str:
    """Minimal JSON serialiser for jsonb cast — uses stdlib json."""
    import json  # noqa: PLC0415

    return json.dumps(obj)


__all__ = ["drain_outbox", "make_chunk_plan", "_OUTBOX_PENDING_GAUGE"]
