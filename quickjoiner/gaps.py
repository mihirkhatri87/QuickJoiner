"""Knowledge-debt backlog: turn refusals into a remediable gap backlog.

Capture happens in `agent/tools.py` (the `search_memory` NO_RESULTS branch logs a
gap via `catalog.log_gap`). This module is the read-time layer — pure functions that
cluster open gaps by query similarity and attach remediation suggestions
(which connector to add, which known entity is nearby). Nothing here touches the DB
except the optional `catalog.resolve_entity` lookups for entity hints.

Clustering is first-fit greedy over query embeddings, deterministic given the gaps'
creation order. Gaps captured in hash-only privacy mode (empty `query`) cluster by
exact normalized-query hash instead; with no text to inspect, connector/entity
suggestions are unavailable, so those clusters carry only a count.
"""

from __future__ import annotations

import math
import re

# query keyword -> connector types that would most likely learn the answer.
# Insertion order fixes the order of suggested_connectors for determinism.
TERM_HINTS: dict[str, list[str]] = {
    "deploy": ["octopus"], "deployment": ["octopus"], "deployed": ["octopus"],
    "release": ["octopus"], "rollback": ["octopus"],
    "ticket": ["jira"], "sprint": ["jira"], "epic": ["jira"], "backlog": ["jira"],
    "issue": ["jira"], "story": ["jira"],
    "wiki": ["confluence"], "runbook": ["confluence"], "doc": ["confluence"],
    "docs": ["confluence"], "documentation": ["confluence"], "page": ["confluence"],
    "log": ["grafana", "datadog", "elastic"], "logs": ["grafana", "datadog", "elastic"],
    "error": ["grafana", "datadog", "elastic"], "trace": ["grafana", "datadog", "elastic"],
    "metric": ["grafana", "datadog"], "metrics": ["grafana", "datadog"],
    "alert": ["grafana", "datadog"],
    "pipeline": ["azure_devops", "github"], "build": ["azure_devops", "github"],
    "ci": ["azure_devops", "github"], "cd": ["azure_devops", "github"],
    "repo": ["git", "github"], "repository": ["git", "github"], "code": ["git", "github"],
    "commit": ["git", "github"], "branch": ["git", "github"],
}

_TOKEN = re.compile(r"[a-z0-9]+")
_NGRAM_WORD = re.compile(r"[A-Za-z0-9.\-]+")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def suggest(text: str, catalog=None) -> tuple[list[str], list[dict]]:
    """(suggested connector types, known-entity hints) for a cluster's query text.

    Connector suggestions come from the TERM_HINTS keyword table. Entity hints, when a
    catalog is provided, come from resolving 1–3 word n-grams of the text against the
    knowledge graph (so a refusal that names a known repo/package surfaces it)."""
    tokens = set(_TOKEN.findall(text.lower()))
    connectors: list[str] = []
    for term, types in TERM_HINTS.items():
        if term in tokens:
            for t in types:
                if t not in connectors:
                    connectors.append(t)

    entity_hints: list[dict] = []
    if catalog is not None and text.strip():
        words = _NGRAM_WORD.findall(text)
        seen: set[str] = set()
        for n in (3, 2, 1):
            for i in range(len(words) - n + 1):
                gram = " ".join(words[i:i + n]).strip(".-")
                key = gram.lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                ent = catalog.resolve_entity(gram)
                if ent and all(e["id"] != ent["id"] for e in entity_hints):
                    entity_hints.append(
                        {"id": ent["id"], "name": ent["name"], "type": ent["type"]}
                    )
    return connectors, entity_hints[:5]


def _medoid(queries: list[str]) -> str:
    """The most representative query (highest average token-Jaccard to the rest).
    First maximum wins, so it's deterministic given the input order."""
    if len(queries) == 1:
        return queries[0]
    toksets = [set(_TOKEN.findall(q.lower())) for q in queries]
    best_i, best_score = 0, -1.0
    for i, ti in enumerate(toksets):
        score = sum(
            len(ti & tj) / (len(ti | tj) or 1) for j, tj in enumerate(toksets) if i != j
        )
        if score > best_score:
            best_score, best_i = score, i
    return queries[best_i]


def _build_cluster(members: list[dict], catalog) -> dict:
    queries = [m["query"] for m in members if (m.get("query") or "").strip()]
    gap_ids = [m["id"] for m in members]
    if queries:
        label = _medoid(queries)
        connectors, entities = suggest(" ".join(queries), catalog)
    else:
        label = "(private refusals)"  # hash-only privacy mode: no text to show
        connectors, entities = [], []
    return {
        "id": gap_ids[0],  # stable cluster id = earliest gap id
        "label": label,
        "count": len(members),
        "samples": queries[:3],
        "suggested_connectors": connectors,
        "entity_hints": entities,
        "gap_ids": gap_ids,
    }


def cluster_gaps(gaps: list[dict], embedder, threshold: float = 0.8, catalog=None) -> list[dict]:
    """Cluster open gap rows into remediable clusters, largest first.

    Rows with query text cluster by embedding cosine (first-fit ≥ threshold, centroid =
    running mean of members). Rows without text (hash-only privacy mode) cluster by exact
    query_hash equality. `catalog` (optional) enables entity hints in the suggestions."""
    rows = sorted(gaps, key=lambda g: (g.get("created_at") or "", g.get("id") or ""))
    clusters: list[dict] = []
    for row in rows:
        q = (row.get("query") or "").strip()
        if q:
            vec = embedder.embed_query(q)
            placed = None
            for c in clusters:
                if c["centroid"] is not None and _cosine(vec, c["centroid"]) >= threshold:
                    placed = c
                    break
            if placed is not None:
                placed["members"].append(row)
                placed["_vecs"].append(vec)
                n = len(placed["_vecs"])
                placed["centroid"] = [sum(col) / n for col in zip(*placed["_vecs"])]
            else:
                clusters.append({"centroid": vec, "_vecs": [vec], "hash": None, "members": [row]})
        else:
            h = row.get("query_hash")
            placed = None
            for c in clusters:
                if c["hash"] is not None and c["hash"] == h:
                    placed = c
                    break
            if placed is not None:
                placed["members"].append(row)
            else:
                clusters.append({"centroid": None, "_vecs": [], "hash": h, "members": [row]})

    out = [_build_cluster(c["members"], catalog) for c in clusters]
    out.sort(key=lambda c: (-c["count"], c["id"]))
    return out
