"""Table-structured extraction and the graph it feeds.

The motivating failure, measured on a real internal service catalogue: 17 team pages,
each carrying a complete member table, produced ZERO graph edges — a flattened table is
a wall of unlabelled values with no sentence linking a person to the team, and the LLM
triple extractor (correctly) emitted nothing from it. Worse, a blank cell disappeared
entirely, shifting every later value in that row into the wrong column.
"""

from __future__ import annotations

from quickjoiner.agent.tools import build_builtin_tools
from quickjoiner.config import RetrievalConfig
from quickjoiner.ingest.extract import extract_text
from quickjoiner.ingest.tables import (
    _DETERMINISTIC_ONLY_RELS,
    _PAIR_RELATIONS,
    extract_table_graph,
    parse_markdown_tables,
    subject_entities,
)
from quickjoiner.ingest.triples import RELATION_SIGNATURES, UNSIGNED_RELS

TEAM_PAGE = b"""<html><body>
<h1>Autobots - SC Aviator</h1>
<p>Members:</p>
<table>
  <tr><th>Name</th><th>Role</th><th>Title</th><th>Email</th><th>Phone</th><th>Location</th></tr>
  <tr><td>Caleb Spring</td><td>Dev</td><td>Developer III</td><td></td><td></td><td>Dallas</td></tr>
  <tr><td>Dorwin Shields</td><td>Dev</td><td>Developer III</td>
      <td>dshields@zixcorp.com</td><td></td><td>Dallas</td></tr>
</table>
</body></html>"""
TEAM_URI = "https://plumber.example.corp/TeamDetails?id=45&team=Autobots%20-%20SC%20Aviator"


# ----------------------------------------------------------------- extraction

def test_blank_cells_are_preserved_so_columns_cannot_shift():
    """The bug this whole feature exists for: `soup.get_text()` drops an empty cell, so
    a member with no email had their Location read as their Phone, and nothing in the
    stored text revealed it."""
    text = extract_text(TEAM_PAGE, "team.html")
    rows = [ln for ln in text.splitlines() if ln.startswith("| Caleb")]
    assert rows, text
    cells = [c.strip() for c in rows[0].strip("|").split("|")]
    # Name, Role, Title, Email(blank), Phone(blank), Location — six cells, in order.
    assert cells == ["Caleb Spring", "Dev", "Developer III", "", "", "Dallas"]

    # And the header survives, which is what makes the row interpretable at all.
    assert "| Name | Role | Title | Email | Phone | Location |" in text


def test_single_column_table_is_layout_not_data_and_degrades_to_text():
    html = b"<html><body><table><tr><td>just a layout wrapper</td></tr></table></body></html>"
    text = extract_text(html, "x.html")
    assert text.strip() == "just a layout wrapper"
    assert "|" not in text


def test_a_data_table_wrapped_in_a_layout_table_is_still_rendered():
    """Old intranet apps wrap real data tables in layout tables. Rendering the OUTER one
    would bury the whole data table inside a single cell, so only leaf tables render."""
    html = (b"<html><body><table><tr><td>sidebar</td><td>"
            b"<table><tr><th>Team</th><th>Repository</th></tr>"
            b"<tr><td>Caffeine</td><td>Connector</td></tr></table>"
            b"</td></tr></table></body></html>")
    text = extract_text(html, "x.html")
    assert text.count("| Caffeine | Connector |") == 1
    assert "| Team | Repository |" in text
    assert "sidebar" in text  # the skipped wrapper still contributes its text


def test_image_inside_a_cell_keeps_its_caption():
    html = (b'<html><body><table><tr><th>Name</th><th>Icon</th></tr>'
            b'<tr><td>Svc</td><td><img alt="health chart"></td></tr></table></body></html>')
    assert "[image: health chart]" in extract_text(html, "x.html")


def test_parse_markdown_tables_reads_caption_headers_and_rows():
    tables = parse_markdown_tables(
        "Members:\n| Name | Email |\n| --- | --- |\n| A Person | a@b.com |\n"
    )
    assert len(tables) == 1
    assert tables[0].caption == "Members:"
    assert tables[0].headers == ["Name", "Email"]
    assert tables[0].rows == [["A Person", "a@b.com"]]


