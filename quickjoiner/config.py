"""Workspace resolution and configuration loading/saving."""

from __future__ import annotations

import copy
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
    thinking_budget: int = 4096  # extra max_tokens headroom for thinking (Anthropic)
    # Anthropic prompt caching (cache_control breakpoints). One breakpoint on the system
    # prompt caches tools+system together; a moving breakpoint on the conversation tail
    # makes rounds 2..N of every tool loop and every follow-up turn read the prefix at
    # ~0.1x input price. Writes cost 1.25x, so break-even is 2 requests — guaranteed
    # whenever a tool fires. Harmless when the prefix is under the model's cacheable
    # minimum (it silently doesn't cache); off only if a fronting proxy rejects the field.
    prompt_cache: bool = True
    # litellm only: name of the env var holding the proxy's bearer key (never store the
    # secret itself). Empty/unset var -> no Authorization header (keyless local proxies).
    api_key_env: str = "LITELLM_API_KEY"
    # litellm only: reasoning-effort hint for OpenAI-compatible reasoning models
    # (gpt-oss & co.): "low" | "medium" | "high". None -> not sent (backend default,
    # usually medium). The big cost/latency dial on a large reasoning model; a backend
    # that rejects the param is handled by the provider's strip-and-retry on 400.
    reasoning_effort: str | None = None
    # litellm only: OpenAI parallel_tool_calls. False forces at most one tool call per
    # turn (Harmony-family models like gpt-oss are happiest that way); None -> not sent.
    parallel_tool_calls: bool | None = None
    # litellm only: attach an Anthropic-style cache_control marker to the system message
    # (as a content-part extra LiteLLM forwards when the proxy routes to Claude). Off by
    # default — a strict non-LiteLLM backend may reject it (also strip-and-retried).
    proxy_cache_control: bool = False

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "claude-opus-4-8")


class EmbeddingConfig(BaseModel):
    provider: str = "fastembed"  # fastembed | ollama
    model: str | None = None
    base_url: str = "http://localhost:11434"
    # Asymmetric retrieval: prepend the model's task instruction to queries vs passages
    # (bge's "Represent this sentence…" on queries; nomic's search_query:/search_document:;
    # e5's query:/passage:). These models were TRAINED this way, so it lifts recall — but
    # it shifts the cosine distribution, so re-check retrieval.min_score after enabling.
    # For bge the passage side is unchanged (empty prefix) => query-time only, NO re-embed;
    # for nomic/e5 passages change too => re-sync to re-embed. OFF by default to preserve
    # existing corpora and the bge-tuned 0.55 gate. See memory/embedder._INSTRUCTIONS.
    instruct: bool = False
    # fastembed only: which execution device runs the ONNX embedding model.
    #   "auto" (default) — use an NVIDIA GPU if onnxruntime exposes CUDAExecutionProvider
    #     (i.e. the `[gpu]` extra / onnxruntime-gpu is installed AND CUDA/cuDNN load), else CPU.
    #   "cuda"/"gpu" — require the GPU (falls back to CPU with a warning if unavailable).
    #   "cpu" — force CPU (the prior behaviour).
    # Embedding is the CPU bottleneck on a large ingest; the GPU offloads it. Same model either
    # way, so vectors differ only by tiny float rounding — but any embedding change warrants a
    # gate re-check (retrieval.min_score) per the house rule; run `qj eval --calibrate` after
    # switching an existing corpus to GPU.
    device: str = "auto"

    def resolved_model(self) -> str:
        return self.model or DEFAULT_EMBED_MODELS.get(self.provider, "BAAI/bge-small-en-v1.5")


