"""Application context: wires config, memory, pipeline, provider, and agent together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quickjoiner.agent.agent import OnboardingAgent
from quickjoiner.agent.prompts import SYSTEM_PROMPT
from quickjoiner.agent.tools import build_builtin_tools
from quickjoiner.config import Config, load_config
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm import create_provider
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.embedder import create_embedder
from quickjoiner.memory.store import KnowledgeStore


@dataclass
class AppContext:
    workspace: Path
    config: Config
    catalog: Catalog
    store: KnowledgeStore
    pipeline: IngestPipeline

    def build_provider(self, provider_override: str | None = None, model_override: str | None = None):
        llm = self.config.llm.model_copy()
        if provider_override:
            llm.provider = provider_override
            if not model_override:
                llm.model = None  # fall back to the new provider's default model
        if model_override:
            llm.model = model_override
        return create_provider(llm)

    def build_agent(
        self,
        provider_override: str | None = None,
        model_override: str | None = None,
        extra_system: str | None = None,
    ) -> OnboardingAgent:
        provider = self.build_provider(provider_override, model_override)
        tools = build_builtin_tools(self.store, self.catalog, self.pipeline, self.config.retrieval)
        tools.extend(self.connector_tools())
        system = SYSTEM_PROMPT + (f"\n\n{extra_system}" if extra_system else "")
        return OnboardingAgent(provider, tools, system)

    def connector_tools(self):
        """Live read-from-source tools contributed by configured connectors."""
        from quickjoiner.connectors.registry import create_connector

        tools = []
        for source in self.config.sources:
            try:
                tools.extend(create_connector(source, self.workspace).tools())
            except Exception:
                continue  # a misconfigured source shouldn't break the agent
        return tools


def build_context(workspace: Path) -> AppContext:
    config = load_config(workspace)
    catalog = Catalog(workspace)
    embedder = create_embedder(config.embedding)
    store = KnowledgeStore(workspace, embedder)
    pipeline = IngestPipeline(store, catalog)
    return AppContext(
        workspace=workspace, config=config, catalog=catalog, store=store, pipeline=pipeline
    )
