"""Embedding backends. Local by default so org data never leaves the machine."""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from quickjoiner.config import EmbeddingConfig


class Embedder(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]:
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
        kwargs = {"cache_dir": cache} if cache else {}
        self._model = TextEmbedding(model_name=config.resolved_model(), **kwargs)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.embed(texts)]


class OllamaEmbedder(Embedder):
    def __init__(self, config: EmbeddingConfig):
        self._base_url = config.base_url.rstrip("/")
        self._model = config.resolved_model()

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = httpx.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": texts},
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]


def create_embedder(config: EmbeddingConfig) -> Embedder:
    if config.provider == "fastembed":
        return FastEmbedEmbedder(config)
    if config.provider == "ollama":
        return OllamaEmbedder(config)
    raise ValueError(f"Unknown embedding provider: {config.provider!r}")