def test_tables_without_a_separator_row_still_parse():
    """Confluence and hand-written markdown both emit these."""
    tables = parse_markdown_tables("Teams\n| Team | Repository |\n| Caffeine | Connector |\n")
    assert len(tables) == 1 and tables[0].rows == [["Caffeine", "Connector"]]


def test_subject_entities_reads_the_url_the_page_states_itself_by():
    assert subject_entities(TEAM_URI) == [
        ("team:autobots - sc aviator", "Autobots - SC Aviator", "team")
    ]
    # An opaque id names nothing usable, and an unrecognised parameter is not guessed at.
    assert subject_entities("https://x/Details?id=45&colour=blue") == []


# --------------------------------------------------------------------- graph

def test_team_page_yields_membership_edges_and_email_aliases():
    text = extract_text(TEAM_PAGE, "team.html")
    entities, aliases, edges = extract_table_graph(
        text, TEAM_URI, "web_scrape:Plumber", "Autobots - SC Aviator"
    )
    assert ("person:caleb spring", "works_on", "team:autobots - sc aviator") in [
        (s, r, d) for s, r, d, _ in edges
    ]
    assert ("person:dorwin shields", "works_on", "team:autobots - sc aviator") in [
        (s, r, d) for s, r, d, _ in edges
    ]
    # The email becomes an ALIAS of the person, which is what merges this source's people
    # with a tracker that names them "Dorwin Shields" and a wiki that @mentions them.
    assert ("dshields@zixcorp.com", "person:dorwin shields") in aliases
    # A member with no email still becomes a person; they simply carry no alias.
    assert not [a for a in aliases if a[1] == "person:caleb spring"]
    assert {e[2] for e in entities} == {"person", "team"}


def test_a_header_ending_in_a_spacer_column_is_still_a_header():
    """Measured on the live catalogue: the real roster's header row ends in an empty
    actions/spacer cell, so requiring EVERY header cell to be populated demoted it to a
    data row. The table then rendered headerless, every column went untyped, and seven
    members produced zero edges — the same silent nothing the flattened table did."""
    html = (b"<html><body><p>Members:</p><table>"
            b"<tr><td>Name</td><td>Role</td><td>Email</td><td></td></tr>"
            b"<tr><td>Liu Maumasi</td><td>Dev</td><td>lm@corp.com</td><td></td></tr>"
            b"</table></body></html>")
    text = extract_text(html, "team.html")
    assert "| Name | Role | Email |" in text

    _ents, aliases, edges = extract_table_graph(
        text, "https://plumber.example.corp/TeamDetails?id=46&team=Caffeine",
        "web_scrape:Plumber", "Caffeine",
    )
    assert ("person:liu maumasi", "works_on", "team:caffeine") in [
        (s, r, d) for s, r, d, _ in edges
    ]
    assert ("lm@corp.com", "person:liu maumasi") in aliases


def test_a_mostly_empty_first_row_is_data_not_a_header():
    """The tolerance is for a header that trails off, not a licence to promote any row:
    a wide row carrying two values is data and must leave the table headerless."""
    html = (b"<html><body><table>"
            b"<tr><td>Caffeine</td><td>Dallas</td><td></td><td></td><td></td></tr>"
            b"<tr><td>Decaf</td><td>Austin</td><td></td><td></td><td></td></tr>"
            b"</table></body></html>")
    text = extract_text(html, "x.html")
    assert "| Caffeine | Dallas |" in text
    # Promoted to a header it would have vanished from the body entirely.
    assert "| Decaf | Austin |" in text


def test_a_team_column_relates_each_row_without_any_url_subject():
    text = ("Team Members\n| Name | Email | Team |\n| --- | --- | --- |\n"
            "| Leif Thillet | lt@corp.com | Black Team |\n")
    _ents, aliases, edges = extract_table_graph(text, "https://x/Roster", "s", "Roster")
    assert ("person:leif thillet", "works_on", "team:black team") in [
        (s, r, d) for s, r, d, _ in edges
    ]
    assert ("lt@corp.com", "person:leif thillet") in aliases


