"""Evidence classification + deterministic confidence scoring (plan 06 §B)."""

from __future__ import annotations

import pytest

from quickjoiner.agent.confidence import classify_evidence, score_chain, score_edge


@pytest.mark.parametrize("title,uri,kind,expected", [
    # dependency-map: synthesized manifest doc URIs win first
    ("a: dependencies & packages", "file:///repo::dependency-map", "doc", "dependency-map"),
    # meeting-notes: date-shaped titles (the motivating "Jan 6, 2026" page)
    ("Jan 6, 2026", "https://wiki/x/1", "doc", "meeting-notes"),
    ("2026-01-06", "https://wiki/x/2", "doc", "meeting-notes"),
    ("01/06/26", "https://wiki/x/3", "doc", "meeting-notes"),
    ("December 25th, 2025", "https://wiki/x/4", "doc", "meeting-notes"),
    # meeting-notes: vocabulary anywhere in the title
    ("Platform team meeting notes", "https://wiki/x/5", "doc", "meeting-notes"),
    ("Weekly sync — infra", "https://wiki/x/6", "doc", "meeting-notes"),
    ("Q2 Retrospective", "https://wiki/x/7", "doc", "meeting-notes"),
    ("Sprint 12 stand-up", "https://wiki/x/8", "doc", "meeting-notes"),
    # authored-doc: deliberate architecture/overview files by basename
    ("Connector/AGENTS.md", "file:///repo/AGENTS.md", "doc", "authored-doc"),
    ("readme", "https://git/x/README.md?ref=main", "doc", "authored-doc"),
    ("design", "file:///docs/design.rst", "doc", "authored-doc"),
    ("adr 12", "file:///docs/adr-012-queues.md", "doc", "authored-doc"),
    # generic: everything else — incl. formal wiki pages (no computable signal)
    ("AppRiver.Nautical", "https://wiki/x/nautical", "doc", "generic"),
    ("Release note", "w://n", "doc", "generic"),
    # date INSIDE a sentence is not a date-titled page
    ("Migration finished on 2026-01-06 successfully", "https://wiki/x/9", "doc", "generic"),
    # None-safety: LEFT-JOIN rows with missing evidence docs
    (None, None, None, "generic"),
])
def test_classify_evidence_table(title, uri, kind, expected):
    assert classify_evidence(title, uri, kind) == expected


@pytest.mark.parametrize("cls,docs,sources,expected", [
    # §2.3: exact values, not just "a number came back"
    ("authored-doc", 3, 2, 0.90),     # AGENTS.md-sourced, triple-corroborated, 2 sources
    ("meeting-notes", 1, 1, 0.25),    # a lone informal meeting-notes claim
    ("generic", 1, 1, 0.45),
    ("dependency-map", 3, 2, 0.95),   # ceiling: clamped at 0.95, never certain
    ("dependency-map", 1, 1, 0.60),
    ("authored-doc", 2, 1, 0.62),     # +0.075 for the second corroborating doc
    ("meeting-notes", 3, 2, 0.60),    # even weak evidence climbs with corroboration
    ("unknown-class", 1, 1, 0.45),    # unrecognized class falls back to generic base
    ("meeting-notes", 0, 0, 0.25),    # zero counts clamp to 1, never negative bonus
])
def test_score_edge_ordering_and_values(cls, docs, sources, expected):
    assert score_edge(cls, docs, sources) == expected


def test_score_edge_ordering_holds():
    strong = score_edge("authored-doc", 3, 2)
    weak = score_edge("meeting-notes", 1, 1)
    assert strong > weak  # the plan's whole point, asserted directly
    assert 0.05 <= weak < strong <= 0.95


def test_score_chain_is_weakest_link():
    assert score_chain([0.55, 0.25, 0.90]) == 0.25
