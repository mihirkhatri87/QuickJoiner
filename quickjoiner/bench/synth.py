"""Deterministic synthetic knowledge graph, for benchmarking without a real corpus.

`qj bench`'s retrieval and agent layers measure the workspace they are pointed at, which is
the right default — latency on somebody's real 57k-chunk corpus is the number that matters.
But that makes every measurement unreproducible off that one machine: a contributor, a CI
runner, or a cloud session has no corpus at all, so the graph layer has nothing to time and a
performance claim cannot be checked by anyone but its author.

This builds a graph of a stated size from a fixed seed, so the same shape appears on every
machine. It is explicitly NOT a stand-in for real data — the text is nonsense and the topology
is random, so retrieval quality figures taken from it would be meaningless. What it reproduces
faithfully is the thing the graph reads are actually sensitive to: **row counts**. Both path
searches load the whole edge table, so their cost is set by how many edges exist and how wide
each row is, not by what the edges say.

That distinction is why this exists at all. The bottleneck it was written for
(`_edge_scan` pulling 15 columns through three LEFT JOINs for every edge in the graph) was
invisible to 1062 passing correctness tests and only appeared under row counts a fixture-sized
graph never reaches — 92% of a 1.45s call, at a scale no test suite had ever built.
"""

from __future__ import annotations

import random

# Shaped after a real corpus rather than picked for roundness: the live workspace this was
# calibrated against ran ~100k edges over ~36k entities and ~20k documents, i.e. a graph
# whose average degree is small but whose absolute row count is large — which is exactly the
# regime where a per-row cost dominates and a per-query cost does not.
LIVE_SCALE = {"entities": 36_000, "edges": 100_000, "documents": 20_000}

_TYPES = ("service", "repo", "package", "team", "environment")
_RELS = ("depends_on", "owns", "deploys", "part_of", "works_on")
_KINDS = ("code", "page", "ticket")


def build_synthetic_graph(catalog, entities: int = 4_000, edges: int = 12_000,
                          documents: int = 2_000, seed: int = 7) -> dict:
    """Populate `catalog` with a random-but-reproducible graph; return what it wrote.

    Uses the ordinary public write path (`upsert_document` / `upsert_entity` /
    `replace_doc_edges`), so the rows are indistinguishable from ingested ones and the
    benchmark measures the real read path rather than a special-cased fixture. Edges are
    grouped by evidence document because `replace_doc_edges` is keyed that way, which also
    reproduces the real fan-out of many edges citing one document.

    Defaults are a fraction of `LIVE_SCALE` so a test can call this in a second or two; pass
    `**LIVE_SCALE` to reproduce the corpus the path-search work was measured against.
    """
    rng = random.Random(seed)
    stamp = "2026-08-15"

    for i in range(documents):
        catalog.upsert_document(f"synth-d{i}", f"synth:src{i % 5}", f"https://synth/{i}",
                                f"Synthetic doc {i}", _KINDS[i % len(_KINDS)],
                                f"hash{i}", stamp, 1)
    ids: list[str] = []
    for i in range(entities):
        etype = _TYPES[i % len(_TYPES)]
        eid = f"{etype}:e{i}"
        ids.append(eid)
        catalog.upsert_entity(eid, f"Entity {i}", etype, "synth:src0")

    # A set, because replace_doc_edges writes to a table keyed
    # (src, rel, dst, evidence_doc_id) — emitting a duplicate would silently write one row
    # and make the reported edge count a lie.
    grouped: dict[str, list[tuple[str, str, str, str]]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    attempts = 0
    while len(seen) < edges and attempts < edges * 10:
        attempts += 1
        a, b = rng.randrange(entities), rng.randrange(entities)
        if a == b:
            continue
        doc = f"synth-d{rng.randrange(documents)}"
        key = (ids[a], rng.choice(_RELS), ids[b], doc)
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(doc, []).append(key)

    for doc, rows in grouped.items():
        catalog.replace_doc_edges(doc, [(s, r, d, "") for s, r, d, _ in rows])

    return {"entities": entities, "edges": len(seen), "documents": documents, "seed": seed}


def sample_pairs(catalog, count: int = 12, seed: int = 7) -> list[tuple[str, str]]:
    """Entity id pairs to time path searches between, drawn from whatever graph exists.

    Deliberately NOT filtered to pairs that are connected. An unconnected pair is the
    EXPENSIVE case — the BFS exhausts its frontier instead of returning early on a hit — and
    it is also the case a user hits whenever they ask about two things that turn out to be
    unrelated. Timing only the happy path would flatter the numbers and hide precisely the
    work this measurement exists to watch.
    """
    rng = random.Random(seed)
    rows = catalog._read_all("SELECT id FROM entities ORDER BY id LIMIT 5000")
    ids = [r["id"] for r in rows]
    if len(ids) < 2:
        return []
    return [(rng.choice(ids), rng.choice(ids)) for _ in range(count)]
