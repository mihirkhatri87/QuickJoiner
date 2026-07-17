"""parse_candidates + evidence resolution + server-side confidence attach (plan 06 §C).
Strictness bar = ingest/triples.py: malformed dropped silently, capped, never raises,
never repairs, LLM-reported confidence never forwarded."""

from __future__ import annotations

from quickjoiner.agent.candidates import (
    Candidate,
    attach_confidence,
    filter_resolvable,
    known_refs,
    parse_candidates,
)

_BLOCK = """Here are the recorded interpretations.

```candidates
1. Direct usage-events claim from meeting notes | confidence=0.30 | sources: Jan 6, 2026
2. Via Stevedore on ServiceBus | confidence=0.90 | sources: Connector/AGENTS.md
```
"""


def test_parse_well_formed_block_strips_prose():
    prose, cands = parse_candidates(_BLOCK)
    assert "```candidates" not in prose
    assert prose.startswith("Here are the recorded interpretations.")
    assert [c.rank for c in cands] == [1, 2]
    assert cands[0].sources == ("Jan 6, 2026",)
    assert cands[1].summary == "Via Stevedore on ServiceBus"
    assert all(c.confidence is None for c in cands)  # the LLM's number is DISCARDED


def test_malformed_lines_dropped_silently():
    text = (
        "```candidates\n"
        "1. Good line | confidence=0.50 | sources: Doc A\n"
        "not a candidate line at all\n"
        "2. Missing sources part | confidence=0.40\n"
        "3. Bad confidence | confidence=high | sources: Doc B\n"
        "```"
    )
    _prose, cands = parse_candidates(text)
    assert len(cands) == 1 and cands[0].summary == "Good line"


def test_cap_at_five():
    lines = "\n".join(f"{i}. Option {i} | confidence=0.50 | sources: Doc" for i in range(1, 9))
    _prose, cands = parse_candidates(f"```candidates\n{lines}\n```")
    assert len(cands) == 5


def test_all_invalid_block_leaves_text_untouched():
    text = "Answer prose.\n```candidates\ngarbage only\n```"
    prose, cands = parse_candidates(text)
    assert cands == [] and prose == text  # block stays visible, content never lost


def test_no_block_passthrough():
    assert parse_candidates("Just an ordinary answer.") == ("Just an ordinary answer.", [])
    assert parse_candidates("") == ("", [])


def test_known_refs_extracts_tool_output_shapes():
    messages = [
        {"role": "user", "content": "ignored [source: Nope]"},
        {"role": "tool", "content":
            "[source: Deploys | uri: file://d.md | kind: doc | score: 0.71]\nWe deploy…"},
        {"role": "tool", "content":
            "1. Connector --depends_on--> Nautical (usage events) [evidence: Jan 6, 2026]"},
    ]
    refs = known_refs(messages)
    assert refs == {"deploys", "jan 6, 2026"}


def test_filter_resolvable_requires_every_tag():
    refs = {"jan 6, 2026", "connector/agents.md"}
    ok = Candidate(1, "s", ("Jan 6, 2026",))
    partial = Candidate(2, "s", ("Connector/AGENTS.md", "Fabricated Doc"))
    assert filter_resolvable([ok, partial], refs) == [ok]  # any invalid tag drops the line


def test_attach_confidence_min_of_ledger_or_none():
    refs = {"jan 6, 2026", "connector/agents.md"}
    ledger = {"jan 6, 2026": 0.25, "connector/agents.md": 0.55}
    both = Candidate(1, "s", ("Jan 6, 2026", "Connector/AGENTS.md"))
    unscored = Candidate(2, "s", ("Some Other Doc",))
    scored = attach_confidence([both, unscored], refs, ledger)
    assert scored[0].confidence == 0.25  # min over resolved refs — weakest link
    assert scored[1].confidence is None  # no ledger entry -> "unscored", never invented
