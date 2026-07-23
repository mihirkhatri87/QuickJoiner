"""Embedding backends. Local by default so org data never leaves the machine."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from quickjoiner.config import EmbeddingConfig

logger = logging.getLogger(__name__)


def _resolve_providers(device: str) -> tuple[list[str] | None, str]:
    """Pick the onnxruntime execution providers for fastembed given the configured `device`.

    Returns `(providers | None, label)`; `None` means "let fastembed default" (CPU). `device`:
    - "auto"        → CUDA if onnxruntime exposes CUDAExecutionProvider (the `[gpu]` extra +
                      a working CUDA/cuDNN), else CPU. This is the resource-aware default: a
                      GPU is used automatically when present, and a plain CPU install is
                      byte-for-byte unaffected.
    - "cuda"/"gpu"  → require the GPU; fall back to CPU with a warning if it isn't available
                      (so a mis-set config never hard-fails a sync).
    - "cpu"         → force CPU (the prior behaviour).
    """
    dev = (device or "auto").strip().lower()
    if dev == "cpu":
        return None, "cpu"
    try:
        import onnxruntime as ort

        available = ort.get_available_providers()
    except Exception:  # onnxruntime import problem — let fastembed sort it out on CPU
        available = []
    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"
    if dev in ("cuda", "gpu"):
        logger.warning(
            "embedding.device=%r but CUDAExecutionProvider is unavailable — falling back to CPU. "
            "Install the GPU runtime (pip install fastembed-gpu / onnxruntime-gpu) and a matching "
            "CUDA/cuDNN.", device,
        )
    return None, "cpu"

# Asymmetric-retrieval instruction prefixes, keyed by a substring of the resolved model
# name -> (query_prefix, passage_prefix). These models were trained to embed queries and
# passages with different task instructions; applying them lifts retrieval recall. Models
# not listed here (or when EmbeddingConfig.instruct is False) use no prefix — symmetric,
# exactly the prior behavior. First substring match wins, so order most-specific first.
_INSTRUCTIONS: list[tuple[str, tuple[str, str]]] = [
    ("bge", ("Represent this sentence for searching relevant passages: ", "")),
    ("nomic-embed", ("search_query: ", "search_document: ")),
    ("e5-", ("query: ", "passage: ")),
]


def _instructions_for(model: str) -> tuple[str, str]:
    """(query_prefix, passage_prefix) for a model name, ("", "") if none known."""
    m = model.lower()
    for key, prefixes in _INSTRUCTIONS:
        if key in m:
            return prefixes
    return ("", "")


def _resolve_prefixes(config: EmbeddingConfig) -> tuple[str, str]:
    """Prefixes to apply given config; disabled -> no prefixes (symmetric embedding)."""
    if not config.instruct:
        return ("", "")
    return _instructions_for(config.resolved_model())


class Embedder(ABC):
    # Query/passage instruction prefixes; "" means none (symmetric). Concrete embedders
    # that support instructions set these from config and prepend them in embed/embed_query.
    _q_prefix: str = ""
    _p_prefix: str = ""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents/passages (what gets stored)."""

    def embed_query(self, text: str) -> list[float]:
        # Default (no instruction) path used by simple embedders; instruction-aware
        # embedders override this to apply the query prefix without the passage prefix.
        return self.embed([text])[0]

    @property
    def dim(self) -> int:
        if not hasattr(self, "_dim"):
            self._dim = len(self.embed_query("probe"))
        return self._dim


class FastEmbedEmbedder(Embedder):
    """ONNX CPU embeddings via fastembed. Model downloads once, then works offline.

    FASTEMBED_CACHE_PATH pins the model cache to a writable, persistent location
    (used by the Docker image so the model survives container restarts).
    """

    def __init__(self, config: EmbeddingConfig):
        import os

        from fastembed import TextEmbedding

        cache = os.environ.get("FASTEMBED_CACHE_PATH")
        kwargs: dict = {"cache_dir": cache} if cache else {}
        providers, device = _resolve_providers(config.device)
        if providers is not None:
            kwargs["providers"] = providers
        self._model = TextEmbedding(model_name=config.resolved_model(), **kwargs)
        logger.info("fastembed embedding model %s on %s", config.resolved_model(), device)
        self._device = device
        self._q_prefix, self._p_prefix = _resolve_prefixes(config)

    def _raw(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.embed(texts)]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._p_prefix:
            texts = [self._p_prefix + t for t in texts]
        return self._raw(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._raw([self._q_prefix + text])[0]


class OllamaEmbedder(Embedder):
    def __init__(self, config: EmbeddingConfig):
        self._base_url = config.base_url.rstrip("/")
        self._model = config.resolved_model()
        self._q_prefix, self._p_prefix = _resolve_prefixes(config)

    def _raw(self, texts: list[str]) -> list[list[float]]:
        resp = httpx.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": texts},
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._p_prefix:
            texts = [self._p_prefix + t for t in texts]
        return self._raw(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._raw([self._q_prefix + text])[0]


def create_embedder(config: EmbeddingConfig) -> Embedder:
    if config.provider == "fastembed":
        return FastEmbedEmbedder(config)
    if config.provider == "ollama":
        return OllamaEmbedder(config)
    raise ValueError(f"Unknown embedding provider: {config.provider!r}")
