"""Question autocomplete: pure ranking/needle/template helpers + the graph-backed
QuestionSuggester over a fake catalog (no DB, no LLM)."""

from __future__ import annotations

from quickjoiner.suggest import (
    QuestionSuggester,
    entity_name_match,
    extract_needle_tokens,
    fill_templates,
    rank_suggestions,
)


# --------------------------------------------------------------- needle extraction

def test_extract_needle_strips_question_words_and_keeps_the_noun():
    assert extract_needle_tokens("where is the management console deployed") == ["console", "management"]
    assert extract_needle_tokens("what does securetide mxchecker depend on") == ["mxchecker", "securetide"]
    # all-stopword input -> no needle
    assert extract_needle_tokens("who owns") == []
    # trailing tokens win, capped
    assert extract_needle_tokens("a b cat dog fish bird", max_tokens=2) == ["bird", "fish"]


# --------------------------------------------------------------- template filling

def test_fill_templates_scores_intent_alignment():
    filled = dict(fill_templates("Foo", "service", prefix="where is foo dep"))
    # "depend" template's intent word appears in the prefix -> higher score than "who owns"
    assert filled["What does Foo depend on?"] > filled["Who owns Foo?"]
    assert "Where is Foo deployed?" in filled


def test_fill_templates_falls_back_for_unknown_type():
    texts = [t for t, _ in fill_templates("Bar", "mystery")]
    assert "What is Bar?" in texts


# --------------------------------------------------------------- ranking

def test_rank_dedupes_drops_prefix_echo_and_respects_bands():
    cands = [
        ("What is deployed to Prod?", 2, 5.0),   # entity band
        ("What environments exist?", 1, 9.0),    # starter-prefix band (better band)
        ("What environments exist?", 3, 1.0),    # dup, worse band
        ("hello", 0, 1.0),                        # equals prefix -> dropped
    ]
    out = rank_suggestions("hello", cands, limit=5)
    assert out == ["What environments exist?", "What is deployed to Prod?"]  # band order, deduped, echo dropped


def test_rank_respects_limit():
    cands = [(f"q{i}", 2, float(i)) for i in range(10)]
    assert len(rank_suggestions("q", cands, limit=3)) == 3


# --------------------------------------------------------------- QuestionSuggester

def test_entity_name_match_scores_name_over_alias():
    # exact word > word-prefix > substring > no-name-hit (alias-only)
    assert entity_name_match(["console"], "Admin Console") == 3.0
    assert entity_name_match(["manage"], "User Management") == 2.0  # prefix of "Management"
    assert entity_name_match(["manage"], "CustomerManagement") == 1.0  # substring within one word
    assert entity_name_match(["manage"], "Black Team") == 0.0  # matched only via an alias
    # multi-token names accumulate
    assert entity_name_match(["securetide", "mxchecker"], "AppRiver.SecureTide.MXChecker") == 6.0


class FakeCatalog:
    def __init__(self, entities=None, gaps=None, source_types=None):
        self._entities = entities or []
        self._gaps = gaps or []
        self._types = source_types or []

    def search_entities(self, query, limit=10):
        # match name OR any alias (like the real catalog), so alias-only hits exist
        q = query.lower()
        hits = [
            e for e in self._entities
            if q in e["name"].lower() or any(q in a.lower() for a in e.get("aliases", []))
        ]
        # the real method orders by degree desc — mimic that so tests exercise re-ranking
        return sorted(hits, key=lambda e: -int(e.get("degree", 0)))[:limit]

    def list_gaps(self, status="open"):
        return list(self._gaps)

    def list_source_configs(self):
        class _S:
            def __init__(self, t):
                self.type = t
        return [_S(t) for t in self._types]


def test_suggester_empty_prefix_returns_source_aware_starters():
    cat = FakeCatalog(source_types=["octopus"])
    out = QuestionSuggester(cat).suggest("", limit=6)
    assert any("Octopus" in s or "deployed to" in s for s in out)


def test_suggester_templates_from_named_entity():
    cat = FakeCatalog(
        entities=[{"id": "service:mxchecker", "name": "SecureTide MXChecker", "type": "service", "degree": 12}]
    )
    out = QuestionSuggester(cat).suggest("where is mxchecker", limit=6)
    assert any("SecureTide MXChecker" in s for s in out)
    assert any("deployed" in s.lower() for s in out)


def test_suggester_demotes_high_degree_alias_only_match_below_name_match():
    """A very-connected entity that only matched via an alias must not bury the
    entity whose actual name contains the typed word."""
    cat = FakeCatalog(entities=[
        # alias-only hit for "manage", hugely connected
        {"id": "team:black", "name": "Black Team", "type": "team", "degree": 94,
         "aliases": ["management"]},
        # real name match, far less connected
        {"id": "svc:usermgmt", "name": "User Management", "type": "service", "degree": 8},
    ])
    out = QuestionSuggester(cat).suggest("where is manage", limit=6)
    # the name match wins the top slot; the alias-only team is pushed down/out
    assert any("User Management" in s for s in out)
    assert out[0].endswith("User Management?") or "User Management" in out[0]


def test_suggester_completes_past_question_by_prefix():
    cat = FakeCatalog(gaps=[{"query": "What is the on-call rotation?"}])
    out = QuestionSuggester(cat).suggest("what is the on", limit=6)
    assert "What is the on-call rotation?" in out


def test_suggester_survives_a_catalog_with_no_graph():
    # No entities, no gaps, no sources -> still returns base starters, never raises.
    out = QuestionSuggester(FakeCatalog()).suggest("hi", limit=6)
    assert isinstance(out, list)
