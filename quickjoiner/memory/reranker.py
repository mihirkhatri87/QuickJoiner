"""Optional cross-encoder reranking stage (config: retrieval.reranker = "fastembed").

A cross-encoder reads query and candidate together, so it ranks far better than
bi-encoder cosine — at the cost of a forward pass per candidate. It therefore
runs only over the small fused candidate set, never the whole corpus.

Lazy at two levels: nothing is built unless the feature is on, and even then the
~80MB ONNX model is not imported/downloaded until the first `rank()` call — so
starting the app (or building a store you never search) costs nothing. The model
downloads once, then loads from the fastembed cache (FASTEMBED_CACHE_PATH pins it,
same as the embedder). If it can't load (e.g. offline first run), reranking
degrades permanently to the input (RRF) order instead of retrying every search.

Env kill-switch: set QJ_DISABLE_RERANKER=1 to force it off regardless of config
(used by the test suite to stay offline; handy for constrained deployments).
"""

from __future__ import annotations

import os

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class CrossEncoderReranker:
    _FAILED = object()  # sentinel: model load failed, don't keep retrying

    def __init__(self, model: str | None = None):
        self._model_name = model or DEFAULT_RERANK_MODEL
        self._encoder = None  # built lazily on first rank()

    def _ensure(self) -> None:
        if self._encoder is not None:
            return
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            cache = os.environ.get("FASTEMBED_CACHE_PATH")
            kwargs = {"cache_dir": cache} if cache else {}
            self._encoder = TextCrossEncoder(model_name=self._model_name, **kwargs)
        except Exception:
            self._encoder = self._FAILED  # offline / missing dep -> degrade quietly

    def rank(self, query: str, texts: list[str]) -> list[int]:
        """Return candidate indices ordered best-first (input order if unavailable)."""
        if not texts:
            return []
        self._ensure()
        if self._encoder is self._FAILED:
            return list(range(len(texts)))
        scores = list(self._encoder.rerank(query, texts))
        return sorted(range(len(texts)), key=lambda i: -float(scores[i]))


def create_reranker(retrieval) -> CrossEncoderReranker | None:
    """Build the configured reranker, or None. Construction is cheap (the model
    loads on first use); the QJ_DISABLE_RERANKER env var forces it off."""
    if os.environ.get("QJ_DISABLE_RERANKER"):
        return None
    if getattr(retrieval, "reranker", "none") != "fastembed":
        return None
    return CrossEncoderReranker(getattr(retrieval, "reranker_model", None))
