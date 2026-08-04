"""Deterministic knowledge-graph extraction from tabular content.

A table is the most structured thing on a page and, until now, the least mined. The LLM
triple extractor reads prose well and tables badly: measured against a real internal
service catalogue, **0 of 17** team pages yielded a single edge, because a flattened table
is a wall of unlabelled values with no sentence connecting any person to the team. The
same pages' key-value blocks ("Teams: Caffeine") extracted fine. Structure was the whole
difference, so this module reads the structure directly and deterministically — 17 of 17,
not 0 of 17 — the same way `code_graph.py` and `pubsub.py` mine code and config.

It runs on the **markdown pipe rows** `ingest/extract.py` now preserves, not on HTML, so it
serves every source whose text carries a table: scraped pages, Confluence `body.view`,
markdown files, and Office documents.

Two things are read, and only two, because both are stated rather than inferred:

  1. **Typed columns.** A header names its column's entity type (`Team`, `Repository`,
     `Environment`, …). A generic `Name` column is typed only when the table's own caption
     says what it lists ("Teams" → team), or when an email column proves the rows are
     people. Two typed cells in one row are a relationship the table asserts.
  2. **The subject in the URL.** Catalogue web apps carry the entity in the query string
     (`/TeamDetails?id=45&team=Autobots`), which is the page telling us what it is about in
     machine-readable form. That subject pairs with each row, which is what turns a members
     table with no team column into `person --works_on--> team`.

Everything else is left alone. An untyped column contributes nothing rather than a guess,
and no relation is emitted that `triples.RELATION_SIGNATURES` would reject — the pairs
below are a subset of it, checked in the test suite so the two can't drift apart.

Pure and testable, same contract as its siblings: (text, uri, src_id) -> (entities,
aliases, edges). Entities are (id, name, type) 3-tuples, aliases (alias, entity_id)
2-tuples, edges (src, rel, dst, detail) 4-tuples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

# Bounds. A catalogue page can carry a 300-row roster and a crawl can carry thousands of
# pages, so every axis is capped; truncation costs edges, never correctness.
_MAX_TABLES_PER_DOC = 20
_MAX_ROWS_PER_TABLE = 400
_MAX_EDGES_PER_DOC = 600
_MAX_NAME_CHARS = 80
_MIN_NAME_CHARS = 3

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
# A markdown separator row: | --- | :---: | ---: |
_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+$")

# Header keyword -> entity type. Matched on the normalized header, exactly or as a whole
# word, so "Octopus Project" types as project but "Projected Cost" does not.
_HEADER_TYPES: dict[str, str] = {
    "team": "team", "teams": "team", "owning team": "team", "squad": "team",
    "repo": "repo", "repos": "repo", "repository": "repo", "repositories": "repo",
    "service": "service", "services": "service", "application": "service",
    "applications": "service", "component": "service",
    "project": "project", "projects": "project",
    "environment": "environment", "environments": "environment", "env": "environment",
    "package": "package", "packages": "package", "library": "package",
    "pipeline": "pipeline", "pipelines": "pipeline", "build": "pipeline",
}
# Columns that name a person, but only once the table proves it is about people (an email
# column). "Name" alone is the most ambiguous header there is — it heads lists of services
# and environments just as often.
_PERSON_HEADERS = {
    "name", "names", "member", "members", "full name", "display name",
    "person", "employee", "user", "developer", "engineer", "contact",
}
_EMAIL_HEADERS = {"email", "e-mail", "email address", "mail", "primary email"}
# A generic column takes the type its caption announces: a table captioned "Teams" whose
# only column is "Name" is a list of teams, and the caption is the page saying so.
_CAPTION_TYPES = {
    "teams": "team", "repositories": "repo", "repos": "repo", "services": "service",
    "projects": "project", "environments": "environment", "packages": "package",
    "pipelines": "pipeline", "applications": "service", "components": "service",
}
# Query parameters that name the page's subject. Deliberately short: each one is a
# convention catalogue apps genuinely use, not a guess at what a parameter might mean.
_URL_SUBJECT_PARAMS = {
    "team": "team", "project": "project", "service": "service", "repo": "repo",
    "repository": "repo", "environment": "environment", "application": "service",
}

# Relations that live outside the LLM triple vocabulary because a connector asserts them
# structurally rather than mining them from prose (the same footing as ADO's `builds` /
# `implemented_in`). They have no RELATION_SIGNATURES entry to check against, and conform
# by construction — this extractor only emits one when both columns are typed.
_DETERMINISTIC_ONLY_RELS = frozenset({"builds"})

# Which relation two typed cells in the same row assert. Ordered pair -> relation, so
# direction is explicit. Every entry either satisfies triples.RELATION_SIGNATURES or is
# listed above (pinned by test_table_pairs_satisfy_relation_signatures) — a deliberate
# subset of what the vocabulary allows, covering the shapes real catalogues tabulate.
_PAIR_RELATIONS: dict[tuple[str, str], str] = {
    ("person", "team"): "works_on",
    ("person", "project"): "works_on",
    ("person", "repo"): "works_on",
    ("person", "service"): "works_on",
    ("team", "repo"): "owns",
    ("team", "service"): "owns",
    ("team", "project"): "owns",
    ("team", "package"): "owns",
    ("repo", "project"): "part_of",
    ("service", "project"): "part_of",
    ("service", "environment"): "deploys",
    ("pipeline", "repo"): "builds",
    ("repo", "package"): "provides",
}

# Values that are data, not entity names. A boolean or a bare number in a typed column is
# a fact about the row, never a thing to put in the graph.
_NON_ENTITY_VALUES = {
    "true", "false", "yes", "no", "n/a", "na", "none", "null", "-", "—", "unknown",
    "active", "inactive", "enabled", "disabled", "0", "1",
}


@dataclass
class Table:
    """One parsed markdown table plus the caption line that introduced it."""

    caption: str
    headers: list[str]
    rows: list[list[str]] = field(default_factory=list)


def _norm(s: str) -> str:
    return " ".join((s or "").replace("\\|", "|").split()).strip().lower()


def _split_row(line: str) -> list[str]:
    """Cells of a `| a | b |` row, outer pipes dropped, escapes restored."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip().replace("\\|", "|") for c in inner.split("|")]


