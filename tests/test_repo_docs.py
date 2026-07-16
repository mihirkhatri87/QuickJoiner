"""Repo architecture briefs: grounded generation from code structure + docs, merge
with an existing AGENTS.md, and the QuickJoiner-internal-only save/re-ingest contract."""

import pytest

from quickjoiner.agent.repo_docs import (
    AGENTS_MD_SYSTEM,
    MERGE_ADDENDUM,
    generate_agents_md,
    has_generated_brief,
    maybe_autogenerate,
)
from quickjoiner.app import AppContext
from quickjoiner.config import Config, SourceConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm.base import ChatResult

from tests.test_agent_loop import ScriptedProvider

SOURCE_NAME = "acme-repo"
SOURCE_ID = f"files:{SOURCE_NAME}"


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "acme-repo"
    root.mkdir()
    (root / "app.py").write_text(
        "import os\n\n\ndef start():\n    return os.getcwd()\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# Acme repo\nDoes acme things.\n", encoding="utf-8")
    return root


@pytest.fixture
def ctx(workspace, catalog, store, repo_root):
    config = Config()
    config.retrieval.min_score = 0.0  # FakeEmbedder scores don't match bge tuning
    config.sources = [SourceConfig(name=SOURCE_NAME, type="files", options={"path": str(repo_root)})]
    return AppContext(
        workspace=workspace,
        config=config,
        catalog=catalog,
        store=store,
        pipeline=IngestPipeline(store, catalog),
    )


def _ingest_code_and_docs(ctx):
    ctx.pipeline.ingest(
        [
            Document(
                uri=f"{SOURCE_NAME}::app.py",
                title="app.py",
                text="import os\n\n\ndef start():\n    return os.getcwd()\n",
                kind="code",
            ),
            Document(
                uri="https://wiki.acme.test/acme-repo",
                title="Acme repo wiki",
                text="acme-repo is the entrypoint service; it starts the acme runtime and "
                "talks to the ledger service over HTTP.",
                kind="doc",
            ),
        ],
        SOURCE_ID,
    )


def test_unknown_source_raises(ctx):
    with pytest.raises(ValueError, match="not a configured git/files source"):
        generate_agents_md(ctx, "nope", provider=ScriptedProvider([]))


def test_unsynced_source_raises(ctx):
    ctx.config.sources.append(SourceConfig(name="ghost", type="files", options={"path": "Z:/does/not/exist"}))
    with pytest.raises(ValueError, match="No local clone/root"):
        generate_agents_md(ctx, "ghost", provider=ScriptedProvider([]))


def test_generates_from_scratch_grounds_saves_and_reingests(ctx):
    _ingest_code_and_docs(ctx)
    provider = ScriptedProvider(
        [ChatResult(text="# acme-repo — Architecture Brief\n## What this system is\n"
                         "Entrypoint service [https://wiki.acme.test/acme-repo]")]
    )
    markdown, path = generate_agents_md(ctx, SOURCE_NAME, provider=provider)

    call = provider.calls[0]
    assert call["system"] == AGENTS_MD_SYSTEM  # no merge addendum — nothing existing yet
    prompt = call["messages"][0]["content"]
    assert "app.py" in prompt and "README.md" in prompt  # file tree
    assert "defines" in prompt and "symbol:start" in prompt  # code graph facts
    assert "imports" in prompt and "module:os" in prompt
    assert "entrypoint service" in prompt  # retrieved prose
    assert "EXISTING AGENTS.MD" not in prompt

    assert path.exists()
    assert path.read_text(encoding="utf-8") == markdown
    assert path == ctx.workspace / "generated" / SOURCE_NAME / "AGENTS.md"
    assert "QuickJoiner-generated" in markdown
    assert "generated from scratch" in markdown

    hits = ctx.store.search("acme-repo architecture entrypoint", top_k=10, min_score=0.0)
    assert any(h.uri == f"agents-md://{SOURCE_NAME}" for h in hits)
    sources = {s["id"]: s for s in ctx.catalog.list_sources()}
    assert "generated:agents-md" in sources and sources["generated:agents-md"]["doc_count"] == 1


