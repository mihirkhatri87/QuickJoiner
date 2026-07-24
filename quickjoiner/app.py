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
    # Set by api.app.create_app: the FastAPI app + a per-process secret the control tools
    # (agent/control.py) use to dispatch API calls in-process as the acting user. None until
    # an app is built (the CLI builds one lazily around this same ctx on first control call).
    app: object | None = None
    internal_secret: str = ""

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
        user: str | None = None,
        role: str | None = None,
    ) -> OnboardingAgent:
        from quickjoiner.agent.control import build_control_tools
        from quickjoiner.agent.ops import build_ops_tools

        provider = self.build_provider(provider_override, model_override)
        # Per-agent (= per chat request) score ledger: graph tools record the chain
        # confidences they compute; candidate answers read displayed confidence from
        # it — server-side numbers only (plan 06 §C).
        ledger: dict[str, float] = {}
        tools = build_builtin_tools(
            self.store, self.catalog, self.pipeline, self.config.retrieval, self.config.gaps,
            score_ledger=ledger,
        )
        tools.extend(build_ops_tools(self))
        tools.extend(self.connector_tools(sources))
        # Self-control tools (plan 08): full API control from chat, gated by the acting user's
        # RBAC role (derived from role_of when not given). Scoped, confirmed, and permission-checked.
        tools.extend(build_control_tools(self, user, role))
        system = SYSTEM_PROMPT + (f"\n\n{extra_system}" if extra_system else "")
        return OnboardingAgent(
            provider, tools, system,
            tool_result_max_chars=self.config.chat.live_tool_result_max_chars,
            score_ledger=ledger,
        )

    def connector_tools(self, sources: list | None = None):
        """Live read-from-source tools contributed by configured connectors.

        `sources=None` means all configured sources; pass a filtered list to
        scope the agent's live tools to what a given user may see.

        Consolidation (plan 09): connectors are grouped by TYPE, and a class that exposes a
        `type_tools(connectors)` classmethod (gitlab/github/…) contributes ONE tool set for all
        its instances (each tool taking a `project`/`repo` selector) instead of N near-duplicate
        sets — otherwise 3 GitLab projects × ~13 tools would flood the model's context. Classes
        without it fall back to per-instance `tools()`.
        """
        from quickjoiner.connectors.registry import create_connector

        by_type: dict[str, list] = {}
        for source in self.config.sources if sources is None else sources:
            try:
                conn = create_connector(source, self.workspace)
            except Exception:
                continue  # a misconfigured source shouldn't break the agent
            by_type.setdefault(source.type, []).append(conn)

        tools = []
        for conns in by_type.values():
            type_tools = getattr(type(conns[0]), "type_tools", None)
            if callable(type_tools):
                try:
                    tools.extend(type_tools(conns))
                except Exception:
                    continue
            else:
                for conn in conns:
                    try:
                        tools.extend(conn.tools())
                    except Exception:
                        continue
        return tools

    def visible_sources(self, user: str | None):
        """Sources `user` may see: everything in open mode, else own + shared."""
        from quickjoiner.auth import Auth, visible

        enabled = Auth(self.catalog).enabled
        return [s for s in self.config.sources if visible(s, user, enabled)]


def _make_triple_extractor(config: Config):
    """A lazy (text, title) -> list[Triple] extractor backed by the configured LLM.
    The provider is built on first use and cached (None on failure), so build_context
    stays cheap and keyless workspaces just skip extraction. Thread-safe: triple
    extraction may run concurrently (IngestPipeline's triple_workers), so provider
    construction is guarded by a lock — a shared, thread-safe httpx-backed provider
    is still only built once even if several worker threads race on first use."""
    import threading

    from quickjoiner.ingest.triples import extract_doc_triples
    from quickjoiner.llm import create_provider

    state: dict = {}
    lock = threading.Lock()

    def extractor(text: str, title: str) -> list:
        if "provider" not in state:
            with lock:
                if "provider" not in state:
                    try:
                        state["provider"] = create_provider(config.llm)
                    except Exception:
                        state["provider"] = None
        return extract_doc_triples(state["provider"], text, title)

    return extractor


def _make_entity_resolver(config: Config, catalog, embedder):
    """An EntityResolver wired to the configured LLM for merge adjudication
    (lazy, cached, thread-safe construction — same pattern as the triple
    extractor above; resolver.resolve() itself is only ever called from the
    main ingest thread, never concurrently, so no lock is needed there)."""
    import threading

    from quickjoiner.ingest.entity_resolution import EntityResolver, make_llm_adjudicator
    from quickjoiner.llm import create_provider

    state: dict = {}
    lock = threading.Lock()

    def adjudicate(type_: str, name: str, candidate_names: list[str]):
        if "fn" not in state:
            with lock:
                if "fn" not in state:
                    try:
                        state["fn"] = make_llm_adjudicator(create_provider(config.llm))
                    except Exception:
                        state["fn"] = None
        return state["fn"](type_, name, candidate_names) if state["fn"] else None

    return EntityResolver(catalog=catalog, embedder=embedder, adjudicate=adjudicate)


def build_context(workspace: Path) -> AppContext:
    load_env(workspace)
    catalog = create_catalog(workspace)
    config = catalog.load_config()
    embedder = create_embedder(config.embedding)
    store = create_store(workspace, embedder, config.retrieval)
    triple_extractor = _make_triple_extractor(config) if config.graph.extract_triples else None
    entity_resolver = _make_entity_resolver(config, catalog, embedder) if config.graph.entity_resolution else None
    pipeline = IngestPipeline(
        store, catalog, config.retrieval, config.graph, triple_extractor,
        entity_resolver, triple_workers=config.graph.triple_workers,
    )
    return AppContext(
        workspace=workspace, config=config, catalog=catalog, store=store, pipeline=pipeline
    )
