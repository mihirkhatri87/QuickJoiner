"""Scrape synthesis: turn a crawl's pages into one markdown report with mermaid
diagrams — the artifact behind the web UI's /scrape command.

Two layers, so the feature degrades instead of failing:
- LLM synthesis (when a provider is available): an overview + key findings with
  [page title] citations + 1-3 mermaid diagrams capturing something real in the
  content (flows, relationships, architecture).
- Deterministic layer (always): a page digest and a "Site structure" mermaid
  built from the crawled URL paths — works keyless and grounds the LLM output.

The report is saved to <workspace>/scrapes/ and returned to the UI, where the
user can inspect it in the artifact modal and explicitly /learn it into memory
(scraped pages are NOT auto-ingested; learning is the user's call).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from quickjoiner.connectors.base import Document

MAX_PAGE_CHARS = 1500  # per-page excerpt in the LLM prompt
MAX_TOTAL_CHARS = 30_000  # prompt budget across all pages
MAX_MAP_NODES = 40  # site-structure diagram stays readable

SYNTHESIS_SYSTEM = """\
You turn a website crawl into an onboarding-quality markdown report. You are given
the extracted text of every crawled page. STRICT RULES: use ONLY the provided pages —
never invent facts. Cite claims with the page title in [brackets].

Structure the report as:
## Overview — what this site/product/org is about (3-6 sentences).
## Key findings — the most useful facts, grouped by theme, each cited with [page title].
## Notable pages — bullets: [page title] — one line on why it matters.
## Diagrams — 1 to 3 mermaid diagrams that visualize something REAL from the content:
a process/flow, an architecture, product/team/concept relationships, a lifecycle.
Each under a short '### ' heading. Use ```mermaid fences with valid Mermaid syntax
(prefer 'graph TD' or 'graph LR'; short node labels; wrap labels containing special
characters in double quotes). Do not draw a site map — that is generated separately.\
"""


def _slug(url: str) -> str:
    host = urlparse(url).netloc or "site"
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "site"


def _mermaid_label(text: str) -> str:
    text = text.replace('"', "'")
    return text[:28] + "…" if len(text) > 29 else text


def site_map_mermaid(uris: list[str], max_nodes: int = MAX_MAP_NODES) -> str:
    """Deterministic site-structure diagram from crawled URL paths."""
    lines = ["graph TD"]
    ids: dict[tuple[str, ...], str] = {}

    def node(key: tuple[str, ...], label: str) -> str:
        if key not in ids:
            ids[key] = f"n{len(ids)}"
            lines.append(f'    {ids[key]}["{_mermaid_label(label)}"]')
        return ids[key]

    for uri in sorted(uris):
        parsed = urlparse(uri)
        if not parsed.netloc:
            continue
        parent = node((parsed.netloc,), parsed.netloc)
        segments = [s for s in parsed.path.split("/") if s]
        key = (parsed.netloc,)
        for segment in segments:
            key = key + (segment,)
            known = key in ids
            child = node(key, segment)
            if not known:
                lines.append(f"    {parent} --> {child}")
            parent = child
            if len(ids) >= max_nodes:
                break
        if len(ids) >= max_nodes:
            break
    return "\n".join(lines)


def _digest(pages: list[Document]) -> str:
    """Keyless fallback: what was found, page by page."""
    lines = [
        "## Overview",
        "",
        f"Crawled {len(pages)} readable pages. LLM synthesis was unavailable for this run, so "
        "this digest lists what was found; with a provider configured, /scrape produces a "
        "full analysis with diagrams.",
        "",
        "## Pages",
        "",
    ]
    for doc in pages:
        first = next((ln for ln in doc.text.splitlines() if len(ln.strip()) > 40), "")
        lines.append(f"- [{doc.title}]({doc.uri}) — {first.strip()[:140]}")
    return "\n".join(lines)


def _synthesis_prompt(url: str, pages: list[Document]) -> str:
    blocks, used = [], 0
    for doc in pages:
        excerpt = doc.text[:MAX_PAGE_CHARS]
        used += len(excerpt)
        blocks.append(f"[{doc.title}] ({doc.uri})\n{excerpt}")
        if used >= MAX_TOTAL_CHARS:
            break
    return (
        f"Crawl root: {url}\nPages ({len(blocks)} of {len(pages)} shown):\n\n"
        + "\n\n---\n\n".join(blocks)
    )


def build_report(
    url: str,
    pages: list[Document],
    provider=None,
    on_stream=None,
) -> str:
    """Compose the full report markdown. `provider=None` (or a provider failure)
    degrades to the deterministic digest — the site map is always included."""
    body = ""
    if provider is not None:
        try:
            result = provider.chat(
                [{"role": "user", "content": _synthesis_prompt(url, pages)}],
                system=SYNTHESIS_SYSTEM,
                on_stream=on_stream,
            )
            body = (result.text or "").strip()
        except Exception:
            body = ""
    if not body:
        body = _digest(pages)

    today = datetime.now(timezone.utc).date().isoformat()
    return (
        f"# Scrape report: {urlparse(url).netloc or url}\n\n"
        f"_Crawled {len(pages)} pages from {url} on {today}._\n\n"
        f"{body}\n\n"
        f"## Site structure\n\n"
        f"```mermaid\n{site_map_mermaid([p.uri for p in pages])}\n```\n"
    )


def save_report(workspace: Path, url: str, markdown: str) -> Path:
    scrapes = workspace / "scrapes"
    scrapes.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = scrapes / f"{_slug(url)}-{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
