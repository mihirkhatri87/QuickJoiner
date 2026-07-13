"""Optional cross-encoder reranking stage (config: retrieval.reranker = "fastembed").

A cross-encoder reads query and candidate together, so it ranks far better than
bi-encoder cosine — at the cost of a forward pass per candidate. It therefore
runs only over the small fused candidate set, never the whole corpus.

Lazy: nothing imports or downloads a model unless the feature is switched on.
The model (~80MB ONNX) downloads once on first use, then loads from the
fastembed cache (FASTEMBED_CACHE_PATH pins it, same as the embedder).
"""

from __future__ import annotations

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class CrossEncoderReranker:
    def __init__(self, model: str | None = None):
        import os

        from fastembed.rerank.cross_encoder import TextCrossEncoder

        cache = os.environ.get("FASTEMBED_CACHE_PATH")
        kwargs = {"cache_dir": cache} if cache else {}
        self._encoder = TextCrossEncoder(model_name=model or DEFAULT_RERANK_MODEL, **kwargs)

    def rank(self, query: str, texts: list[str]) -> list[int]:
        """Return candidate indices ordered best-first."""
        if not texts:
            return []
        scores = list(self._encoder.rerank(query, texts))
        return sorted(range(len(texts)), key=lambda i: -float(scores[i]))


def create_reranker(retrieval) -> CrossEncoderReranker | None:
    """Build the configured reranker, or None. Failures degrade to RRF order
    rather than breaking search (e.g. no network for the first download)."""
    if getattr(retrieval, "reranker", "none") != "fastembed":
        return None
    try:
        return CrossEncoderReranker(retrieval.reranker_model)
    except Exception:
        return None
