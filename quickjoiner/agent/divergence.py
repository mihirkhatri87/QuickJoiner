"""Per-source breakdown: showing what each system separately says.

The motivating report (2026-08-05): "list all teams and their team members" is answerable
from an internal catalogue, from Confluence charters, and from TFS team configuration; the
user got one merged answer with no sign the alternatives existed. Retrieval returns a flat
ranked list of chunks and the knowledge graph returns a **union**, so in both channels the
per-system picture was invisible by construction.

**What measurement ruled out, and why this module is smaller than it first was.** The
obvious retrieval-side rule — "several sources have competitive hits, so several systems
each answer this" — was implemented and then measured against the real 11-connector corpus.
It fired on **80% of ordinary questions**, and a threshold sweep (score band × minimum
documents per source) found **no setting that separates the two classes**: recall 1.00 came
with 75% false positives, and pushing noise to 6% cost all recall. That is not a tuning
problem, it is an absence of signal — "what does X depend on" legitimately draws on TFS, a
repo and a wiki *jointly building one answer*, and from provenance alone that is identical
to three systems each answering alone. So there is deliberately **no retrieval-side
announcement** here; the `[source: …]` label already on every chunk carries the same
information without spending context on it, and the ledger below still runs, invisibly.

What survives is the graph-side read, because there the claims are directly comparable:
`graph_relations` returns a union of typed edges, so we can ask what each system actually
asserted for one group. Two corrections that measurement forced, both of which had made the
first version over-claim:

  * **A naming variant is not a disagreement.** TFS spells a team by its full backlog path
    (`AppRiver\\SecureCloud 2.0\\Caffeine`) where a catalogue says `Caffeine`. Comparing raw
    names reported three of the top contested groups as conflicts when the systems agreed
    exactly, so names are folded to their last path segment before comparison.
  * **Differing coverage is not a conflict.** Three systems each listing different services
    in an environment are describing different parts of it, not contradicting each other —
    partial coverage is the norm in an org corpus. So this reports *what each system says*
    and states plainly that a difference may be coverage rather than contradiction. It never
    calls the systems wrong, because nothing here can tell which of those two it is.

Pure module: no I/O, no catalog, no store.
"""

from __future__ import annotations

import re

from quickjoiner.agent.confidence import classify_evidence, score_edge

# Team/project names arrive fully qualified from some trackers and bare from others. The
# last path segment is the name a person uses and the one every other source stores.
_PATH_SEP = re.compile(r"[\\/]")


def source_label(source_id: str) -> str:
    """`web_scrape:Plumber` -> `Plumber`. The connector's name is what the user called it
    and what the UI shows; the type prefix is plumbing."""
    return (source_id or "").split(":", 1)[-1] or source_id


def fold_member(name: str) -> str:
    """The comparable form of a member name.

    `AppRiver\\SecureCloud 2.0\\Caffeine` and `Caffeine` are one team spelled two ways, and
    treating them as different members manufactured conflicts between systems that agreed.
    Folding to the last path segment is enough for the real cases and stays predictable —
    it never merges two genuinely different names.
    """
    return _PATH_SEP.split((name or "").strip())[-1].strip().lower()


def membership_by_source(rows) -> list[tuple[str, list[str]]]:
    """`[(source label, members)]` for ONE `graph_relations` group whose systems list
    different members — or `[]` when they list the same ones.

    The graph is a union: every system's assertions land as edges on the same entities, so
    a merged group cannot show that the charter names someone the catalogue does not. The
    provenance is still on each edge's evidence document, and this reads it back out.

    Comparison is on folded names, so a naming convention is not mistaken for a difference.
    A row whose evidence has no source (a `same_as` bridge cites no document) is skipped
    rather than grouped under a blank name; if that leaves fewer than two systems there is
    nothing to compare.
    """
    by_source: dict[str, dict[str, str]] = {}
    for r in rows:
        source_id = (r["evidence_source_id"] or "").strip()
        if not source_id:
            continue
        member = r["src_name"] or r["src"]
        if member:
            # Keep the first spelling seen for display; compare on the folded key.
            by_source.setdefault(source_id, {}).setdefault(fold_member(member), member)
    if len(by_source) < 2:
        return []
    if len({frozenset(m) for m in by_source.values()}) < 2:
        return []  # same members, differently spelled at most — agreement, not a split
    return [
        (source_label(sid), sorted(members.values()))
        for sid, members in sorted(by_source.items(), key=lambda kv: source_label(kv[0]))
    ]


def render_membership_split(dst: str, split: list[tuple[str, list[str]]]) -> list[str]:
    """The lines appended under one group whose systems list different members.

    Deliberately says "lists differ", not "the systems disagree": a difference here is just
    as likely to be partial coverage, and asserting a contradiction we cannot verify would
    put a false claim in the transcript.
    """
    if not split:
        return []
    return [f"    - per {label}: {', '.join(members)}" for label, members in split]


def ledger_entries_for_hits(hits) -> dict[str, float]:
    """Citation ref -> server-side confidence, for the per-request score ledger.

    Runs on every grounded search, invisibly: it renders nothing and changes no answer. Its
    only job is that a candidate the model *does* offer carries a real number. Before this,
    only `graph_path` populated the ledger, so any candidate drawn from memory rendered
    "unscored" — which is what limited plan 06's carousel to graph questions.

    Keyed by the lowercased citation label, matching what `search_memory` puts in its
    `[source: …]` refs and what `candidates.attach_confidence` matches against. Corroboration
    is counted per source: distinct documents from that source backing the hit, with
    `source_corroboration` reflecting how many distinct sources appeared at all.
    """
    if not hits:
        return {}
    by_source: dict[str, set[str]] = {}
    for h in hits:
        by_source.setdefault(h.source_id, set()).add(h.doc_id)
    source_count = len(by_source)

    out: dict[str, float] = {}
    for h in hits:
        key = (h.title or h.uri or "").strip().lower()
        if not key:
            continue
        score = score_edge(
            classify_evidence(h.title, h.uri, h.kind),
            doc_corroboration=len(by_source.get(h.source_id, ())),
            source_corroboration=source_count,
        )
        out[key] = max(out.get(key, 0.0), score)
    return out
