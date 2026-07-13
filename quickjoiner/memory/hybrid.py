"""Reciprocal rank fusion — shared by the LanceDB and Postgres stores.

Each retrieval leg (dense vector, sparse BM25) contributes a ranking; RRF
combines them without needing the legs' scores to be on comparable scales:

    fused(id) = sum over legs of 1 / (k + rank_in_leg)

Items ranked well by either leg float to the top; items ranked by both beat
items ranked by only one. k=60 is the standard constant from the RRF paper —
it damps the difference between rank 1 and rank 2 so one leg can't dominate.
"""

from __future__ import annotations


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Fuse ordered id lists into one ordered id list (best first).

    `rankings` is a list of legs; each leg is ids ordered best-first.
    Ties break by first appearance so results are deterministic.
    """
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for leg in rankings:
        for rank, id_ in enumerate(leg):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + rank + 1)
            if id_ not in first_seen:
                first_seen[id_] = order
                order += 1
    return sorted(scores, key=lambda i: (-scores[i], first_seen[i]))
