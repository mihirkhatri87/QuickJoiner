"""Entity resolution: embedding-candidate recall + LLM adjudication merges a
newly-seen graph entity into an existing same-type node instead of creating a
duplicate, modeled on Graphiti's node-dedup approach but kept in the SQL
catalog (see quickjoiner/ingest/entity_resolution.py).

FakeEmbedder (tests/conftest.py) is a plain whitespace bag-of-words hash, not a
semantic model, so test entity names are chosen to share literal tokens where
similarity is expected — the resolver's candidate-search/merge *logic* is what's
under test here, not embedding quality (that's exercised live against the real
model separately)."""

from __future__ import annotations

from quickjoiner.ingest.entity_resolution import EntityResolver, make_llm_adjudicator
from quickjoiner.llm.base import ChatResult

from tests.conftest import FakeEmbedder


class _Provider:
    def __init__(self, text: str):
        self._text = text

    def chat(self, messages, system=None, **kw):
        return ChatResult(text=self._text)


def test_exact_id_already_known_is_a_noop(catalog):
    catalog.upsert_entity("service:connector", "Connector", "service")
    resolver = EntityResolver(catalog=catalog, embedder=FakeEmbedder())
    canonical, merged = resolver.resolve("service:connector", "Connector", "service")
    assert canonical == "service:connector" and merged is False


def test_unresolvable_type_is_never_touched(catalog):
    catalog.upsert_entity("symbol:foo", "foo", "symbol")
    resolver = EntityResolver(catalog=catalog, embedder=FakeEmbedder(),
                               adjudicate=lambda *a: "foo")
    # Different id, same type "symbol" (not in RESOLVABLE_TYPES) -> passthrough, no LLM call.
    canonical, merged = resolver.resolve("symbol:bar", "bar", "symbol")
    assert canonical == "symbol:bar" and merged is False


def test_no_candidates_within_floor_creates_new_entity(catalog):
    catalog.upsert_entity("service:completely-unrelated-topic", "Completely Unrelated Topic", "service")
    resolver = EntityResolver(catalog=catalog, embedder=FakeEmbedder())
    canonical, merged = resolver.resolve("service:widget-factory", "Widget Factory", "service")
    assert canonical == "service:widget-factory" and merged is False


def test_llm_confirms_merge_adds_alias_to_canonical(catalog):
    catalog.upsert_entity("project:appriver-connector-web", "AppRiver Connector Web", "project")
    resolver = EntityResolver(
        catalog=catalog, embedder=FakeEmbedder(),
        adjudicate=lambda type_, name, candidates, ctx, cctxs: candidates[0],  # always confirm top
    )
    canonical, merged = resolver.resolve("project:connector-web-service", "Connector Web Service", "project")
    assert merged is True
    assert canonical == "project:appriver-connector-web"
    resolved = catalog.resolve_entity("connector web service")
    assert resolved and resolved["id"] == "project:appriver-connector-web"


def test_llm_rejects_merge_creates_new_entity(catalog):
    catalog.upsert_entity("project:appriver-connector-web", "AppRiver Connector Web", "project")
    resolver = EntityResolver(
        catalog=catalog, embedder=FakeEmbedder(),
        adjudicate=lambda type_, name, candidates, ctx, cctxs: None,  # never confirms
    )
    canonical, merged = resolver.resolve("project:connector-web-service", "Connector Web Service", "project")
    assert merged is False
    assert canonical == "project:connector-web-service"


def test_keyless_fallback_merges_only_above_strict_cutoff(catalog):
    catalog.upsert_entity("service:appriver-nautical", "AppRiver Nautical", "service")
    resolver = EntityResolver(catalog=catalog, embedder=FakeEmbedder(), adjudicate=None)

    # Same words, different case -> identical bag-of-words -> merges even without an LLM.
    canonical, merged = resolver.resolve("service:appriver-nautical-2", "appriver nautical", "service")
    assert merged is True and canonical == "service:appriver-nautical"

    # Partial overlap only -> not confident enough to auto-merge without an LLM to ask.
    canonical2, merged2 = resolver.resolve(
        "service:nautical-client-service", "Nautical Client Service", "service"
    )
    assert merged2 is False and canonical2 == "service:nautical-client-service"


