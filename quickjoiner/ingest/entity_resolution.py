"""Entity resolution for the knowledge graph: before a newly-observed entity is
persisted as a brand new node, decide whether it's actually a differently-named
mention of something already in the graph (e.g. a Confluence ADR's "Webroot
Connector" vs. the repo's own dependency-map name "AppRiver.Connector.Web").

Modeled on Graphiti's node-dedup approach (embedding-recall candidates + an LLM
adjudicating the match) but scoped down and kept entirely in the existing SQL
catalog — no external graph database, no new storage engine, consistent with
docs/KNOWLEDGE_GRAPH.md's non-goals. Cheapest-safe-thing-first:

1. Exact id already known -> no-op (the common case: same entity re-asserted
   by another document).
2. Embedding-similarity search among existing entities of the *same type*;
   below a floor, it's genuinely new and no LLM call is spent.
3. Above the floor, an optional LLM adjudicates ("is this the same thing as
   any of these candidates?"). Keyless workspaces fall back to a stricter
   cosine-only cutoff so the feature still degrades gracefully without a
   provider, at the cost of being more conservative about merging.

A merge never renames or deletes the earlier (canonical) entity — it only adds
an alias, so existing edges/citations keep pointing at the same id.

Deliberately scoped to a fixed vocabulary of entity types: symbols, modules,
tickets, and people are excluded (tickets are already exact-key matched;
symbols/modules are too fine-grained and numerous for per-entity LLM calls;
merging two different *people* on an LLM's say-so is a higher-stakes mistake
than merging two services, so it's left to exact/alias matching only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Protocol

RESOLVABLE_TYPES = frozenset({"repo", "project", "package", "service", "team", "environment"})

_CANDIDATE_FLOOR = 0.55        # below this cosine, don't even spend an LLM call
_KEYLESS_MERGE_CUTOFF = 0.90   # no LLM available: only auto-merge near-exact name matches
_MAX_CANDIDATES = 5

# (type, name, candidate_names, new_entity_context, candidate_contexts) -> the matching
# candidate name, or None for "not a match". Contexts are short evidence descriptions
# ('mentioned in "Jan 6, 2026" (doc)') — bare name strings alone gave the LLM nothing
# to judge with, so it safely defaulted to NONE and real cross-source merges (the
# "Webroot Connector" == AppRiver.Connector.Web case) never fired (plan 06 §1.D).
Adjudicator = Callable[[str, str, list[str], str, list[str]], "str | None"]


class _EntityCatalog(Protocol):
    def resolve_entity(self, text: str) -> dict | None: ...
    def entities_by_type(self, type_: str) -> list[dict]: ...
    def add_entity_alias(self, alias: str, entity_id: str) -> None: ...
    def entity_evidence(self, entity_id: str, limit: int = 3) -> list[dict]: ...


class _Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class EntityResolver:
    """Per-run resolver: caches name embeddings for entities it has already
    looked up or newly minted this run, so a long ingest batch doesn't re-embed
    the same existing entities on every call."""

    catalog: _EntityCatalog
    embedder: _Embedder
    adjudicate: Adjudicator | None = None
    candidate_floor: float = _CANDIDATE_FLOOR
    keyless_cutoff: float = _KEYLESS_MERGE_CUTOFF
    max_candidates: int = _MAX_CANDIDATES
    resolvable_types: frozenset[str] = RESOLVABLE_TYPES

    _cache: dict[str, list[tuple[str, str, list[float]]]] = field(default_factory=dict, init=False)

    def resolve(self, entity_id: str, name: str, type_: str,
                context: str = "") -> tuple[str, bool]:
        """(canonical_id, merged). merged=False: create `entity_id` as normal.
        merged=True: an alias was added to the returned existing id instead —
        the caller must NOT upsert a new entity row for the original id.
        `context` describes where the new name was observed (evidence doc
        title/kind) so the adjudicator judges from evidence, not bare strings."""
        if type_ not in self.resolvable_types:
            return entity_id, False
        if self.catalog.resolve_entity(entity_id) is not None:
            return entity_id, False  # already the canonical id itself

        bucket = self._bucket(type_)
        if not bucket:
            self._remember(type_, entity_id, name)
            return entity_id, False

        vec = self.embedder.embed([name])[0]
        scored = sorted(
            ((cid, cname, _cosine(vec, cvec)) for cid, cname, cvec in bucket),
            key=lambda t: t[2],
            reverse=True,
        )[: self.max_candidates]
        candidates = [(cid, cname) for cid, cname, score in scored if score >= self.candidate_floor]
        if not candidates:
            self._remember(type_, entity_id, name, vec)
            return entity_id, False

        if self.adjudicate is not None:
            candidate_contexts = self._candidate_contexts([cid for cid, _ in candidates])
            try:
                match = self.adjudicate(type_, name, [cname for _, cname in candidates],
                                        context, candidate_contexts)
            except Exception:
                match = None
            if match:
                hit = next((cid for cid, cname in candidates if cname.lower() == match.strip().lower()), None)
                if hit:
                    self.catalog.add_entity_alias(name, hit)
                    return hit, True
            self._remember(type_, entity_id, name, vec)
            return entity_id, False

        # Keyless: no LLM to ask, so only merge on a much stricter cosine match.
        best_id, _best_name, best_score = scored[0]
        if best_score >= self.keyless_cutoff:
            self.catalog.add_entity_alias(name, best_id)
            return best_id, True
        self._remember(type_, entity_id, name, vec)
        return entity_id, False

    def _candidate_contexts(self, candidate_ids: list[str]) -> list[str]:
        """Evidence summaries ('"Connector/AGENTS.md" (doc); …') per candidate, from
        the docs behind the edges touching each one. Only runs on the (rare)
        adjudication path, bounded by max_candidates * limit rows. Older test
        doubles without entity_evidence degrade to empty contexts, not a crash."""
        fetch = getattr(self.catalog, "entity_evidence", None)
        if fetch is None:
            return ["" for _ in candidate_ids]
        out = []
        for cid in candidate_ids:
            try:
                rows = fetch(cid, 3)
            except Exception:
                rows = []
            out.append("; ".join(f'"{r["title"]}" ({r["kind"]})' for r in rows if r.get("title")))
        return out

    def _bucket(self, type_: str) -> list[tuple[str, str, list[float]]]:
        if type_ not in self._cache:
            rows = self.catalog.entities_by_type(type_)
            names = [r["name"] for r in rows]
            vectors = self.embedder.embed(names) if names else []
            self._cache[type_] = list(zip((r["id"] for r in rows), names, vectors))
        return self._cache[type_]

    def _remember(self, type_: str, entity_id: str, name: str, vec: list[float] | None = None) -> None:
        if vec is None:
            vec = self.embedder.embed([name])[0]
        self._cache.setdefault(type_, []).append((entity_id, name, vec))


ADJUDICATE_SYSTEM = """You determine whether a newly mentioned entity is the \
same real-world thing as one of a short list of entities already known to the \
system. Judge from the evidence context provided, as a person familiar with the \
organization would — an informal nickname and a formal repo/package name are \
often the same thing when their evidence describes the same system. Reply with \
ONLY the exact candidate name if it is the same thing, or NONE if it is not \
(or you are unsure). Never guess — reply NONE by default."""


def make_llm_adjudicator(provider) -> Adjudicator:
    """An Adjudicator backed by a chat provider. A malformed or "NONE" reply
    (or any error) means no match — the caller creates a new entity. Empty
    contexts degrade to the plain bare-names prompt lines."""

    def adjudicate(type_: str, name: str, candidate_names: list[str],
                   context: str = "", candidate_contexts: list[str] | None = None) -> str | None:
        contexts = candidate_contexts or []
        lines = []
        for i, c in enumerate(candidate_names):
            evidence = contexts[i] if i < len(contexts) else ""
            lines.append(f'- "{c}" — evidence: {evidence}' if evidence else f"- {c}")
        candidates = "\n".join(lines)
        new_ctx = f"\n  context: {context}" if context else ""
        prompt = (
            f'New entity (type: {type_}): "{name}"{new_ctx}\n\nCandidates:\n{candidates}\n\n'
            f"Which candidate (if any) refers to the same real-world {type_}? "
            "Judge from the evidence context as a person familiar with the org would. "
            "Reply with just that candidate's exact name, or NONE."
        )
        result = provider.chat([{"role": "user", "content": prompt}], system=ADJUDICATE_SYSTEM)
        reply = (result.text or "").strip()
        return None if not reply or reply.upper() == "NONE" else reply

    return adjudicate
