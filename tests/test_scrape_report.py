"""Scrape synthesis (agent/scrape_report.py): deterministic site-map mermaid,
keyless digest fallback, provider synthesis + streaming, and report saving."""

from __future__ import annotations

from quickjoiner.agent.scrape_report import build_report, save_report, site_map_mermaid
from quickjoiner.connectors.base import Document
from quickjoiner.llm.base import ChatResult


def _page(uri: str, title: str, text: str) -> Document:
    return Document(uri=uri, title=title, text=text, kind="doc")


def test_site_map_mermaid_builds_a_path_tree():
    m = site_map_mermaid([
        "https://site.test/docs/setup",
        "https://site.test/docs/usage",
        "https://site.test/pricing",
    ])
    assert m.startswith("graph TD")
    for label in ('"site.test"', '"docs"', '"setup"', '"usage"', '"pricing"'):
        assert label in m
    # edges: site->docs, docs->setup, docs->usage, site->pricing
    assert m.count("-->") == 4


def test_site_map_mermaid_caps_nodes():
    uris = [f"https://big.test/section{i}/page{j}" for i in range(20) for j in range(10)]
    m = site_map_mermaid(uris, max_nodes=15)
    assert m.count('["') <= 15


def test_build_report_keyless_digest(tmp_path):
    pages = [
        _page("https://s.test/a", "Alpha", "alpha content " * 20),
        _page("https://s.test/b", "Beta", "beta content " * 20),
    ]
    md = build_report("https://s.test/", pages, provider=None)
    assert md.startswith("# Scrape report: s.test")
    assert "- [Alpha](https://s.test/a)" in md
    assert "## Site structure" in md and "```mermaid" in md

    path = save_report(tmp_path, "https://s.test/", md)
    assert path.exists() and path.parent.name == "scrapes" and path.suffix == ".md"


def test_build_report_synthesizes_with_provider_and_streams():
    pages = [_page("https://s.test/a", "Alpha", "alpha content")]
    deltas: list[str] = []

    class Provider:
        def chat(self, messages, system=None, on_stream=None, **kw):
            assert "Alpha" in messages[0]["content"]  # page excerpt reached the prompt
            assert "mermaid" in system  # diagram instructions present
            if on_stream:
                on_stream("text", "## Overview\nSynthesized.")
            return ChatResult(text="## Overview\nSynthesized.\n```mermaid\ngraph TD\n  a --> b\n```")

    md = build_report("https://s.test/", pages, provider=Provider(),
                      on_stream=lambda kind, delta: deltas.append(delta))
    assert "Synthesized." in md
    assert md.count("```mermaid") == 2  # the LLM's diagram + the site structure map
    assert deltas == ["## Overview\nSynthesized."]


def test_build_report_degrades_when_provider_fails():
    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("no key")

    md = build_report("https://s.test/", [_page("https://s.test/a", "Alpha", "x" * 100)],
                      provider=Boom())
    assert "LLM synthesis was unavailable" in md
    assert "## Site structure" in md  # deterministic layer still present
