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
}

DEFAULT_EMBED_MODELS = {
    "fastembed": "BAAI/bge-small-en-v1.5",
    "ollama": "nomic-embed-text",
}


class LLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic | ollama
    model: str | None = None  # None -> DEFAULT_MODELS[provider]
    base_url: str = "http://localhost:11434"  # used by ollama
    max_tokens: int = 8192
    thinking: bool = False  # extended thinking (Anthropic) / think mode (Ollama reasoning models)
    thinking_budget: int = 4096  # max thinking tokens (Anthropic)

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
    min_score: float = 0.55


class ChatConfig(BaseModel):
    # Context compression: when a session's estimated tokens exceed the threshold,
    # older turns are folded into a rolling summary (LLM if available, else a
    # deterministic digest) and only the recent turns stay verbatim.
    compress_after_est_tokens: int = 6000
    keep_recent_messages: int = 12
    tool_result_max_chars: int = 2000  # persisted tool outputs are truncated to this
    learn_from_conversations: bool = True  # distill durable facts into memory on compression


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
    sources: list[SourceConfig] = Field(default_factory=list)


def workspace_dir(name: str | None = None) -> Path:
    """Resolve the workspace directory: QJ_WORKSPACE env var wins, else ~/.quickjoiner/<name>."""
    env = os.environ.get("QJ_WORKSPACE")
    if env and name is None:
        return Path(env)
    return Path.home() / ".quickjoiner" / (name or "default")


def config_path(workspace: Path) -> Path:
    return workspace / "config.yaml"


def load_config(workspace: Path) -> Config:
    load_dotenv(workspace / ".env")
    load_dotenv()  # also pick up a cwd .env
    path = config_path(workspace)
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


def save_config(workspace: Path, config: Config) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    config_path(workspace).write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
