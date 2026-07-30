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
                "person", "team",
                # message channels (Service Bus topics/queues, event streams) and data
                # stores (databases/caches/blob containers): the runtime-coupling nouns
                # that package manifests can never see — two services wired only through
                # a topic or a shared table have no compile-time relationship for
                # deps.py to find, so these edges are the event-driven org's missing map.
                "topic", "datastore"}
TRIPLE_RELS = {"depends_on", "provides", "references", "part_of", "deploys",
               "owns", "works_on",
               # pub/sub + storage verbs (2026-07-17, user request): service
               # publishes_to/subscribes_to topic; service stores_in datastore.
               "publishes_to", "subscribes_to", "stores_in"}
_TRIPLE_LINE = re.compile(
    r"^([a-z_]+)\s*:\s*(.{1,80}?)\s*\|\s*([a-z_]+)\s*\|\s*([a-z_]+)\s*:\s*(.{1,80}?)$"
)
_MAX_TRIPLES = 20

# -- relation signatures (ontology-lite domain/range validation, 2026-07-30) ---------
# The type check and the relation check were independent, so a line whose three words
# were each individually in-vocabulary became a real edge even when the combination is
# a category error: "environment: prod | owns | person: bob" validated. Each relation
# now declares which entity types may stand on its LEFT (domain) and RIGHT (range);
# a triple outside its signature is dropped exactly like an off-vocabulary word is —
# never coerced, never "fixed up".
#
# Deliberately permissive. This exists to reject impossible shapes, not to referee
# debatable-but-real statements: a signature that is too tight silently deletes true
# relationships, which is the worse failure for a system whose whole claim is that it
# only says what it learned. When a statement is arguable, it is admitted.
#
# **Calibrated against a real corpus, not taste (2026-07-30).** The first cut of this
# table was drawn around an idealised ontology and measured against the live workspace's
# 109k-edge graph: it rejected 4,201 of 29,461 in-vocabulary edges (13.6%), and 43% of
# those rejections were perfectly sensible statements — `person owns ticket`,
# `person works_on team`, `team provides service`, `service part_of environment` — that
# real org prose makes constantly. The lesson the data taught:
#
#   Almost every genuine error is a **domain** error — the subject cannot perform the
#   relation. Inverted lines dominate (`ticket works_on person` ×548), followed by
#   software "working on" things and places/channels acting as agents. **Ranges** only
#   earn their keep for the verbs whose object type is definitional: you publish to a
#   topic, you store in a datastore, you deploy to somewhere. Everywhere else a tight
#   range costs true edges and buys nothing.
#
# So: constrain subjects tightly, objects loosely, and exclude `person` as an object
# generally (people own and work on things, not the reverse). Re-measured: 2,441
# rejections (8.3%), every high-volume one a real category error.
_ALL = frozenset(TRIPLE_TYPES)
_SOFTWARE = frozenset({"repo", "package", "project", "service"})
_ACTOR = frozenset({"person", "team"})
_PLACE = frozenset({"environment"})
# Nothing is part of / owned by / provided to a *person* — that is the inverted shape.
_NOT_PERSON = _ALL - {"person"}

RELATION_SIGNATURES: dict[str, tuple[frozenset, frozenset]] = {
    "depends_on": (_SOFTWARE, _NOT_PERSON),
    # a team or a host can provide a service, not just other software
    "provides": (_SOFTWARE | _ACTOR | _PLACE, _NOT_PERSON),
    # ticket part_of ticket is the Epic/Feature hierarchy the ADO + Jira connectors emit;
    # the domain is open because almost anything can belong to something
    "part_of": (_ALL, _NOT_PERSON),
    "deploys": (_SOFTWARE | _ACTOR, _SOFTWARE | _PLACE),
    "owns": (_ACTOR | _SOFTWARE, _NOT_PERSON),
    # the highest-value constraint in the table: ONLY people and teams work on things.
    # 1,400+ inverted `ticket works_on <thing>` lines in the live graph came from here.
    "works_on": (_ACTOR, _NOT_PERSON),
    "publishes_to": (_SOFTWARE | _ACTOR, frozenset({"topic"})),
    "subscribes_to": (_SOFTWARE | _ACTOR, frozenset({"topic"})),
    "stores_in": (_SOFTWARE | _ACTOR | _PLACE, frozenset({"datastore"})),
}
# `references` is the loose "mentioned alongside" verb (the ticket-key extractor's edge,
# among others) — it asserts co-occurrence, not a typed relationship, so constraining it
# would only invent violations. Kept explicit so the lockstep test can tell "deliberately
# unrestricted" from "someone added a relation and forgot its signature".
UNSIGNED_RELS = frozenset({"references"})


