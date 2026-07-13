"""Workspace resolution and configuration loading/saving."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "ollama": "llama3.1",
    # LiteLLM routes by whatever model names its proxy is configured with, so there
    # is no universal default — this is only a sane fallback the user should override.
    "litellm": "gpt-4o-mini",
}

DEFAULT_EMBED_MODELS = {
    "fastembed": "BAAI/bge-small-en-v1.5",
    "ollama": "nomic-embed-text",
}


class LLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic | ollama | litellm
    model: str | None = None  # None -> DEFAULT_MODELS[provider]
    # base_url is used by ollama (native /api/chat) and litellm (OpenAI-compatible
    # /chat/completions). For a LiteLLM proxy set this to e.g. http://localhost:4000.
    base_url: str = "http://localhost:11434"
    max_tokens: int = 8192
    thinking: bool = False  # extended thinking (Anthropic) / think mode (Ollama reasoning models)
    thinking_budget: int = 4096  # max thinking tokens (Anthropic)
    # litellm only: name of the env var holding the proxy's bearer key (never store the
    # secret itself). Empty/unset var -> no Authorization header (keyless local proxies).
    api_key_env: str = "LITELLM_API_KEY"

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "claude-opus-4-8")


class EmbeddingConfig(BaseModel):
    provider: str = "fastembed"  # fastembed | ollama
    model: str | None = None
    base_url: str = "http://localhost:11434"

    def resolved_model(self) -> str:
        return self.model or DEFAULT_EMBED_MODELS.get(self.provider, "BAAI/bge-small-en-v1.5")


class RetrievalConfig(BaseModel):
    top_k: int = 8
    # Below this cosine-similarity score hits are dropped; if nothing clears it the agent
    # must say "not learned yet". Tuned for BAAI/bge-small-en-v1.5 (relevant ~0.64+,
    # unrelated ~0.55 and below) — retune if you switch embedding models.
    # NOTE: this gate stays cosine-based even in hybrid mode — hybrid changes which
    # candidates surface and their order, never the grounded-vs-refuse decision.
    min_score: float = 0.55
    # Hybrid retrieval: dense (vector) + sparse (BM25 full-text) legs fused with
    # reciprocal rank fusion. The sparse leg rescues exact-token matches (error
    # codes, ticket IDs, service names) that sit outside the dense top-k.
    hybrid: bool = True
    rrf_k: int = 60  # RRF constant: score = sum(1 / (rrf_k + rank)); 60 is the literature default
    candidate_multiplier: int = 4  # each leg fetches top_k * this before fusion
    # LanceDB builds an approximate (IVF) vector index once the chunk count crosses
    # this threshold; below it brute-force search is exact and fast enough.
    ann_min_rows: int = 4000
    # Optional second-stage ranking with a cross-encoder over the fused candidates.
    # "none" (default) keeps RRF order; "fastembed" downloads a small ONNX
    # cross-encoder on first use (needs network once, then cached).
    reranker: str = "none"  # none | fastembed
    reranker_model: str | None = None  # None -> Xenova/ms-marco-MiniLM-L-6-v2
    rerank_candidates: int = 24  # how many fused candidates the reranker scores


class ChatConfig(BaseModel):
    # Context compression: when a session's estimated tokens exceed the threshold,
    # older turns are folded into a rolling summary (LLM if available, else a
    # deterministic digest) and only the recent turns stay verbatim.
    compress_after_est_tokens: int = 6000
    keep_recent_messages: int = 12
    tool_result_max_chars: int = 2000  # persisted tool outputs are truncated to this
    learn_from_conversations: bool = True  # distill durable facts into memory on compression


class GapsConfig(BaseModel):
    # Knowledge-debt backlog: every refusal (search_memory NO_RESULTS) is logged and
    # clustered into remediable gaps. Turn off to stop logging entirely.
    enabled: bool = True
    # Privacy: when False, the raw query text is NOT stored (hash-only mode) — clustering
    # then falls back to exact normalized-query equality. Use in shared/cloud workspaces.
    store_queries: bool = True
    # Cosine similarity above which two refusal queries share a cluster.
    cluster_threshold: float = 0.8


class SourceConfig(BaseModel):
    name: str
    type: str
    options: dict = Field(default_factory=dict)
    sync_interval_minutes: int | None = None
    # Ownership (used when workspace auth is enabled). owner=None -> commons
    # (pre-auth sources); shared=True -> visible to everyone even when owned.
    owner: str | None = None
    shared: bool = True


class Config(BaseModel):
    org: str = "default"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    gaps: GapsConfig = Field(default_factory=GapsConfig)
    sources: list[SourceConfig] = Field(default_factory=list)


def workspace_dir(name: str | None = None) -> Path:
    """Resolve the workspace directory: QJ_WORKSPACE env var wins, else ~/.quickjoiner/<name>."""
    env = os.environ.get("QJ_WORKSPACE")
    if env and name is None:
        return Path(env)
    return Path.home() / ".quickjoiner" / (name or "default")


def config_path(workspace: Path) -> Path:
    return workspace / "config.yaml"


def load_env(workspace: Path) -> None:
    """Load environment variables from <workspace>/.env and a cwd .env.

    Workspace configuration itself now lives in SQLite (see Catalog.load_config /
    save_config); this only hydrates env vars used for connector secret indirection
    (token=env:VAR) and provider keys (ANTHROPIC_API_KEY).
    """
    load_dotenv(workspace / ".env")
    load_dotenv()