def test_generic_name_column_is_typed_by_the_caption_not_guessed():
    """'Name' heads lists of services as often as lists of people, so it is only typed
    when the table says what it lists — or when an email column proves they are people."""
    text = "Teams\n| Name | Member Count |\n| --- | --- |\n| Black Team | 15 |\n"
    tables = parse_markdown_tables(text)
    from quickjoiner.ingest.tables import _column_types

    assert _column_types(tables[0]) == {0: "team"}

    # Same shape, caption that names no type: nothing is typed, nothing is invented.
    plain = parse_markdown_tables("Summary\n| Name | Count |\n| --- | --- |\n| Foo | 2 |\n")
    assert _column_types(plain[0]) == {}


def test_booleans_numbers_and_placeholders_never_become_entities():
    text = ("Members:\n| Name | Email | Active | Team |\n| --- | --- | --- | --- |\n"
            "| True | x@y.com | True | 42 |\n| N/A | | True | Black Team |\n")
    entities, _aliases, edges = extract_table_graph(text, "https://x/p", "s", "p")
    names = {e[1].lower() for e in entities}
    assert not (names & {"true", "n/a", "42"})
    assert not [e for e in edges if "true" in e[0] or "true" in e[2]]


def test_a_document_with_no_table_contributes_nothing():
    assert extract_table_graph("just prose about the team", "https://x/p", "s") == ([], [], [])


def test_only_entities_that_took_part_in_an_edge_are_emitted():
    """A node with no edge is exactly what gc_orphan_entities sweeps, so emitting one
    would be pure churn."""
    text = "Teams\n| Name | Member Count |\n| --- | --- |\n| Black Team | 15 |\n"
    entities, _a, edges = extract_table_graph(text, "https://x/Teams", "s", "Teams")
    assert edges == [] and entities == []


def test_table_pairs_satisfy_relation_signatures():
    """Lockstep with triples.RELATION_SIGNATURES: this extractor must never emit a shape
    the vocabulary's own domain/range validation would reject from the LLM."""
    for (src_type, dst_type), rel in _PAIR_RELATIONS.items():
        if rel in UNSIGNED_RELS or rel in _DETERMINISTIC_ONLY_RELS:
            continue
        assert rel in RELATION_SIGNATURES, f"{rel} has no signature"
        domain, range_ = RELATION_SIGNATURES[rel]
        assert src_type in domain, f"{src_type} cannot be the subject of {rel}"
        assert dst_type in range_, f"{dst_type} cannot be the object of {rel}"


def test_row_and_edge_counts_are_bounded():
    from quickjoiner.ingest.tables import _MAX_ROWS_PER_TABLE

    rows = "\n".join(f"| Person {i} | p{i}@x.com |" for i in range(_MAX_ROWS_PER_TABLE + 50))
    text = f"Members:\n| Name | Email |\n| --- | --- |\n{rows}\n"
    _e, _a, edges = extract_table_graph(text, "https://x/T?team=Big", "s", "Big")
    assert len(edges) == _MAX_ROWS_PER_TABLE


# ------------------------------------------------------- catalog + agent tool

def _seed(catalog):
    for eid, name, type_ in (
        ("team:black team", "Black Team", "team"),
        ("person:ann lee", "Ann Lee", "person"),
        ("person:bo ray", "Bo Ray", "person"),
        ("repo:widget", "Widget", "repo"),
    ):
        catalog.upsert_entity(eid, name, type_, "web_scrape:Plumber")
    catalog.upsert_document("d1", "web_scrape:Plumber", "https://x/TeamDetails?team=Black%20Team",
                            "Black Team", "web", "h", "2026-07-31", 1)
    catalog.replace_doc_edges("d1", [
        ("person:ann lee", "works_on", "team:black team", "table"),
        ("person:bo ray", "works_on", "team:black team", "table"),
        ("team:black team", "owns", "repo:widget", "table"),
    ])


def test_graph_relations_enumerates_one_relation_across_the_whole_graph(catalog):
    _seed(catalog)
    rows = catalog.graph_relations("works_on", src_type="person", dst_type="team")
    assert {r["src_name"] for r in rows} == {"Ann Lee", "Bo Ray"}
    # The type filters genuinely constrain: team->repo is `owns`, not `works_on`.
    assert catalog.graph_relations("works_on", src_type="team") == []


