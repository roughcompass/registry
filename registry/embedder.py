"""Embedder implementations.

Two flavours:
* `SentenceTransformerEmbedder` — production, loads `all-MiniLM-L6-v2` once at
  startup. Loading failures raise at construction time so the app fails early
  rather than serving requests with degraded retrieval.
* `StubEmbedder` — returns zero vectors of the right shape. Integration tests
  that don't exercise retrieval recall use this to skip the model download.

Both implement the `Embedder` Protocol from `registry/types.py`.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt


class SentenceTransformerEmbedder:
    """Production Embedder: in-process `sentence-transformers/all-MiniLM-L6-v2`.

    Constructed once at app startup and injected via DI. The model is loaded
    eagerly in `__init__`; a load failure raises here, not on first request.
    """

    model_version: str = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        # Local import so test paths that wire `StubEmbedder` don't pay the
        # transformers / torch import cost.
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self._model = SentenceTransformer(self.model_version)

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]:
        vectors: npt.NDArray[np.float32] = self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=False,
        ).astype(np.float32)
        return vectors


class StubEmbedder:
    """Test Embedder: zero vectors of the right shape (384). No model load."""

    model_version: str = "stub-zero"
    _DIM: int = 384

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]:
        return np.zeros((len(texts), self._DIM), dtype=np.float32)


__all__ = ["SentenceTransformerEmbedder", "StubEmbedder"]