def test_merges_with_existing_agents_md(ctx):
    _ingest_code_and_docs(ctx)
    ctx.pipeline.ingest(
        [
            Document(
                uri=f"{SOURCE_NAME}::AGENTS.md",
                title="AGENTS.md",
                text="# acme-repo\nHand-written: this repo owns billing reconciliation.",
                kind="doc",
            )
        ],
        SOURCE_ID,
    )
    provider = ScriptedProvider([ChatResult(text="# acme-repo — Architecture Brief (refined)")])
    markdown, path = generate_agents_md(ctx, SOURCE_NAME, provider=provider)

    call = provider.calls[0]
    assert call["system"] == AGENTS_MD_SYSTEM + MERGE_ADDENDUM
    prompt = call["messages"][0]["content"]
    assert "EXISTING AGENTS.MD" in prompt
    assert "billing reconciliation" in prompt
    assert "Refined from an existing AGENTS.md" in markdown


def test_dependency_map_doc_is_used_as_evidence_not_prose(ctx):
    ctx.pipeline.ingest(
        [
            Document(
                uri=f"{SOURCE_NAME}::dependency-map",
                title="Dependency map",
                text="acme-repo depends on AppRiver.Ledger.Client 2.0.0.",
                kind="doc",
            )
        ],
        SOURCE_ID,
    )
    provider = ScriptedProvider([ChatResult(text="# acme-repo — Architecture Brief")])
    _, _ = generate_agents_md(ctx, SOURCE_NAME, provider=provider)

    prompt = provider.calls[0]["messages"][0]["content"]
    assert "AppRiver.Ledger.Client 2.0.0" in prompt
    # The dependency-map doc must not also be double-counted as a generic PROSE block.
    assert prompt.count("AppRiver.Ledger.Client 2.0.0") == 1


# ---------------------------------------------------------------- auto-generation

def test_maybe_autogenerate_off_by_default_is_noop(ctx):
    _ingest_code_and_docs(ctx)
    # config.repos.auto_agents_md defaults to False
    source = ctx.config.sources[0]
    monkey = _CountingProvider()
    ctx.build_provider = lambda *a, **k: monkey
    assert maybe_autogenerate(ctx, source) is None
    assert monkey.calls == 0
    assert not has_generated_brief(ctx.catalog, SOURCE_NAME)


def test_maybe_autogenerate_fires_once_when_enabled(ctx):
    _ingest_code_and_docs(ctx)
    ctx.config.repos.auto_agents_md = True
    source = ctx.config.sources[0]
    provider = _CountingProvider()
    ctx.build_provider = lambda *a, **k: provider

    first = maybe_autogenerate(ctx, source)
    assert first is not None and first.exists()
    assert has_generated_brief(ctx.catalog, SOURCE_NAME)
    assert provider.calls == 1

    # Second sync of the same repo must NOT regenerate — one-shot guard holds.
    second = maybe_autogenerate(ctx, source)
    assert second is None
    assert provider.calls == 1


def test_maybe_autogenerate_skips_non_repo_sources(ctx):
    ctx.config.repos.auto_agents_md = True
    from quickjoiner.config import SourceConfig

    web = SourceConfig(name="site", type="confluence", options={})
    provider = _CountingProvider()
    ctx.build_provider = lambda *a, **k: provider
    assert maybe_autogenerate(ctx, web) is None
    assert provider.calls == 0


def test_maybe_autogenerate_never_raises_on_failure(ctx):
    _ingest_code_and_docs(ctx)
    ctx.config.repos.auto_agents_md = True
    source = ctx.config.sources[0]

    def _boom(*a, **k):
        raise RuntimeError("provider down")

    ctx.build_provider = _boom
    logs: list[str] = []
    # Must swallow the error (return None), and must NOT mark a brief as generated.
    assert maybe_autogenerate(ctx, source, on_log=logs.append) is None
    assert not has_generated_brief(ctx.catalog, SOURCE_NAME)


class _CountingProvider(ScriptedProvider):
    """A provider that always returns a brief and counts how many times it was called,
    so 'fires once' is checkable independently of the scripted-result queue length."""

    def __init__(self):
        super().__init__([])
        self.calls = 0

    def chat(self, messages, system=None, tools=None, on_stream=None):
        self.calls += 1
        return ChatResult(text="# repo — Architecture Brief\nGenerated.")