class RetrievalConfig(BaseModel):
    top_k: int = 8
    # Below this cosine-similarity score hits are dropped; if nothing clears it the agent
    # must say "not learned yet". Retuned 2026-07-29 (plan 05, coupled with the
    # ann_refine_factor fix above): the old 0.55 was calibrated against IVF_PQ's
    # DISTORTED scores. True cosines shift the whole distribution up, and a 12-case
    # refusal set found NO threshold cleanly separates refusal near-misses (0.62-0.78)
    # from real answerable hits (0.66-0.87) — `qj eval --calibrate`'s own max-margin
    # pick was 0.72, but that cost 3-4 of 20 answerable cases their grounding. 0.64 is
    # a deliberately conservative middle ground: zero measured answerable-recall cost
    # on that eval set, while still gating out some refusal near-misses (3 of 12 vs 0
    # at the old 0.55) — see docs/plans/05-eval-on-connected-org.md for the full sweep.
    # Retune again (via --calibrate) if you switch embedding models or the eval set grows.
    # NOTE: this gate stays cosine-based even in hybrid mode — hybrid changes which
    # candidates surface and their order, never the grounded-vs-refuse decision.
    min_score: float = 0.64
    # Hybrid retrieval: dense (vector) + sparse (BM25 full-text) legs fused with
    # reciprocal rank fusion. The sparse leg rescues exact-token matches (error
    # codes, ticket IDs, service names) that sit outside the dense top-k.
    hybrid: bool = True
    rrf_k: int = 60  # RRF constant: score = sum(1 / (rrf_k + rank)); 60 is the literature default
    candidate_multiplier: int = 4  # each leg fetches top_k * this before fusion
    # Contextual chunking: prepend a provenance/structure breadcrumb (source · title ·
    # path, plus the markdown section heading) to each chunk before embedding + indexing,
    # so a chunk's vector carries the context it would otherwise be split away from. Biggest
    # single retrieval-quality win for code/wikis; changes what is embedded, so retune
    # min_score if you toggle it on an existing corpus (re-sync to re-embed).
    contextual_chunks: bool = True
    # LanceDB builds an approximate (IVF) vector index once the chunk count crosses
    # this threshold; below it brute-force search is exact and fast enough.
    ann_min_rows: int = 4000
    # LanceDB's default IVF index is IVF_PQ (product-quantized) and is lossy enough to
    # matter. `refine_factor` re-ranks the ANN candidates against their un-quantized
    # vectors; without it the index silently degrades retrieval on any workspace past
    # ann_min_rows.
    # **Measured directly on the live 56,401-chunk corpus (real IvfPq index, 8-bit PQ /
    # 24 sub-vectors, lancedb 0.34.0, 2026-07-30), 12 real queries:**
    #   recall@5 vs exact brute force ... refine=1: 45%      refine=10: 100%
    #   top-1 identical to exact ........ refine=1: 9/12     refine=10: 12/12
    #   p50 latency ..................... refine=1: 16.4ms   refine=10: 17.6ms
    #                                     (brute force 21.3ms — refined ANN is still faster)
    # NB the failure mode is **missing documents, not distorted scores**: for any chunk
    # the unrefined search did return, its reported cosine matched exact to 4 decimal
    # places. It substitutes worse-but-plausible chunks and reports honest-looking scores
    # for them, so nothing in the numbers reveals the loss — which is what let it ship.
    # (Plan 05's 2026-07-28 note described it as a 0.25-0.45 score distortion on the same
    # top chunk; that form did not reproduce on this version/corpus. Same root cause, same
    # fix, and the fix is validated either way.)
    # Harmless below ann_min_rows (no index yet) — LanceDB ignores it for brute force.
    # `ge=1` is load-bearing, not decoration: LanceDB RAISES "Refine factor cannot be
    # zero" on 0, so an unvalidated 0 (the obvious way someone would try to "turn this
    # off") would break EVERY dense search on an indexed workspace. There is no reason
    # to want it off — without it retrieval is simply worse — so 0 is rejected at
    # config validation instead of silently degrading back to the defect.
    ann_refine_factor: int = Field(default=10, ge=1)
    # Second-stage ranking with a cross-encoder over the fused candidates. A
    # cross-encoder reads query+candidate together, so it resolves nuance (code vs
    # prose, near-duplicates) that bi-encoder cosine misses. On by default; "none"
    # disables it. "fastembed" downloads a small ONNX cross-encoder on first use
    # (needs network once, then cached; failures degrade gracefully to RRF order).
    reranker: str = "fastembed"  # fastembed | none
    reranker_model: str | None = None  # None -> Xenova/ms-marco-MiniLM-L-6-v2
    rerank_candidates: int = 24  # how many fused candidates the reranker scores
    # Graph-expansion retrieval: after grounded hits are found, surface documents
    # linked to them through the knowledge graph (1 hop) that the vector search
    # missed — the multi-hop / cross-source correlation channel. It NEVER changes the
    # grounded-vs-refuse decision (it only runs when there are already grounded hits)
    # and only adds clearly-labeled related leads for the agent to follow/cite.
    graph_expansion: bool = True
    graph_expansion_limit: int = 5  # max related documents surfaced per search
    # Alias query expansion (memory/expansion.py): before searching, look up the org's
    # spoken forms in the query against the knowledge graph and append the canonical
    # entity name, so a loose question ("nautical models") also retrieves docs indexed
    # under the formal package name. Query-time (no re-embed); the twin of ingest-time
    # aliasing in connectors/deps.py. It only ADDS canonical tokens to the query, so it
    # can surface hits the raw query missed but never invents a match from nothing.
    alias_expansion: bool = True