def test_repeated_calls_reuse_cached_candidate_embeddings(catalog):
    catalog.upsert_entity("service:one", "One", "service")
    embed_calls = []
    embedder = FakeEmbedder()
    orig_embed = embedder.embed

    def counting_embed(texts):
        embed_calls.append(list(texts))
        return orig_embed(texts)

    embedder.embed = counting_embed  # type: ignore[method-assign]
    resolver = EntityResolver(catalog=catalog, embedder=embedder)
    resolver.resolve("service:two", "Two", "service")
    resolver.resolve("service:three", "Three", "service")
    # The existing-entities-of-type bucket ("One") is only embedded once across both calls.
    bucket_embeds = [c for c in embed_calls if c == ["One"]]
    assert len(bucket_embeds) == 1


def test_make_llm_adjudicator_parses_match_and_none():
    match = make_llm_adjudicator(_Provider("AppRiver Connector Web"))
    assert match("project", "Webroot Connector", ["AppRiver Connector Web"]) == "AppRiver Connector Web"

    none_reply = make_llm_adjudicator(_Provider("NONE"))
    assert none_reply("project", "Webroot Connector", ["AppRiver Connector Web"]) is None


# ------------------------------------------- adjudicator context (plan 06 §1.D)

def test_adjudicator_receives_contexts(catalog):
    """The resolver hands the adjudicator the new entity's evidence context AND
    per-candidate evidence summaries (from entity_evidence) — not bare names."""
    catalog.upsert_entity("project:appriver-connector-web", "AppRiver Connector Web", "project")
    catalog.upsert_document("agents", "git:connector", "file:///repo/AGENTS.md",
                            "Connector/AGENTS.md", "doc", "h", None, 1)
    catalog.replace_doc_edges("agents", [
        ("project:appriver-connector-web", "depends_on", "package:x", ""),
    ])
    seen = {}

    def spy(type_, name, candidates, ctx, cctxs):
        seen.update(type=type_, ctx=ctx, cctxs=cctxs)
        return None

    resolver = EntityResolver(catalog=catalog, embedder=FakeEmbedder(), adjudicate=spy)
    resolver.resolve("project:connector-web-service", "Connector Web Service", "project",
                     context='mentioned in "Jan 6, 2026" (doc)')
    assert seen["ctx"] == 'mentioned in "Jan 6, 2026" (doc)'
    assert seen["cctxs"] == ['"Connector/AGENTS.md" (doc)']


def test_make_llm_adjudicator_prompt_contains_evidence_context():
    """The built prompt carries both sides' evidence so the model can judge like a
    human skimming the source pages (bare names alone defaulted to NONE)."""
    captured = {}

    class CapturingProvider:
        def chat(self, messages, system=None, **kw):
            captured["prompt"] = messages[0]["content"]
            captured["system"] = system
            return ChatResult(text="AppRiver Connector Web")

    adj = make_llm_adjudicator(CapturingProvider())
    out = adj("project", "Webroot Connector", ["AppRiver Connector Web"],
              'mentioned in "Jan 6, 2026" (doc)', ['"Connector/AGENTS.md" (doc)'])
    assert out == "AppRiver Connector Web"
    assert 'context: mentioned in "Jan 6, 2026" (doc)' in captured["prompt"]
    assert '"AppRiver Connector Web" — evidence: "Connector/AGENTS.md" (doc)' in captured["prompt"]
    assert "Never guess" in captured["system"]  # NONE-by-default spine kept


def test_context_enables_merge_where_bare_names_said_none(catalog):
    """Webroot-shaped scripted case: an adjudicator that only confirms when it can
    see evidence context merges the nickname into the canonical repo entity."""
    catalog.upsert_entity("project:appriver-connector-web", "AppRiver Connector Web", "project")
    catalog.upsert_document("agents", "git:connector", "file:///repo/AGENTS.md",
                            "Connector/AGENTS.md", "doc", "h", None, 1)
    catalog.replace_doc_edges("agents", [
        ("project:appriver-connector-web", "depends_on", "package:x", ""),
    ])

    def context_dependent(type_, name, candidates, ctx, cctxs):
        return candidates[0] if (ctx and any(cctxs)) else None  # NONE without context

    resolver = EntityResolver(catalog=catalog, embedder=FakeEmbedder(),
                              adjudicate=context_dependent)
    canonical, merged = resolver.resolve(
        "project:webroot-connector-web", "Webroot Connector Web", "project",
        context='mentioned in "Webroot integration overview" (doc)')
    assert merged is True and canonical == "project:appriver-connector-web"
    resolved = catalog.resolve_entity("webroot connector web")
    assert resolved and resolved["id"] == "project:appriver-connector-web"