def parse_markdown_tables(text: str) -> list[Table]:
    """Every markdown pipe table in `text`, with the nearest preceding non-empty,
    non-table line as its caption.

    Tolerant of tables written without a separator row (a header row followed by data
    rows of the same width still reads as a table), because Confluence and hand-written
    markdown both produce those.
    """
    lines = (text or "").splitlines()
    tables: list[Table] = []
    i = 0
    last_text_line = ""
    while i < len(lines) and len(tables) < _MAX_TABLES_PER_DOC:
        line = lines[i].strip()
        if not line.startswith("|") or line.count("|") < 2:
            if line:
                last_text_line = line
            i += 1
            continue
        block: list[list[str]] = []
        while i < len(lines):
            cur = lines[i].strip()
            if not cur.startswith("|") or cur.count("|") < 2:
                break
            if not _SEPARATOR.match(cur):
                block.append(_split_row(cur))
            i += 1
        if len(block) >= 2:
            width = max(len(r) for r in block)
            headers = block[0] + [""] * (width - len(block[0]))
            rows = [r + [""] * (width - len(r)) for r in block[1:]][:_MAX_ROWS_PER_TABLE]
            tables.append(Table(caption=last_text_line, headers=headers, rows=rows))
    return tables


def subject_entities(uri: str) -> list[tuple[str, str, str]]:
    """(id, name, type) for entities the URL itself names — a catalogue app's
    `?team=Black%20Team` is the page stating its own subject in machine-readable form.

    Only recognised parameter names are read, and only values that look like a name
    (an id like `?id=45` names nothing we can use)."""
    try:
        query = urlsplit(uri or "").query
    except ValueError:
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for key, raw in parse_qsl(query, keep_blank_values=False):
        type_ = _URL_SUBJECT_PARAMS.get(_norm(key))
        if type_ is None:
            continue
        name = unquote(raw).strip()
        if not _usable_name(name):
            continue
        eid = f"{type_}:{name.lower()}"
        if eid not in seen:
            seen.add(eid)
            out.append((eid, name, type_))
    return out


def _usable_name(value: str) -> bool:
    """Whether a cell value can stand as an entity name."""
    v = (value or "").strip()
    if not (_MIN_NAME_CHARS <= len(v) <= _MAX_NAME_CHARS):
        return False
    if v.lower() in _NON_ENTITY_VALUES:
        return False
    if not any(c.isalpha() for c in v):  # pure numbers/dates/punctuation
        return False
    return True


