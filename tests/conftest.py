from __future__ import annotations

import hashlib
import math

import pytest

from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.embedder import Embedder
from quickjoiner.memory.store import KnowledgeStore


class FakeEmbedder(Embedder):
    """Deterministic hash-based embeddings so tests need no model download."""

    DIM = 32

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for token in text.lower().split():
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "ws"


@pytest.fixture
def catalog(workspace):
    c = Catalog(workspace)
    yield c
    c.close()


@pytest.fixture
def store(workspace):
    return KnowledgeStore(workspace, FakeEmbedder())
