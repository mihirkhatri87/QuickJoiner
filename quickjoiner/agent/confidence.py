"""Deterministic evidence classification + confidence scoring for graph claims.

Plan 06: confidence is computed server-side from real signals (what kind of document
asserted an edge, how corroborated it is) — never from an LLM's self-reported number.
The motivating failure: a 1-hop `depends_on` edge sourced from an informal
meeting-notes page ("Jan 6, 2026") silently beat the real, documented 3-hop
mechanism (AGENTS.md + wiki) purely by being shorter. Hop count is therefore NOT a
confidence signal here; evidence shape and corroboration are.

Pure module: no I/O, no catalog access — callers pass in what a `_EDGE_SELECT` row
already carries (evidence title/uri/kind) and corroboration counts they queried.
"""

from __future__ import annotations

import re

# Titles that ARE a date ("2026-01-06", "01/06/26", "Jan 6, 2026") — the classic
# meeting-notes/journal page naming pattern seen across the Confluence corpus.
_DATE_TITLE = re.compile(
    r"^\s*(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\s*$",
    re.IGNORECASE,
)
_MEETING_TITLE = re.compile(
    r"\b(meeting notes?|minutes|retro(?:spective)?|stand-?up|weekly sync|1:1|agenda)\b",
    re.IGNORECASE,
)
# Deliberate, authored architecture/overview docs by basename.
_AUTHORED_BASENAME = re.compile(
    r"^(agents|readme|architecture|arch|design|adr(?:[-_].+)?|claude|contributing)\.(md|rst|txt|adoc)$",
    re.IGNORECASE,
)


def classify_evidence(title: str, uri: str, kind: str) -> str:
    """Bucket one evidence document: 'dependency-map' | 'meeting-notes' |
    'authored-doc' | 'generic'. First match wins, checked in that order.

    Honesty note: a dedicated architecture *wiki page* is indistinguishable from any
    other prose page at this layer (Confluence ingests everything as kind="doc"), so
    formal wiki pages land in 'generic' and earn their lift via corroboration instead
    of a shape heuristic we can't actually compute.
    """
    title, uri = title or "", uri or ""  # LEFT-JOIN rows may carry None
    if uri.lower().endswith("::dependency-map"):
        return "dependency-map"
    if _DATE_TITLE.match(title) or _MEETING_TITLE.search(title):
        return "meeting-notes"
    basename = uri.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
    if _AUTHORED_BASENAME.match(basename):
        return "authored-doc"
    return "generic"


# Base confidence by evidence shape. Weights are heuristic and deliberately live in
# this one table (with exact-value tests) so retuning after plan 05's eval runbook is
# a one-line diff. Rationale: a deterministic manifest-derived dependency map is the
# strongest claim; a deliberately authored architecture doc close behind; ordinary
# prose in the middle; an informal meeting-notes/journal page weakest — real, but
# never authoritative on its own (the "Jan 6, 2026" lesson).
_BASE = {"dependency-map": 0.60, "authored-doc": 0.55, "generic": 0.45, "meeting-notes": 0.25}


def score_edge(evidence_class: str, doc_corroboration: int, source_corroboration: int) -> float:
    """Deterministic confidence for one edge, in [0.05, 0.95].

    doc_corroboration = distinct evidence docs asserting this exact edge
    (+0.075 per extra doc, capped at +0.15 for >=3); source_corroboration =
    distinct *sources* those docs came from (+0.20 once >=2 agree — code and wiki
    agreeing means more than two wiki pages). Never 0 or 1: heuristic, not proof.
    Hop count contributes nothing — that is plan 06's entire lesson."""
    s = _BASE.get(evidence_class, 0.45)
    s += 0.15 * (min(max(doc_corroboration, 1), 3) - 1) / 2
    s += 0.20 * min(max(source_corroboration, 1) - 1, 1)
    return round(min(max(s, 0.05), 0.95), 2)


def score_chain(hop_scores: list[float]) -> float:
    """A chain is only as trustworthy as its weakest hop."""
    return min(hop_scores)
