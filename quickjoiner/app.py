"""Application context: wires config, memory, pipeline, provider, and agent together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quickjoiner.agent.agent import OnboardingAgent
from quickjoiner.agent.prompts import SYSTEM_PROMPT
from quickjoiner.agent.tools import build_builtin_tools
from quickjoiner.config import Config, load_env
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm import create_provider
from quickjoiner.memory.base import CatalogBackend, StoreBackend
from quickjoiner.memory.embedder import create_embedder
from quickjoiner.memory.factory import create_catalog, create_store


@dataclass
class AppContext:
    workspace: Path
    config: Config
    catalog: CatalogBackend
    store: StoreBackend
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
        sources: list | None = None,
    ) -> OnboardingAgent:
        from quickjoiner.agent.ops import build_ops_tools

        provider = self.build_provider(provider_override, model_override)
        tools = build_builtin_tools(
            self.store, self.catalog, self.pipeline, self.config.retrieval, self.config.gaps
        )
        tools.extend(build_ops_tools(self))
        tools.extend(self.connector_tools(sources))
        system = SYSTEM_PROMPT + (f"\n\n{extra_system}" if extra_system else "")
        return OnboardingAgent(provider, tools, system)

    def connector_tools(self, sources: list | None = None):
        """Live read-from-source tools contributed by configured connectors.

        `sources=None` means all configured sources; pass a filtered list to
        scope the agent's live tools to what a given user may see.
        """
        from quickjoiner.connectors.registry import create_connector

        tools = []
        for source in self.config.sources if sources is None else sources:
            try:
                tools.extend(create_connector(source, self.workspace).tools())
            except Exception:
                continue  # a misconfigured source shouldn't break the agent
        return tools

    def visible_sources(self, user: str | None):
        """Sources `user` may see: everything in open mode, else own + shared."""
        from quickjoiner.auth import Auth, visible

        enabled = Auth(self.catalog).enabled
        return [s for s in self.config.sources if visible(s, user, enabled)]


def build_context(workspace: Path) -> AppContext:
    load_env(workspace)
    catalog = create_catalog(workspace)
    config = catalog.load_config()
    embedder = create_embedder(config.embedding)
    store = create_store(workspace, embedder, config.retrieval)
    pipeline = IngestPipeline(store, catalog)
    return AppContext(
        workspace=workspace, config=config, catalog=catalog, store=store, pipeline=pipeline
    )