def test_graph_relations_tool_groups_by_the_right_hand_entity(catalog, store):
    _seed(catalog)
    tools = {t.spec.name: t for t in build_builtin_tools(store, catalog, None, RetrievalConfig())}
    out = tools["graph_relations"].fn(rel="works_on", src_type="person", dst_type="team")
    assert "Black Team (2)" in out
    assert "Ann Lee, Bo Ray" in out
    assert "Black Team" in out and "evidence" in out

    missing = tools["graph_relations"].fn(rel="publishes_to")
    assert missing.startswith("NO_RESULTS")


def test_graph_neighbors_hub_view_separates_incoming_from_outgoing(catalog, store):
    """Regression: grouping a hub's edges by relation ALONE hid whole categories. A team
    with many outgoing `works_on` (its projects) and a few incoming `works_on` (its
    people) put both in one bucket ordered by destination, so every sampled row was a
    project and not one member appeared."""
    catalog.upsert_entity("team:hub", "Hub", "team", "s")
    catalog.upsert_document("d2", "s", "https://x/hub", "Hub", "web", "h", "2026-07-31", 1)
    edges = [(f"person:p{i}", "works_on", "team:hub", "") for i in range(4)]
    edges += [("team:hub", "works_on", f"project:proj{i}", "") for i in range(70)]
    for i in range(4):
        catalog.upsert_entity(f"person:p{i}", f"P{i}", "person", "s")
    for i in range(70):
        catalog.upsert_entity(f"project:proj{i}", f"Proj{i}", "project", "s")
    catalog.replace_doc_edges("d2", edges)

    tools = {t.spec.name: t for t in build_builtin_tools(store, catalog, None, RetrievalConfig())}
    out = tools["graph_neighbors"].fn(entity="Hub")
    assert "<--works_on--" in out and "--works_on-->" in out
    assert "P0" in out, "incoming member edges must survive the hub sampling"


# ------------------------------------------------------------- end to end

def test_ingested_team_page_becomes_queryable_membership(catalog, store):
    """The whole chain on one document: HTML -> structured rows -> deterministic edges ->
    one `graph_relations` call answering "list all teams with their members", which a
    top-k memory search cannot answer at all."""
    from quickjoiner.connectors.base import Document
    from quickjoiner.ingest.pipeline import IngestPipeline

    pipeline = IngestPipeline(store, catalog)
    pages = {
        "Caffeine": [("Ann Lee", "alee@corp.com"), ("Bo Ray", "")],
        "Narwhals": [("Cy Fox", "cfox@corp.com")],
    }
    docs = []
    for team, members in pages.items():
        rows = "".join(
            f"<tr><td>{n}</td><td>Dev</td><td>{e}</td><td>Dallas</td></tr>"
            for n, e in members
        )
        html = (f"<html><body><h1>{team}</h1><p>Members:</p><table>"
                f"<tr><th>Name</th><th>Role</th><th>Email</th><th>Location</th></tr>"
                f"{rows}</table></body></html>").encode()
        docs.append(Document(
            uri=f"https://plumber.example.corp/TeamDetails?id=1&team={team}",
            title=team, kind="web", text=extract_text(html, "p.html"),
        ))
    pipeline.ingest(iter(docs), "web_scrape:Plumber")

    rows = catalog.graph_relations("works_on", src_type="person", dst_type="team")
    by_team: dict[str, set[str]] = {}
    for r in rows:
        by_team.setdefault(r["dst_name"], set()).add(r["src_name"])
    assert by_team == {"Caffeine": {"Ann Lee", "Bo Ray"}, "Narwhals": {"Cy Fox"}}

    # Every edge is citable back to the page that asserted it — the honesty contract.
    assert all(r["evidence_title"] in pages for r in rows)

    # And the email registered as an alias, so a tracker that knows this person only by
    # their address resolves to the same node.
    assert catalog.resolve_entity("alee@corp.com")["id"] == "person:ann lee"
