"""Onboarding briefs: markdown reports generated ONLY from learned memory.

Each brief type has seed retrieval queries and writing instructions. Retrieved
chunks (with citations) are the entire context — the same grounding contract as
chat. Generated briefs are saved to <workspace>/briefs/ and ingested back into
memory so the agent can answer questions about its own briefs later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from quickjoiner.connectors.base import Document
from quickjoiner.memory.store import SearchHit

MAX_CHUNKS = 40
MAX_CHUNK_CHARS = 1500

BRIEF_SYSTEM = """\
You write onboarding briefs for a principal software developer who just joined this \
organization. You are given retrieval results from a knowledge base of everything \
learned about the org so far. STRICT RULES: use ONLY the provided context — never \
invent org-specific facts. Cite each claim with [uri] from the context block it came \
from. Where the context is thin, say so explicitly in a final "Not learned yet" \
section listing what should be connected or synced to fill the gap. Write tight, \
skimmable markdown with headings and bullet points.\
"""


@dataclass(frozen=True)
class BriefSpec:
    slug: str
    title: str
    queries: tuple[str, ...]
    instructions: str


BRIEFS: dict[str, BriefSpec] = {
    spec.slug: spec
    for spec in (
        BriefSpec(
            slug="architecture",
            title="Architecture map",
            queries=(
                "architecture overview services components systems",
                "repository structure main modules code layout",
                "deployment pipeline environments release process",
                "API integration dependencies between services",
                "code owners teams responsibilities",
            ),
            instructions=(
                "Produce an architecture map: the systems/services and what each does, how "
                "they talk to each other, where the code lives, how changes reach production, "
                "and who owns what. End with the biggest open questions a new principal "
                "engineer should ask."
            ),
        ),
        BriefSpec(
            slug="week1",
            title="Week-1 brief",
            queries=(
                "onboarding getting started setup development environment",
                "tools platforms access accounts",
                "team processes rituals meetings conventions",
                "glossary terminology acronyms project names",
                "people roles contacts who owns which area",
            ),
            instructions=(
                "Produce a week-1 survival brief: tools inventory (what the org uses and for "
                "what), processes and rituals, a glossary of org-specific terms, key people/"
                "owners, and a suggested day-by-day focus for the first week."
            ),
        ),
        BriefSpec(
            slug="roadmap",
            title="Roadmap digest",
            queries=(
                "roadmap initiative epic quarter planning",
                "priorities upcoming milestones deadlines",
                "in progress current sprint active work",
                "recently completed shipped released",
            ),
            instructions=(
                "Produce a roadmap digest: major initiatives/epics and their status, what is "
                "actively in flight, what is coming next, and any deadlines or milestones. "
                "Group by theme or team where the context supports it."
            ),
        ),
        BriefSpec(
            slug="quick-wins",
            title="Quick-wins report",
            queries=(
                "failing flaky pipeline build broken CI",
                "TODO FIXME technical debt workaround hack",
                "long open stale pull request merge request",
                "recurring alert monitor noise incident",
                "slow build performance problem timeout",
                "missing owner unowned undocumented",
            ),
            instructions=(
                "Produce a quick-wins report: concrete, low-risk improvements a new principal "
                "engineer could ship early to establish credibility — flaky/failing pipelines, "
                "stale PRs, debt hotspots, alert noise, slow builds, ownership gaps. For each: "
                "the evidence (cited), why it matters, and a suggested first step. Rank by "
                "impact-to-effort."
            ),
        ),
    )
}

NOT_LEARNED = (
    "I haven't learned enough to write this brief yet. Connect and sync sources first "
    "(e.g. qj connect git/github/jira/confluence ... then qj sync), or teach me facts "
    "with qj learn."
)


def collect_context(store, retrieval, queries: tuple[str, ...]) -> list[SearchHit]:
    """Union of per-query retrievals, deduped, best-score first, capped."""
    best: dict[tuple[str, str], SearchHit] = {}
    for query in queries:
        for hit in store.search(query, top_k=retrieval.top_k, min_score=retrieval.min_score):
            key = (hit.doc_id, hit.text[:80])
            if key not in best or hit.score > best[key].score:
                best[key] = hit
    ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
    return ranked[:MAX_CHUNKS]


def build_prompt(spec: BriefSpec, hits: list[SearchHit]) -> str:
    blocks = [
        f"[{h.uri}] (source: {h.source_id}, kind: {h.kind}, score: {h.score:.2f})\n"
        f"{h.text[:MAX_CHUNK_CHARS]}"
        for h in hits
    ]
    return (
        f"Write the brief: {spec.title}.\n{spec.instructions}\n\n"
        f"Learned context ({len(blocks)} chunks):\n\n" + "\n\n---\n\n".join(blocks)
    )


def generate_brief(
    ctx,
    brief_type: str,
    provider=None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> tuple[str, Path | None]:
    """Generate one brief. Returns (markdown, saved_path); path is None if not learned enough.

    The LLM provider is only constructed after the retrieval check, so "not learned
    yet" works even without an API key configured.
    """
    spec = BRIEFS.get(brief_type)
    if spec is None:
        raise ValueError(f"Unknown brief type {brief_type!r}. Types: {', '.join(BRIEFS)}")

    hits = collect_context(ctx.store, ctx.config.retrieval, spec.queries)
    if not hits:
        return NOT_LEARNED, None

    if provider is None:
        provider = ctx.build_provider(provider_override, model_override)

    # The date was computed AFTER this call and used only for the header and filename, so
    # the model never saw it — while two of the brief specs are explicitly temporal
    # ("a day-by-day focus for the first week", "what is in flight, what is coming next,
    # and any deadlines"). Deciding what is upcoming or recently finished with no idea
    # what today is, from undated chunks, is the same silent-wrong-window failure
    # agent/dates.py exists to remove.
    now = datetime.now(timezone.utc)
    dated_system = (
        f"{BRIEF_SYSTEM}\n\nToday is {now.strftime('%A, %Y-%m-%d')} (UTC). Judge what is "
        "recent, in flight, overdue or upcoming against that date, and say when a source "
        "carries no date rather than assuming it is current."
    )
    result = provider.chat(
        [{"role": "user", "content": build_prompt(spec, hits)}], system=dated_system
    )
    today = now.date().isoformat()
    markdown = (
        f"# {spec.title}\n\n_Generated {today} from {len(hits)} learned chunks; "
        f"claims are cited, gaps are listed at the end._\n\n{result.text.strip()}\n"
    )

    briefs_dir = ctx.workspace / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / f"{spec.slug}-{today}.md"
    path.write_text(markdown, encoding="utf-8")

    ctx.catalog.upsert_source("briefs:generated", "Generated briefs", "briefs", {})
    ctx.pipeline.ingest(
        [
            Document(
                uri=f"brief://{spec.slug}/{today}",
                title=spec.title,
                text=markdown,
                kind="note",
            )
        ],
        "briefs:generated",
    )
    return markdown, path
