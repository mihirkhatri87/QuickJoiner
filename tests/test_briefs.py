"""Onboarding briefs: grounded generation, refusal when unlearned, save + re-ingest."""

import pytest

from quickjoiner.agent.briefs import BRIEF_SYSTEM, BRIEFS, generate_brief
from quickjoiner.app import AppContext
from quickjoiner.config import Config
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm.base import ChatResult

from tests.test_agent_loop import ScriptedProvider


@pytest.fixture
def ctx(workspace, catalog, store):
    config = Config()
    config.retrieval.min_score = 0.0  # FakeEmbedder scores don't match bge tuning
    return AppContext(
        workspace=workspace,
        config=config,
        catalog=catalog,
        store=store,
        pipeline=IngestPipeline(store, catalog),
    )


def _learn_some_facts(ctx):
    ctx.pipeline.ingest(
        [
            Document(
                uri="https://wiki.acme.test/deploys",
                title="Deploys",
                text="We deploy with Octopus on Fridays. The deployment pipeline environments "
                "are dev, staging, and production.",
            ),
            Document(
                uri="https://gitlab.acme.test/grp/app",
                title="app repo",
                text="The main repository structure: services/payments and services/ledger "
                "talk over gRPC. Code owners: payments team.",
            ),
        ],
        "test:src",
    )


def test_brief_types_registered():
    assert set(BRIEFS) == {"architecture", "week1", "roadmap", "quick-wins"}


def test_unknown_brief_type_raises(ctx):
    with pytest.raises(ValueError, match="Unknown brief type"):
        generate_brief(ctx, "nope", provider=ScriptedProvider([]))


def test_brief_refuses_when_nothing_learned(ctx):
    provider = ScriptedProvider([])  # would crash if chat() were called
    markdown, path = generate_brief(ctx, "architecture", provider=provider)
    assert "haven't learned enough" in markdown
    assert path is None and provider.calls == []


def test_brief_generation_grounds_saves_and_reingests(ctx):
    _learn_some_facts(ctx)
    provider = ScriptedProvider(
        [ChatResult(text="## Services\n- payments and ledger talk over gRPC "
                         "[https://gitlab.acme.test/grp/app]")]
    )
    markdown, path = generate_brief(ctx, "architecture", provider=provider)

    # The prompt was grounded: system prompt + retrieved chunks with their URIs.
    call = provider.calls[0]
    assert BRIEF_SYSTEM in call["system"]
    # ...and dated. The `week1` and `roadmap` specs ask what is in flight, upcoming or
    # overdue; the date used to be computed AFTER this call, so the model never saw it and
    # judged recency against nothing.
    assert "Today is" in call["system"]
    prompt = call["messages"][0]["content"]
    assert "Octopus on Fridays" in prompt and "[https://wiki.acme.test/deploys]" in prompt

    # Output saved under workspace/briefs and returned.
    assert path is not None and path.exists()
    assert path.read_text(encoding="utf-8") == markdown
    assert "gRPC" in markdown and markdown.startswith("# Architecture map")

    # Re-ingested into memory under the briefs source.
    hits = ctx.store.search("payments ledger gRPC services brief", top_k=10, min_score=0.0)
    assert any(h.uri.startswith("brief://architecture/") for h in hits)
    sources = {s["id"]: s for s in ctx.catalog.list_sources()}
    assert "briefs:generated" in sources and sources["briefs:generated"]["doc_count"] == 1
