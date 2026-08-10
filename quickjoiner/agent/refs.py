"""Citable references, lifted out of tool output so a citation can become a link.

The retrieval tools already hand the MODEL everything needed to cite well — each hit
carries `[source: <label> | uri: <uri> | kind: <kind> | score: <n>]`. The client never
saw any of it: the answer text carries only whatever label the model chose to write,
so a citation of "Caffeine - Team Charter" had no URL behind it and could not be a
link, while a citation of the raw URL was clickable but unreadable.

This parses those headers back out of the tool output and sends them alongside the
answer, so the UI can resolve a cited LABEL to the page it came from.

House style follows candidates.py: one bounded regex, off-format lines silently
dropped, never raises, never repairs. The strictness matters more here than usual —
this feeds hyperlinks, and a citation that links to the wrong page is worse than one
that doesn't link at all.
"""

from __future__ import annotations

import re

# Every shape the tools emit. `search_memory` puts the header on its own line with the
# chunk beneath it; the graph-expansion section emits a one-line bullet with a trailing
# "— <src> <rel> <dst>" summary and no kind/score; the graph tools cite their evidence
# as "[evidence: <title> | uri: <uri>]". The graph form matters more than it looks: a
# question like "list all teams with their members" is answered from graph_relations,
# so WITHOUT it the majority of citations in exactly that kind of answer resolve to
# nothing — the same document cited via the graph would behave differently from the
# same document cited via search.
_HEADER = re.compile(
    r"\[(?:source|evidence):\s*(?P<label>[^|\]\n]{1,300}?)\s*\|\s*uri:\s*(?P<uri>[^|\]\n]{0,600}?)\s*"
    r"(?:\|\s*kind:\s*(?P<kind>[^|\]\n]{0,60}?)\s*)?"
    r"(?:\|\s*score:\s*(?P<score>[0-9.]{1,6})\s*)?\]"
)

MAX_REFS = 60
SNIPPET_CHARS = 220


# Contextual chunking prepends "[<source_id> · <title> · <uri>]" to every chunk before
# embedding (ingest.pipeline.breadcrumb). It is provenance for the vector, not content,
# and in an excerpt it merely repeats the title and uri already shown beside it — so the
# snippet starts at the document's own first words instead.
_BREADCRUMB = re.compile(r"^\[[^\]\n]*·[^\]\n]*\]\s*\n?")

# A graph_relations group cites several documents as consecutive brackets on ONE line
# ("Caffeine (7): [evidence: A | uri: …] [evidence: B | uri: …]"), so the text following
# the first bracket is the SECOND bracket, not prose. Without this the panel showed raw
# "[evidence: … | uri: …]" markup as the excerpt (seen live). Leading sibling brackets are
# dropped, and a later one ends the snippet — one source must never quote another's.
_SIBLING_REF = re.compile(r"^\s*\[(?:source|evidence):[^\]\n]*\]")


def _snippet(text: str, start: int) -> str:
    """The readable beginning of what this source actually said — the excerpt shown
    under the title in the sources list. Bounded, whitespace-collapsed, and cut at a
    word boundary so it doesn't end mid-token."""
    body = text[start : start + SNIPPET_CHARS * 3]
    body = body.lstrip(" \t\r\n—-")
    body = _BREADCRUMB.sub("", body, count=1).lstrip()
    while True:  # drop any sibling citations sitting between this ref and its text
        stripped = _SIBLING_REF.sub("", body)
        if stripped == body:
            break
        body = stripped.lstrip(" \t\r\n—-")
    # Stop at the separator between hits, or at the next citation, so one source can
    # never quote the next one's text.
    for stop in ("\n\n---\n\n", "\n[", "[source:", "[evidence:"):
        cut = body.find(stop)
        if cut > 0:
            body = body[:cut]
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) <= SNIPPET_CHARS:
        return body
    clipped = body[:SNIPPET_CHARS]
    space = clipped.rfind(" ")
    return (clipped[:space] if space > SNIPPET_CHARS // 2 else clipped).rstrip() + "…"


def parse_source_refs(output: str) -> list[dict]:
    """Every citable reference in one tool result, in the order it appeared.

    Deduped by (label, uri) keeping the FIRST occurrence, which is the highest-scoring
    hit — `search_memory` returns hits in score order, and the first mention carries
    the most relevant snippet. A header with an empty uri is still returned: the label
    remains worth listing in the sources panel, it simply cannot be linked.
    """
    if not output:
        return []
    refs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _HEADER.finditer(output):
        label = (m.group("label") or "").strip()
        uri = (m.group("uri") or "").strip()
        if not label:
            continue
        key = (label, uri)
        if key in seen:
            continue
        seen.add(key)
        ref = {"label": label, "uri": uri, "snippet": _snippet(output, m.end())}
        kind = (m.group("kind") or "").strip()
        if kind:
            ref["kind"] = kind
        try:
            if m.group("score"):
                ref["score"] = float(m.group("score"))
        except ValueError:
            pass
        refs.append(ref)
        if len(refs) >= MAX_REFS:
            break
    return refs
