import pytest

from quickjoiner.config import LLMConfig
from quickjoiner.llm.factory import create_provider
from quickjoiner.llm.litellm_provider import LiteLLMProvider
from quickjoiner.llm.ollama_provider import OllamaProvider


def test_ollama_provider_selected():
    provider = create_provider(LLMConfig(provider="ollama"))
    assert isinstance(provider, OllamaProvider)


def test_litellm_provider_selected():
    provider = create_provider(LLMConfig(provider="litellm"))
    assert isinstance(provider, LiteLLMProvider)


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        create_provider(LLMConfig(provider="nope"))


def test_default_models_resolve():
    assert LLMConfig(provider="anthropic").resolved_model() == "claude-opus-4-8"
    assert LLMConfig(provider="ollama").resolved_model() == "llama3.1"
    assert LLMConfig(provider="ollama", model="qwen2.5").resolved_model() == "qwen2.5"
    assert LLMConfig(provider="litellm").resolved_model() == "gpt-4o-mini"
    assert LLMConfig(provider="litellm", model="claude-3-5-sonnet").resolved_model() == "claude-3-5-sonnet"