class ChatConfig(BaseModel):
    # Context compression: when a session's estimated tokens exceed the threshold,
    # older turns are folded into a rolling summary (LLM if available, else a
    # deterministic digest) and only the recent turns stay verbatim.
    compress_after_est_tokens: int = 6000
    keep_recent_messages: int = 12
    tool_result_max_chars: int = 2000  # persisted tool outputs are truncated to this
    # Live agent-loop cap: a single tool result is truncated to this before being fed
    # back to the model, so an unbounded connector tool (e.g. the full Octopus dashboard)
    # can't overflow the context window and make the provider reject the follow-up turn.
    live_tool_result_max_chars: int = 24000
    learn_from_conversations: bool = True  # distill durable facts into memory on compression
    # Per-question file attachments (context for a single question, NOT long-term memory —
    # kept separate from the Uploads connector). Their extracted text injected into the turn
    # is capped to this many chars, and the files themselves are auto-deleted after N days.
    attachment_context_max_chars: int = 24000
    context_retention_days: int = 7


class GapsConfig(BaseModel):
    # Knowledge-debt backlog: every refusal (search_memory NO_RESULTS) is logged and
    # clustered into remediable gaps. Turn off to stop logging entirely.
    enabled: bool = True
    # Privacy: when False, the raw query text is NOT stored (hash-only mode) — clustering
    # then falls back to exact normalized-query equality. Use in shared/cloud workspaces.
    store_queries: bool = True
    # Cosine similarity above which two refusal queries share a cluster.
    cluster_threshold: float = 0.8


class GraphConfig(BaseModel):
    # Knowledge-graph enrichment. Deterministic extractors (dependency maps, code
    # structure, ticket refs, connector metadata) ALWAYS run. This gates the optional
    # LLM relationship extraction over ingested prose documents, which costs one LLM
    # call per qualifying document at ingest — hence off by default.
    extract_triples: bool = False
    triple_doc_kinds: list[str] = Field(
        default_factory=lambda: ["doc", "page", "issue", "note", "wiki", "ticket", "incident"]
    )
    triple_min_chars: int = 400  # skip trivially short documents
    # Concurrent LLM calls for triple extraction during one ingest batch — the
    # extractor is the bottleneck on a large corpus (one blocking network call per
    # qualifying doc). Measured live against the real broker (plan 05, 2026-07-28):
    # p50 ~18s/call, so the old default of 4 meant ~10h to drain 5.6k docs, where
    # 16 keeps latency flat with zero errors and finishes in ~1-2h (32 tested clean
    # too, but is more likely to be impolite to a shared broker).
    triple_workers: int = 16
    # Entity-resolution dedup (ingest/entity_resolution.py): merge a newly-seen
    # entity into an existing one of the same type via embedding-candidate search +
    # LLM adjudication (Graphiti-style), instead of creating a duplicate node every
    # time the same real-world thing is named differently across sources.
    entity_resolution: bool = False


