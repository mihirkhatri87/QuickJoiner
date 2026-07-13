"""Agent-tool bridge (agent/ops.py): scrape/connect/sync as agent tools sharing
the slash commands' service functions."""

from __future__ import annotations

import pytest

from quickjoiner.agent.ops import build_ops_tools
from quickjoiner.app import AppContext
from quickjoiner.config import Config
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline


@pytest.fixture
def ctx(workspace, catalog, store):
    config = Config()
    config.retrieval.min_score = 0.0
    return AppContext(
        workspace=workspace,
        config=config,
        catalog=catalog,
        store=store,
        pipeline=IngestPipeline(store, catalog),
    )


def _tools(ctx):
    return {t.spec.name: t for t in build_ops_tools(ctx)}


def test_scrape_website_returns_excerpts_and_saves_report(ctx, monkeypatch):
    from quickjoiner.connectors.browser.scraper import WebScrapeConnector

    def fake_sync(self, state):
        assert self.options["max_depth"] == 2 and self.options["max_pages"] == 20
        yield Document(uri="https://ex.test/", title="Home",
                       text="Example is a demo platform for onboarding tests. " * 5, kind="doc")
        yield Document(uri="https://ex.test/docs", title="Docs",
                       text="The docs explain the deploy process in detail. " * 5, kind="doc")

    monkeypatch.setattr(WebScrapeConnector, "sync", fake_sync)
    out = _tools(ctx)["scrape_website"].run(url="https://ex.test/")
    assert "Crawled 2 readable pages" in out
    assert "[Home] (https://ex.test/)" in out and "demo platform" in out
    assert "Report saved to" in out and "Nothing was ingested" in out
    assert list((ctx.workspace / "scrapes").glob("*.md"))  # artifact trail kept


def test_scrape_website_rejects_non_http(ctx):
    out = _tools(ctx)["scrape_website"].run(url="ftp://nope")
    assert out.startswith("ERROR") and "user explicitly gave" in out


def test_list_connector_types_documents_fields(ctx):
    out = _tools(ctx)["list_connector_types"].run()
    assert "- jira" in out and "- web_scrape" in out
    assert "base_url*" in out  # required marker
    assert "(secret)" in out and "env:MY_TOKEN" in out


def test_add_connector_persists_and_syncs(ctx, tmp_path):
    docs = tmp_path / "team-docs"
    docs.mkdir()
    (docs / "deploys.md").write_text("# Deploys\nWe deploy with Octopus on Fridays.",
                                     encoding="utf-8")
    out = _tools(ctx)["add_connector"].run(
        name="handbook", type="files", options={"path": str(docs)}, sync_now=True)
    assert "Connector 'handbook' (files) saved" in out
    assert "Sync of 'handbook' finished: 1 added" in out
    assert [s.name for s in ctx.catalog.list_source_configs()] == ["handbook"]  # persisted
    hits = ctx.store.search("deploy with Octopus on Fridays", top_k=3, min_score=0.0)
    assert hits and hits[0].source_id == "files:handbook"

    # duplicate names refused
    again = _tools(ctx)["add_connector"].run(name="handbook", type="files", options={})
    assert again.startswith("ERROR") and "already exists" in again


def test_add_connector_failed_test_not_saved(ctx, tmp_path):
    out = _tools(ctx)["add_connector"].run(
        name="ghost", type="files", options={"path": str(tmp_path / "missing")})
    assert out.startswith("NOT SAVED") and "Path not found" in out
    assert ctx.catalog.list_source_configs() == []

    unknown = _tools(ctx)["add_connector"].run(name="x", type="teleporter", options={})
    assert unknown.startswith("ERROR") and "list_connector_types" in unknown


def test_sync_source_unknown_name(ctx):
    out = _tools(ctx)["sync_source"].run(name="nope")
    assert out.startswith("ERROR") and "none configured" in out


def test_ops_tools_registered_in_agent_build(ctx):
    """build_agent wires ops tools in; assemble the same list it does."""
    from quickjoiner.agent.tools import build_builtin_tools

    tools = build_builtin_tools(ctx.store, ctx.catalog, ctx.pipeline, ctx.config.retrieval)
    tools.extend(build_ops_tools(ctx))
    names = {t.spec.name for t in tools}
    assert {"search_memory", "remember", "graph_neighbors", "graph_path",
            "scrape_website", "add_connector", "list_connector_types",
            "sync_source"} <= names