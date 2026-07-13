"""Knowledge-graph triples: the controlled vocabulary, strict parsing, the
triple→graph-metadata conversion, and an LLM relationship extractor for ingested
documents.

Two producers share this module: conversation distillation (sessions.py) and
document ingestion (pipeline.py). LLM output is NEVER trusted into the graph
without shape validation against TRIPLE_TYPES/TRIPLE_RELS — off-vocabulary lines
are dropped, never coerced, and the count is capped. Every resulting edge carries
its evidence document, so graph answers stay as citable as search answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TRIPLE_TYPES = {"repo", "package", "project", "service", "environment", "ticket",
                "person", "team"}
TRIPLE_RELS = {"depends_on", "provides", "references", "part_of", "deploys",
               "owns", "works_on"}
_TRIPLE_LINE = re.compile(
    r"^([a-z_]+)\s*:\s*(.{1,80}?)\s*\|\s*([a-z_]+)\s*\|\s*([a-z_]+)\s*:\s*(.{1,80}?)$"
)
_MAX_TRIPLES = 20


@dataclass(frozen=True)
class Triple:
    src_type: str
    src_name: str
    rel: str
    dst_type: str
    dst_name: str


def parse_triples(lines: list[str]) -> list[Triple]:
    """Validate proposed relationship lines; anything off-vocabulary is dropped."""
    out: list[Triple] = []
    for line in lines:
        m = _TRIPLE_LINE.match(line.strip())
        if not m:
            continue
        src_type, src_name, rel, dst_type, dst_name = m.groups()
        if src_type in TRIPLE_TYPES and dst_type in TRIPLE_TYPES and rel in TRIPLE_RELS:
            out.append(Triple(src_type, src_name, rel, dst_type, dst_name))
            if len(out) >= _MAX_TRIPLES:
                break
    return out


def triples_to_graph(triples: list[Triple], detail: str) -> dict:
    """Convert validated triples into the Document.metadata["graph"] shape
    (entities / aliases / edges) the ingest pipeline persists. Entity ids and org
    aliases reuse connectors/deps so triple entities merge with the same repos,
    packages and services the deterministic extractors produce."""
    from quickjoiner.connectors.deps import aliases, entity_id

    entities: dict[str, tuple[str, str, str]] = {}
    alias_rows: set[tuple[str, str]] = set()
    edges: list[tuple[str, str, str, str]] = []
    for t in triples:
        src_id = entity_id(t.src_type, t.src_name)
        dst_id = entity_id(t.dst_type, t.dst_name)
        entities.setdefault(src_id, (src_id, t.src_name, t.src_type))
        entities.setdefault(dst_id, (dst_id, t.dst_name, t.dst_type))
        for name, eid in ((t.src_name, src_id), (t.dst_name, dst_id)):
            for form in aliases(name, drop_prefix="." in name):
                alias_rows.add((form, eid))
        edges.append((src_id, t.rel, dst_id, detail))
    return {"entities": sorted(entities.values()), "aliases": sorted(alias_rows), "edges": edges}


DOC_TRIPLE_SYSTEM = """You extract a knowledge graph of relationships from an \
organization document. Output ONLY relationship lines, one per line, in exactly \
this format:

<type>: <name> | <relation> | <type>: <name>

Allowed types: repo, package, project, service, environment, ticket, person, team.
Allowed relations: depends_on, provides, references, part_of, deploys, owns, works_on.
Record only concrete links the document actually states between two NAMED things
(e.g. "service: checkout | depends_on | service: payments"). Use names exactly as
written. Never invent relationships. If there are none, output nothing."""


def extract_doc_triples(provider, text: str, title: str, max_chars: int = 6000) -> list[Triple]:
    """One LLM call proposing relationship triples for a document; the result is
    validated by parse_triples (so a hallucinated/off-vocabulary line is dropped).
    Returns [] with no provider or on any error — extraction never breaks ingest."""
    if provider is None:
        return []
    snippet = text[:max_chars]
    try:
        result = provider.chat(
            [{"role": "user", "content": f"Document title: {title}\n\n{snippet}"}],
            system=DOC_TRIPLE_SYSTEM,
        )
    except Exception:
        return []
    return parse_triples((result.text or "").splitlines())