def _column_types(table: Table) -> dict[int, str]:
    """Index -> entity type for the columns we can type with confidence."""
    headers = [_norm(h) for h in table.headers]
    types: dict[int, str] = {}
    has_email = any(h in _EMAIL_HEADERS or "email" in h.split() for h in headers)
    caption_type = _CAPTION_TYPES.get(_norm(table.caption).rstrip(":"))

    for idx, h in enumerate(headers):
        if not h:
            continue
        if h in _HEADER_TYPES:
            types[idx] = _HEADER_TYPES[h]
            continue
        # multi-word headers like "Octopus Project" / "TFS Repository"
        words = h.split()
        tail = _HEADER_TYPES.get(words[-1]) if words else None
        if tail is not None:
            types[idx] = tail
            continue
        if h in _PERSON_HEADERS:
            if has_email:
                types[idx] = "person"
            elif caption_type is not None:
                # "Teams" table whose column is just "Name": the caption types it.
                types[idx] = caption_type
    return types


def _email_columns(table: Table) -> list[int]:
    return [i for i, h in enumerate(table.headers)
            if _norm(h) in _EMAIL_HEADERS or "email" in _norm(h).split()]


def extract_table_graph(
    text: str, uri: str, src_id: str, title: str = ""
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """(entities, aliases, edges) asserted by the tables in one document.

    `src_id` is the source entity (the connector), used only for the edge detail line —
    every edge here is between entities the table names, not between the source and them.
    """
    tables = parse_markdown_tables(text)
    if not tables:
        return [], [], []

    subjects = subject_entities(uri)
    entities: dict[str, tuple[str, str, str]] = {e[0]: e for e in subjects}
    aliases: set[tuple[str, str]] = set()
    edges: list[tuple] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add_entity(name: str, type_: str) -> str:
        eid = f"{type_}:{name.strip().lower()}"
        entities.setdefault(eid, (eid, name.strip(), type_))
        return eid

    def add_edge(src: str, rel: str, dst: str, detail: str) -> None:
        if src == dst or len(edges) >= _MAX_EDGES_PER_DOC:
            return
        key = (src, rel, dst)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append((src, rel, dst, detail))

    for table in tables:
        col_types = _column_types(table)
        if not col_types:
            continue
        email_cols = _email_columns(table)
        where = f"table in {title[:60]}" if title else "table"

        for row in table.rows:
            # Typed cells present in THIS row, as (type, entity_id).
            present: list[tuple[str, str]] = []
            for idx, type_ in col_types.items():
                if idx >= len(row):
                    continue
                value = row[idx].strip()
                if not _usable_name(value):
                    continue
                # A person column holding an email is still a person; the email is then
                # both the name and the alias, which is what lets it merge later.
                present.append((type_, add_entity(value, type_)))

            # Emails in a person row become aliases of that person — the join that makes
            # one source's "Leif Thillet" and another's "lthillet@corp.com" one node.
            people = [eid for t, eid in present if t == "person"]
            if people:
                for col in email_cols:
                    if col < len(row):
                        addr = row[col].strip().lower()
                        if _EMAIL.match(addr):
                            for eid in people:
                                aliases.add((addr, eid))

            # Relationships the row asserts: between its own typed cells, and between
            # each typed cell and the subject the URL named.
            for a_type, a_id in present:
                for b_type, b_id in present:
                    rel = _PAIR_RELATIONS.get((a_type, b_type))
                    if rel:
                        add_edge(a_id, rel, b_id, where)
                for s_id, _s_name, s_type in subjects:
                    rel = _PAIR_RELATIONS.get((a_type, s_type))
                    if rel:
                        add_edge(a_id, rel, s_id, where)
                    rel_rev = _PAIR_RELATIONS.get((s_type, a_type))
                    if rel_rev:
                        add_edge(s_id, rel_rev, a_id, where)

    if not edges:
        return [], [], []
    # Only entities that actually took part in an edge: a node with no edge is exactly
    # what gc_orphan_entities sweeps, so emitting one would be churn.
    used = {e[0] for e in edges} | {e[2] for e in edges}
    return (
        [e for eid, e in entities.items() if eid in used],
        [a for a in sorted(aliases) if a[1] in used],
        edges,
    )
