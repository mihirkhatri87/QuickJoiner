"""Application context: wires config, memory, pipeline, provider, and agent together."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
from quickjoiner.memory.reranker import create_reranker


# graph_relations/graph_neighbors/graph_path each bound themselves to a small, fixed
# shape (400 relationships / a hub sample / <=3 path chains) with no model-controllable
# size parameter, and each states its own truncation explicitly in the text it returns.
# Exempt from OnboardingAgent's blanket live_tool_result_max_chars cap — see the
# uncapped_tools comment in agent/agent.py for why applying that cap here anyway
# silently re-truncates an already-complete, already-honest result.
_UNCAPPED_TOOLS = frozenset({"graph_relations", "graph_neighbors", "graph_path"})


def _current_date_line(tz_name: str = "UTC") -> str:
    """Grounds relative dates ("Friday", "last week", "yesterday's deploy") in the real
    clock instead of the model's training cutoff or an invented guess. Without this the
    agent has NO idea what day it is: observed live, a single 22-round tool-calling turn
    resolving "Friday" invented four different, mutually inconsistent "today"s
    (2024-05-21, 2025-05-19, 2025-05-20, "Oct 21 2024") and never actually computed a
    date — the eventual failure (an interactive skill script) was real, but this was an
    independent, silent defect underneath it.

    Date-only, deliberately no time-of-day: Anthropic prompt caching hashes the whole
    system+tools prefix as one unit (see llm/anthropic_provider.py), so anything that
    changes here misses cache on the NEXT request. A per-minute clock would invalidate
    it on every follow-up turn of every conversation; a per-day one costs at most one
    miss every 24h, which is the same order of cost the codebase already accepts for
    session-compression cache invalidation.
    """
    from quickjoiner.agent.dates import load_timezone

    tz = load_timezone(tz_name)
    label = "UTC" if tz is timezone.utc else tz_name
    now = datetime.now(timezone.utc).astimezone(tz)
    return (
        f"Current date: {now.strftime('%A, %Y-%m-%d')} ({label}). "
        "For any other date the user names in words, call resolve_dates — do not do the "
        "arithmetic yourself."
    )


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
    # The embedding settings `store.embedder` was built from. Rebuilding a fastembed
    # embedder reloads the ONNX model eagerly, so `apply_config()` only does it when
    # these actually changed.
    embedding_signature: dict | None = None

    def apply_config(self) -> None:
        """Re-derive everything that was BUILT FROM config, after `self.config` changed.

        `build_context` constructs the pipeline and store once at process start, so a
        settings save used to reach the persisted config and nothing else: turning on
        `graph.extract_triples` left the already-built pipeline holding no triple
        extractor, and a clean re-sync then ingested with the old behaviour — silently,
        since nothing reports which config a running pipeline was born with. Retrieval
        knobs the store itself holds (hybrid, rrf_k, ANN, reranker) had the same trap.
        Called after every settings write, so a toggle applies to the next sync/query
        without restarting the server.
        """
        signature = self.config.embedding.model_dump()
        embedder = self.store.embedder
        if signature != self.embedding_signature:
            embedder = create_embedder(self.config.embedding)
            self.embedding_signature = signature
            self.store = create_store(self.workspace, embedder, self.config.retrieval)
        else:
            # Same vectors, only the query-side knobs moved — mutate in place rather than
            # reopening LanceDB + the FTS sidecar under a possibly-running sync.
            self.store.set_retrieval(self.config.retrieval, create_reranker(self.config.retrieval))
        self.pipeline = _build_pipeline(self.config, self.catalog, self.store, embedder)

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
        scope=None,
    ) -> OnboardingAgent:
        """`scope` (memory.store.SearchScope) narrows this turn to chosen connectors/documents.

        It does two things, and the second is what actually saves round-trips: memory reads
        are filtered inside the query, AND live connector tools for sources outside the scope
        are not offered at all — the model cannot spend a call on a system the user excluded.
        """
        from quickjoiner.agent.control import build_control_tools
        from quickjoiner.agent.ops import build_ops_tools

        provider = self.build_provider(provider_override, model_override)
        # Per-agent (= per chat request) score ledger: graph tools record the chain
        # confidences they compute; candidate answers read displayed confidence from
        # it — server-side numbers only (plan 06 §C).
        ledger: dict[str, float] = {}
        tools = build_builtin_tools(
            self.store, self.catalog, self.pipeline, self.config.retrieval, self.config.gaps,
            score_ledger=ledger, scope=scope, user=user,
            timezone_name=self.config.chat.timezone,
        )
        tools.extend(build_ops_tools(self))
        tools.extend(self.connector_tools(self._scoped_sources(sources, scope)))
        # Self-control tools (plan 08): full API control from chat, gated by the acting user's
        # RBAC role (derived from role_of when not given). Scoped, confirmed, and permission-checked.
        tools.extend(build_control_tools(self, user, role))
        # Skills: packaged expertise from the open Agent Skills format. Only names +
        # descriptions ride the prompt; bodies, references and scripts are fetched on
        # demand, and a user-scoped skill runs only with THIS user's own credentials.
        skills_prompt, skill_tools = self.skill_tools(user)
        tools.extend(skill_tools)
        system = SYSTEM_PROMPT + f"\n\n{_current_date_line(self.config.chat.timezone)}"
        system += f"\n\n{skills_prompt}" if skills_prompt else ""
        system += f"\n\n{extra_system}" if extra_system else ""
        return OnboardingAgent(
            provider, tools, system,
            tool_result_max_chars=self.config.chat.live_tool_result_max_chars,
            score_ledger=ledger,
            uncapped_tools=_UNCAPPED_TOOLS,
        )

    def skill_roots(self) -> list[tuple[Path, str]]:
        """Where skills are looked for, most authoritative first.

        The workspace folder is the portable one — it travels with the deployment and is
        what an upload writes into. The personal Claude Code / Copilot folders are read
        too, so a skill already written for those tools works here with no copying; they
        simply don't exist on a container host, where the workspace folder is the whole
        story. A same-named workspace skill deliberately shadows a personal one.
        """
        roots = [(self.workspace / "skills", "workspace")]
        home = Path.home()
        roots.append((home / ".claude" / "skills", "personal (claude)"))
        roots.append((home / ".copilot" / "skills", "personal (copilot)"))
        return roots

    def skill_configs(self, installed_by: str = "") -> list:
        """Every discovered skill joined to QuickJoiner's stored configuration for it."""
        from quickjoiner.skills import discover, sync_registry

        return sync_registry(self.catalog, discover(self.skill_roots()), installed_by)

    def secret_store(self):
        from quickjoiner.skills import SecretStore

        if getattr(self, "_secret_store", None) is None:
            self._secret_store = SecretStore(self.catalog, self.workspace)
        return self._secret_store

    def skill_tools(self, user: str | None) -> tuple[str, list]:
        """(prompt section, tools) for this user. Best-effort: a broken skill folder or
        secret store costs the skills feature, never the whole agent."""
        from quickjoiner.skills import build_skill_tools, catalogue_prompt

        try:
            configs = self.skill_configs()
            if not configs:
                return "", []
            store = self.secret_store()
            return catalogue_prompt(configs, user, store), build_skill_tools(configs, user, store)
        except Exception:
            return "", []

    def _scoped_sources(self, sources: list | None, scope) -> list | None:
        """The sources whose live tools this turn may use.

        Scoping to specific documents still leaves their OWN connector's live tools available
        (asking about one GitLab doc may reasonably need a current-state lookup in that
        project) — it only removes the connectors the user did not pick. An empty scope is
        unchanged behaviour: every visible source contributes its tools.
        """
        if scope is None or scope.is_empty():
            return sources
        allowed = set(scope.source_ids)
        for doc_id in scope.doc_ids:
            row = self.catalog.document_source(doc_id)
            if row:
                allowed.add(row)
        pool = sources if sources is not None else list(self.config.sources)
        return [s for s in pool if f"{s.type}:{s.name}" in allowed]

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

    def visible_source_ids(self, user: str | None) -> list[str] | None:
        """The source ids `user` may READ, or None when nothing is restricted.

        None is the open-mode / single-user answer and means "apply no visibility filter at
        all" — distinct from `[]`, which means this user may read nothing. Every memory read
        narrows its scope through this (see `SearchScope.narrowed_to`), so a private source's
        documents stop being retrievable, citable and graph-reachable for everyone else
        rather than merely being hidden from the connector list.
        """
        from quickjoiner.auth import Auth

        if not Auth(self.catalog).enabled:
            return None
        return self.catalog.visible_source_ids(user)


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

    def extractor(text: str, title: str, guidance: str = "") -> list:
        if "provider" not in state:
            with lock:
                if "provider" not in state:
                    try:
                        state["provider"] = create_provider(config.llm)
                    except Exception:
                        state["provider"] = None
        return extract_doc_triples(state["provider"], text, title, guidance=guidance)

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


def _build_pipeline(config: Config, catalog, store, embedder) -> IngestPipeline:
    """The one place an IngestPipeline is wired from config — shared by first-time
    construction (`build_context`) and by `AppContext.apply_config()`, so a rebuilt
    pipeline can't drift from the one the process started with."""
    triple_extractor = _make_triple_extractor(config) if config.graph.extract_triples else None
    entity_resolver = (
        _make_entity_resolver(config, catalog, embedder) if config.graph.entity_resolution else None
    )
    return IngestPipeline(
        store, catalog, config.retrieval, config.graph, triple_extractor,
        entity_resolver, triple_workers=config.graph.triple_workers,
    )


def build_context(workspace: Path) -> AppContext:
    load_env(workspace)
    catalog = create_catalog(workspace)
    config = catalog.load_config()
    embedder = create_embedder(config.embedding)
    store = create_store(workspace, embedder, config.retrieval)
    return AppContext(
        workspace=workspace, config=config, catalog=catalog, store=store,
        pipeline=_build_pipeline(config, catalog, store, embedder),
        embedding_signature=config.embedding.model_dump(),
    )