class ReposConfig(BaseModel):
    # Per-repo architecture briefs (agent/repo_docs.py, `qj agents-md`). Deterministic
    # on demand; this only gates the *automatic* one-shot generation on sync.
    # Auto-generate a repo's AGENTS.md the first time a git/files source is synced and
    # has no generated brief yet. One LLM call per repo, fired ONCE (guarded on the
    # generated doc's existence), never on every sync — off by default so a sync stays
    # free unless the user opts in. Manual `qj agents-md <source>` always works regardless.
    auto_agents_md: bool = False


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
    graph: GraphConfig = Field(default_factory=GraphConfig)
    repos: ReposConfig = Field(default_factory=ReposConfig)
    sources: list[SourceConfig] = Field(default_factory=list)


# --- shipped defaults reaching workspaces that already exist -------------------------
#
# The settings blob is persisted SPARSELY (Catalog.save_config dumps with
# exclude_defaults=True), so a field the user never set is simply absent and picks up
# whatever default this file currently ships. That is what makes a retune here reach
# everyone. It did NOT used to be: save_config dumped every field, so the value in force
# the first time a workspace saved was materialised into the blob and pinned there
# forever — measured 2026-07-31 on the live corpus, still running min_score 0.55 two
# days after 0.64 shipped, which is why plan 05's retune had no effect on the only real
# corpus it was calibrated against.
#
# A blob written by that older code has every field materialised, so "never set" and
# "deliberately set" are indistinguishable in it — EXCEPT for a value that exactly equals
# a default this file has since superseded. Those are listed below and dropped once (see
# Catalog.load_config), letting the current default apply. Anything else is treated as a
# real customisation and kept: the live workspace's deliberate `triple_workers: 8`
# survives untouched, and so would a 0.55 someone chose on purpose *after* 0.64 shipped
# (it would have been re-saved sparsely by then, so it isn't in the blob to prune).
#
# WHEN YOU CHANGE A DEFAULT ABOVE: add the old value here and bump DEFAULTS_EPOCH — that
# is the step that makes the change reach anyone who already has a workspace.
SUPERSEDED_DEFAULTS: dict[str, tuple] = {
    "retrieval.min_score": (0.55,),      # -> 0.64 (plan 05 retune, 2026-07-29)
    "retrieval.reranker": ("none",),     # -> "fastembed" (cross-encoder on by default)
    "graph.triple_workers": (4,),        # -> 16 (measured p50 ~18s/call)
}

# Bumped whenever SUPERSEDED_DEFAULTS gains an entry, so the reconciliation runs again
# for a workspace that has not re-saved (and thus not sparsified) since the last one.
DEFAULTS_EPOCH = 1


def _is_same_value(value: object, candidate: object) -> bool:
    """Equality that won't confuse a bool with 0/1 (JSON round-trips both as scalars)."""
    if isinstance(value, bool) != isinstance(candidate, bool):
        return False
    return value == candidate


def reconcile_superseded_defaults(blob: dict) -> tuple[dict, list[tuple[str, object, object]]]:
    """Drop stored values that are only a superseded shipped default (pure).

    Returns `(pruned_blob, adopted)`, where `adopted` holds one `(path, was, now)` per
    field the current default now governs — so the caller can SAY what moved instead of
    changing a workspace's retrieval behaviour silently. The input blob is not mutated.
    """
    fresh = Config()
    pruned = copy.deepcopy(blob)
    adopted: list[tuple[str, object, object]] = []
    for path, superseded in SUPERSEDED_DEFAULTS.items():
        group, _, field = path.partition(".")
        section = pruned.get(group)
        if not isinstance(section, dict) or field not in section:
            continue
        was = section[field]
        if not any(_is_same_value(was, old) for old in superseded):
            continue  # a real customisation (or already the current value) — keep it
        now = getattr(getattr(fresh, group), field)
        if _is_same_value(was, now):
            continue  # nothing would change; leave the blob alone
        del section[field]
        adopted.append((path, was, now))
        if not section:
            pruned.pop(group, None)
    return pruned, adopted


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