def signature_allows(src_type: str, rel: str, dst_type: str) -> bool:
    """Whether `src_type --rel--> dst_type` is a shape this relation can hold (pure).

    Unknown/unsigned relations are permitted — the caller has already checked the
    relation is in TRIPLE_RELS, and this table is a second, narrower gate, not the
    vocabulary itself.
    """
    sig = RELATION_SIGNATURES.get(rel)
    if sig is None:
        return True
    domain, range_ = sig
    return src_type in domain and dst_type in range_


@dataclass(frozen=True)
class Triple:
    src_type: str
    src_name: str
    rel: str
    dst_type: str
    dst_name: str


def parse_triples(lines: list[str]) -> list[Triple]:
    """Validate proposed relationship lines; anything off-vocabulary or off-signature
    is dropped (see RELATION_SIGNATURES — the words being individually legal is not
    enough, the combination must be a shape the relation can actually hold)."""
    out: list[Triple] = []
    for line in lines:
        m = _TRIPLE_LINE.match(line.strip())
        if not m:
            continue
        src_type, src_name, rel, dst_type, dst_name = m.groups()
        if (
            src_type in TRIPLE_TYPES
            and dst_type in TRIPLE_TYPES
            and rel in TRIPLE_RELS
            and signature_allows(src_type, rel, dst_type)
        ):
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


# Both extraction prompts (documents here, conversations in sessions.py) enumerate the
# vocabulary from these strings so the sets above are the single source of truth —
# extending TRIPLE_TYPES/TRIPLE_RELS updates every prompt automatically.
ALLOWED_TYPES_LINE = "Allowed types: " + ", ".join(sorted(TRIPLE_TYPES)) + "."
ALLOWED_RELS_LINE = "Allowed relations: " + ", ".join(sorted(TRIPLE_RELS)) + "."


def _signature_line(rel: str) -> str:
    domain, range_ = RELATION_SIGNATURES[rel]
    return f"  {rel}: {'|'.join(sorted(domain))} -> {'|'.join(sorted(range_))}"


# Rendered from RELATION_SIGNATURES so a new/changed signature reaches both prompts with
# no hand-edit — the same single-source-of-truth discipline as the ALLOWED_* lines. A
# model told the shape up front proposes fewer lines the validator would only discard.
SIGNATURE_LINES = (
    "Each relation only connects certain types (left -> right); a line outside its "
    "signature is discarded:\n"
    + "\n".join(_signature_line(r) for r in sorted(RELATION_SIGNATURES))
    + "".join(f"\n  {r}: any -> any" for r in sorted(UNSIGNED_RELS))
)

DOC_TRIPLE_SYSTEM = f"""You extract a knowledge graph of relationships from an \
organization document. Output ONLY relationship lines, one per line, in exactly \
this format:

<type>: <name> | <relation> | <type>: <name>

{ALLOWED_TYPES_LINE}
{ALLOWED_RELS_LINE}
{SIGNATURE_LINES}
"topic" covers message topics, queues and event streams; "datastore" covers \
databases, caches and blob/object stores. Use publishes_to/subscribes_to for \
messaging links (e.g. "service: billing | subscribes_to | topic: order-events") \
and stores_in for data residence (e.g. "service: checkout | stores_in | \
datastore: OrdersDb").
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
