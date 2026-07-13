"""Asymmetric-retrieval instruction prefixes for query vs passage embedding."""

from quickjoiner.config import EmbeddingConfig
from quickjoiner.memory import embedder as emb_mod
from quickjoiner.memory.embedder import OllamaEmbedder, _instructions_for, _resolve_prefixes


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return {"embeddings": [[0.1, 0.2, 0.3] for _ in self._payload["input"]]}


def test_instructions_for_known_and_unknown():
    q, p = _instructions_for("BAAI/bge-small-en-v1.5")
    assert q.startswith("Represent this sentence") and p == ""  # bge: passages unprefixed
    assert _instructions_for("nomic-embed-text") == ("search_query: ", "search_document: ")
    assert _instructions_for("intfloat/multilingual-e5-large") == ("query: ", "passage: ")
    assert _instructions_for("some/unknown-model") == ("", "")


def test_resolve_prefixes_is_gated_by_instruct():
    off = EmbeddingConfig(provider="fastembed")  # instruct defaults False
    assert _resolve_prefixes(off) == ("", "")

    on = EmbeddingConfig(provider="fastembed", instruct=True)  # default bge model
    assert on.resolved_model() == "BAAI/bge-small-en-v1.5"
    assert _resolve_prefixes(on)[0].startswith("Represent this sentence")


def test_ollama_embedder_applies_asymmetric_prefixes(monkeypatch):
    calls: list[list[str]] = []

    def fake_post(url, json, timeout):
        calls.append(json["input"])
        return _FakeResp(json)

    monkeypatch.setattr(emb_mod.httpx, "post", fake_post)

    e = OllamaEmbedder(EmbeddingConfig(provider="ollama", model="nomic-embed-text", instruct=True))
    e.embed(["the checkout service"])   # passage side -> search_document:
    e.embed_query("who owns checkout")  # query side   -> search_query:
    assert calls[0] == ["search_document: the checkout service"]
    assert calls[1] == ["search_query: who owns checkout"]


def test_ollama_embedder_symmetric_when_instruct_off(monkeypatch):
    calls: list[list[str]] = []

    def fake_post(url, json, timeout):
        calls.append(json["input"])
        return _FakeResp(json)

    monkeypatch.setattr(emb_mod.httpx, "post", fake_post)

    e = OllamaEmbedder(EmbeddingConfig(provider="ollama", model="nomic-embed-text"))  # off
    e.embed(["the checkout service"])
    e.embed_query("who owns checkout")
    assert calls[0] == ["the checkout service"]  # unchanged, prior behavior preserved
    assert calls[1] == ["who owns checkout"]
