"""Question autocomplete — help a user finish a question as they type.

Autocomplete must feel instant and work offline, so this is **keyless and
deterministic**: no LLM call per keystroke (the broker is slow/flaky and a
per-keystroke round-trip would be unusable). Instead we complete from what the
workspace has actually learned:

  1. Entity-templated questions — the knowledge graph holds the org's real nouns
     (services, repos, environments, packages, people, tickets, teams). We extract
     the entity the user is naming and offer the natural questions about it
     ("Where is <service> deployed?", "What does <repo> depend on?").
  2. Past questions — real phrasings people already tried (the gaps backlog),
     prefix/substring matched.
  3. Source-aware starters — high-value openers seeded by which connectors are
     configured (Octopus present ⇒ deployment questions, git ⇒ repo questions …).

`rank_suggestions` and the pure helpers are unit-tested without a catalog; the
`QuestionSuggester` glues them to the live graph.
"""

from __future__ import annotations

import re
from typing import Any

# Leading question/filler words stripped when locating the *entity* the user is
# naming — everything here is noise for entity lookup ("where is <X> deployed").
_STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "me", "i", "my", "our",
    "what", "whats", "where", "wheres", "who", "whos", "which", "when", "why", "how", "is",
    "are", "was", "were", "be", "been", "does", "do", "did", "has", "have", "had", "can",
    "about", "tell", "show", "list", "give", "get", "find", "explain",
    "deployed", "deploy", "deploys", "depend", "depends", "dependency", "dependencies",
    "run", "runs", "running", "use", "uses", "using", "used", "own", "owns", "owned",
    "work", "works", "working", "implemented", "define", "defined", "defines", "import",
    "imports", "provide", "provides", "provided", "environment", "environments", "service",
    "services", "repo", "repos", "repository", "team", "teams", "package", "packages",
}

# Per-entity-type question templates. `{name}` is filled with the entity's real name.
TEMPLATES: dict[str, list[str]] = {
    "service": [
        "Where is {name} deployed?",
        "What does {name} depend on?",
        "Who owns {name}?",
        "What is {name}?",
    ],
    "project": [
        "Where is {name} deployed?",
        "What does {name} depend on?",
        "What is {name}?",
    ],
    "environment": [
        "What is deployed to {name}?",
        "Which services run in {name}?",
    ],
    "repo": [
        "What does the {name} repo do?",
        "What are the dependencies of {name}?",
        "Who works on {name}?",
    ],
    "package": [
        "Which services depend on {name}?",
        "What provides {name}?",
    ],
    "person": [
        "What does {name} work on?",
        "Which team is {name} on?",
    ],
    "ticket": [
        "What is {name} about?",
        "Was {name} implemented and deployed?",
    ],
    "team": [
        "What does the {name} team own?",
        "Who is on the {name} team?",
    ],
    "module": [
        "What imports {name}?",
        "Which code uses {name}?",
    ],
    "symbol": [
        "Where is {name} defined?",
        "What uses {name}?",
    ],
}
DEFAULT_TEMPLATES = ["What is {name}?", "Tell me about {name}."]

# Boilerplate stripped from a *template* when computing intent alignment. Unlike
# _STOPWORDS (which strips domain verbs so they don't pollute entity lookup), this
# keeps the verbs that carry intent — "deploy", "depend", "own", "import" — so a
# user typing "dep" can align with the "depend" template.
_TEMPLATE_BOILERPLATE = {
    "a", "an", "the", "of", "to", "in", "on", "for", "is", "are", "be", "do", "does",
    "did", "what", "who", "which", "where", "when", "why", "how", "me", "about",
}

# Generic openers, always available. The per-connector-type openers live in the
# connector catalog (`connectors/specs.py` -> FORM_SPECS[type]["suggests"]), so a new
# connector contributes its own starters without touching this module.
_BASE_STARTERS = [
    "What can you help me with?",
    "Give me a quick onboarding overview.",
    "What are the main systems in this org?",
]

# Priority bands (lower sorts first).
_P_HISTORY_PREFIX = 0
_P_STARTER_PREFIX = 1
_P_ENTITY = 2
_P_HISTORY_SUBSTRING = 3
_P_STARTER_OTHER = 4

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-_]*")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def extract_needle_tokens(prefix: str, max_tokens: int = 3) -> list[str]:
    """The meaningful (non-stopword) tokens naming the entity, last ones first.

    "where is the management console deployed" -> ["console", "management"]
    Kept short (max_tokens) because the entity reference is near the end of a
    half-typed question; length>=3 drops noise like "is"/"of".
    """
    tokens = [t.lower() for t in _WORD.findall(prefix)]
    kept = [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]
    # Prefer the trailing tokens (the thing being named), de-duplicated.
    ordered: list[str] = []
    for t in reversed(kept):
        if t not in ordered:
            ordered.append(t)
        if len(ordered) >= max_tokens:
            break
    return ordered


