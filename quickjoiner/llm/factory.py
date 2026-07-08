from __future__ import annotations

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import LLMProvider


def create_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "anthropic":
        from quickjoiner.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(config)
    if config.provider == "ollama":
        from quickjoiner.llm.ollama_provider import OllamaProvider

        return OllamaProvider(config)
    raise ValueError(f"Unknown LLM provider: {config.provider!r} (expected 'anthropic' or 'ollama')")
