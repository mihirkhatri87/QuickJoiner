"""Alias query expansion — the query-side twin of ingest-time aliasing.

The ingest pipeline teaches the knowledge graph the org's spoken forms as entity
aliases (`connectors/deps.py`: "AppRiver.Nautical.Models" is *also referred to as*
"nautical models"). This module walks a user's query for those spoken forms and
appends the canonical entity name, so a loose question ("how does nautical models
auth work") also retrieves documents indexed only under the formal package name —
the user never has to know it.

It reads the same graph the pipeline populates on every sync, so it self-improves as
sources connect — no rebuild. It runs in the agent's `search_memory` and
`/api/search` (NOT inside the stores: they have no catalog handle), gated by
`retrieval.alias_expansion`.

Pure w.r.t. the catalog and safe to call on every search: if nothing resolves, the
query is returned unchanged. Canonical names are appended space-separated (not
parenthesized) so both the real bi-encoder and the token-hash `FakeEmbedder` see the
formal name's tokens cleanly.
"""

from __future__ import annotations

import re

from quickjoiner.connectors.deps import _GENERIC_TOKENS
from quickjoiner.ingest.normalize import normalize_query

# Cap on how many canonical names we append — a runaway expansion would drown the
# real query tokens and shift the cosine distribution the grounding gate depends on.
_MAX_EXPANSIONS = 3
# Slide longest windows first so "core diagnostics healthchecks http" claims the
# 4-token alias before its shorter sub-windows re-resolve a parent entity. The cap of
# 4 covers ~90% of the org's real spoken-form aliases (measured on a connected org:
# 1-token 31%, 2 27%, 3 20%, 4 12%) — longer package names are almost never typed
# verbatim in a question, and every lookup is an exact indexed hit so the extra pass
# is cheap. Matching is exact (alias/name), so a wider window never mis-resolves.
_WINDOW_SIZES = (4, 3, 2, 1)
# A query token: alphanumeric start, then dotted/hyphenated/underscored package-ish
# runs ("appriver.nautical.models", "nautical-models"). Punctuation is dropped.
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.\-_]*")


def expand_query(catalog, query: str, *, max_expansions: int = _MAX_EXPANSIONS) -> str:
    """Return `query` with canonical entity names appended for any org spoken-form it
    contains. Unchanged when nothing resolves.

    Resolves each 1–3-token window via `catalog.resolve_entity` (exact id/name/alias,
    case-insensitive — the same lookup the graph tools use). Windows that are entirely
    generic/stopword tokens are skipped, each entity is appended at most once, and a
    canonical name already literally present in the query is not re-appended.
    """
    tokens = _TOKEN.findall(normalize_query(query).lower())
    if not tokens:
        return query
    lowered_query = query.lower()
    appended: list[str] = []
    seen_ids: set[str] = set()
    covered: set[int] = set()  # token positions already claimed by a longer window
    for size in _WINDOW_SIZES:
        if len(appended) >= max_expansions:
            break
        for i in range(len(tokens) - size + 1):
            if len(appended) >= max_expansions:
                break
            positions = range(i, i + size)
            if any(p in covered for p in positions):
                continue
            window = tokens[i : i + size]
            if all(t in _GENERIC_TOKENS for t in window):
                continue
            phrase = " ".join(window)
            ent = catalog.resolve_entity(phrase)
            if not ent:
                continue
            name = (ent.get("name") or "").strip()
            eid = ent.get("id") or name
            covered.update(positions)  # claim positions so sub-windows don't re-resolve
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            # Skip if the canonical name adds nothing (user already typed the formal
            # name, or it equals the matched spoken form itself).
            if name and name.lower() != phrase and name.lower() not in lowered_query:
                appended.append(name)
    if not appended:
        return query
    return f"{query} {' '.join(appended)}"