def fill_templates(name: str, entity_type: str, prefix: str = "") -> list[tuple[str, float]]:
    """Templated questions for one entity, each with an intent-alignment score.

    A template whose verb ("deploy", "depend", …) already appears in what the user
    typed scores higher, so "where is X dep|" surfaces the deploy/depend question first.
    """
    templates = TEMPLATES.get(entity_type, DEFAULT_TEMPLATES)
    plow = prefix.lower()
    # Prefix tokens the user has typed (they may be half-words mid-type, e.g. "dep").
    ptoks = [t for t in _WORD.findall(plow) if len(t) >= 3]

    def _aligned(word: str) -> bool:
        # a template intent word aligns if the user typed it, or a typed token is a
        # prefix of it (or vice-versa) — so "dep" still boosts "depend".
        return word in plow or any(word.startswith(t) or t.startswith(word) for t in ptoks)

    out: list[tuple[str, float]] = []
    for tmpl in templates:
        text = tmpl.format(name=name)
        skeleton = tmpl.replace("{name}", " ")  # template words minus the entity name
        intent_words = [w for w in _WORD.findall(skeleton.lower()) if w not in _TEMPLATE_BOILERPLATE and len(w) >= 3]
        boost = sum(1 for w in intent_words if _aligned(w))
        out.append((text, float(boost)))
    return out


def rank_suggestions(prefix: str, candidates: list[tuple[str, int, float]], limit: int) -> list[str]:
    """Order + dedupe candidates for display.

    candidates: (text, priority_band, score). Sort by band asc, then score desc,
    then shorter text (snappier completions first). Drop anything equal to the
    prefix itself, and dedupe case-insensitively.
    """
    p = _norm(prefix)
    seen: set[str] = set()
    ranked = sorted(candidates, key=lambda c: (c[1], -c[2], len(c[0])))
    out: list[str] = []
    for text, _band, _score in ranked:
        key = _norm(text)
        if not key or key == p or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


class QuestionSuggester:
    """Live autocomplete backed by the workspace's knowledge graph + history."""

    def __init__(self, catalog: Any):
        self.catalog = catalog

    def suggest(self, prefix: str, limit: int = 6) -> list[str]:
        prefix = (prefix or "").strip()
        p = prefix.lower()
        if not prefix:
            return rank_suggestions("", [(s, _P_STARTER_PREFIX, 0.0) for s in self._starters()], limit)

        candidates: list[tuple[str, int, float]] = []

        # 1) Past real questions.
        for q in self._past_questions():
            ql = q.lower()
            if ql.startswith(p):
                candidates.append((q, _P_HISTORY_PREFIX, float(len(p))))
            elif _contains_all_tokens(p, ql):
                candidates.append((q, _P_HISTORY_SUBSTRING, float(len(p))))

        # 2) Starters (source-aware) — prefix match ranks high, others trail.
        for s in self._starters():
            sl = s.lower()
            if sl.startswith(p):
                candidates.append((s, _P_STARTER_PREFIX, float(len(p))))
            elif len(prefix) < 3:
                candidates.append((s, _P_STARTER_OTHER, 0.0))

        # 3) Entity-templated questions for whatever noun the user is naming.
        for text, score in self._entity_templated(prefix):
            candidates.append((text, _P_ENTITY, score))

        return rank_suggestions(prefix, candidates, limit)

    # -- candidate sources ----------------------------------------------------

    def _entity_templated(self, prefix: str) -> list[tuple[str, float]]:
        needles = extract_needle_tokens(prefix)
        if not needles:
            return []
        # Merge per-token entity hits, ranking an entity by (#needles it matched,
        # graph degree) so the most-referenced, best-matching nouns win.
        merged: dict[str, dict] = {}
        for tok in needles:
            try:
                hits = self.catalog.search_entities(tok, limit=6)
            except Exception:
                hits = []
            for e in hits:
                row = merged.setdefault(e["id"], {"e": e, "matches": 0})
                row["matches"] += 1
        top = sorted(
            merged.values(), key=lambda r: (-r["matches"], -int(r["e"].get("degree", 0) or 0))
        )[:4]
        out: list[tuple[str, float]] = []
        for r in top:
            e = r["e"]
            degree = int(e.get("degree", 0) or 0)
            for text, intent in fill_templates(e["name"], e.get("type", ""), prefix):
                # score blends match count, intent alignment, and connectedness
                out.append((text, r["matches"] * 2.0 + intent * 3.0 + min(degree, 20) * 0.05))
        return out

    def _past_questions(self) -> list[str]:
        try:
            rows = self.catalog.list_gaps("open")
        except Exception:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for r in rows:
            q = (r.get("query") or "").strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
        return out

    def _starters(self) -> list[str]:
        """Openers seeded by which connector types are configured — pulled from the
        connector catalog so each connector ships its own, plus the generic base set.
        Configured-source starters come first (they're the most relevant here)."""
        from quickjoiner.connectors.specs import FORM_SPECS

        try:
            types = {s.type for s in self.catalog.list_source_configs()}
        except Exception:
            types = set()
        starters: list[str] = []
        for t in sorted(types):
            starters += FORM_SPECS.get(t, {}).get("suggests", [])
        starters += _BASE_STARTERS
        # de-dupe, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for s in starters:
            if s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out


def _contains_all_tokens(needle: str, haystack: str) -> bool:
    toks = [t for t in _WORD.findall(needle) if len(t) >= 2]
    return bool(toks) and all(t in haystack for t in toks)
