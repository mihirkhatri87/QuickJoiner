"""Strict parsing of the model's optional ```candidates block (plan 06 §C).

House style mirrors ingest/triples.py::parse_triples exactly — LLM output that must
never be trusted verbatim into structured state: one anchored bounded regex per
line, off-format lines silently dropped, whole-line-drop on any invalid part,
hard cap, never raises, never repairs.

The block format the prompt requests (tags are SEMICOLON-separated — evidence titles
routinely contain commas, e.g. the motivating "Jan 6, 2026" page):
    ```candidates
    1. <one-line summary> | confidence=<0.00-1.00> | sources: <tag>; <tag>
    ```
The model's confidence number is parsed for FORMAT VALIDATION ONLY and then
discarded — displayed confidence comes from the server-side score ledger the graph
tools populated this turn (invariant: confidence is computed from real signals,
never LLM self-report). A candidate survives only if every one of its source tags
resolves against a citation ref actually returned by tools this turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

_MAX_CANDIDATES = 5

_BLOCK = re.compile(r"```candidates[ \t]*\n(.*?)\n?```", re.DOTALL)
_LINE = re.compile(
    r"^(\d{1,2})[.)]\s+(.{1,200}?)\s*\|\s*confidence\s*=\s*"
    r"(0(?:\.\d{1,2})?|1(?:\.0{1,2})?)\s*\|\s*sources\s*:\s*(.{1,300}?)\s*$"
)
# The exact shapes the built-in tools emit refs in: "[source: <label> | …]" from
# search_memory / graph expansion, "[evidence: <title>]" from the graph tools.
_REF = re.compile(r"\[source:\s*([^|\]]+)|\[evidence:\s*([^\]]+)\]")


@dataclass(frozen=True)
class Candidate:
    rank: int
    summary: str
    sources: tuple[str, ...]
    confidence: float | None = None  # server-attached (ledger), never the LLM's number


def parse_candidates(text: str) -> tuple[str, list[Candidate]]:
    """(prose with the block removed, parsed candidates). The block is only
    stripped when at least one candidate parsed — an all-invalid block stays in
    the text as a visible code fence rather than silently vanishing."""
    if not text:
        return text, []
    m = _BLOCK.search(text)
    if m is None:
        return text, []
    cands: list[Candidate] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        lm = _LINE.match(line)
        if lm is None:
            continue  # off-format: dropped silently, never repaired
        rank, summary, _confidence_discarded, sources = lm.groups()
        tags = tuple(t.strip() for t in sources.split(";") if t.strip())
        if not tags:
            continue
        cands.append(Candidate(rank=int(rank), summary=summary.strip(), sources=tags))
        if len(cands) >= _MAX_CANDIDATES:
            break
    if not cands:
        return text, []
    stripped = (text[: m.start()] + text[m.end():]).strip()
    return stripped, cands


def known_refs(messages: list[dict]) -> set[str]:
    """Normalized citation refs present in THIS turn's tool results — the only
    things a candidate's source tags may resolve against."""
    refs: set[str] = set()
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        for m in _REF.finditer(str(msg.get("content", ""))):
            ref = (m.group(1) or m.group(2) or "").strip().lower()
            if ref:
                refs.add(ref)
    return refs


def _matches(tag: str, ref: str) -> bool:
    return tag in ref or ref in tag


def filter_resolvable(cands: list[Candidate], refs: set[str]) -> list[Candidate]:
    """Keep a candidate iff it has >= 1 tag and EVERY normalized tag resolves to a
    known ref — drop-whole-line-on-any-invalid-part, the parse_triples posture."""
    out = []
    for c in cands:
        tags = [t.strip().lower() for t in c.sources]
        if tags and all(any(_matches(t, r) for r in refs) for t in tags):
            out.append(c)
    return out


def attach_confidence(cands: list[Candidate], refs: set[str],
                      ledger: dict[str, float]) -> list[Candidate]:
    """Server-side confidence per candidate: min over its tags' best ledger scores
    (weakest link), or None ("unscored") when any tag has no scored ref this turn."""
    out = []
    for c in cands:
        scores: list[float] = []
        complete = True
        for tag in (t.strip().lower() for t in c.sources):
            matched = [s for k, s in ledger.items() if _matches(tag, k)]
            if matched:
                scores.append(max(matched))
            else:
                complete = False
                break
        conf = round(min(scores), 2) if complete and scores else None
        out.append(replace(c, confidence=conf))
    return out
