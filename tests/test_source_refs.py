"""Citable refs parsed out of tool output (agent/refs.py).

These feed HYPERLINKS in the UI, so the bar is different from ordinary parsing: a
missed ref costs a link, while a wrong one sends the reader to a document the answer
was never about. Every test here is written from that asymmetry.
"""

from quickjoiner.agent.refs import MAX_REFS, SNIPPET_CHARS, parse_source_refs

SEARCH_OUTPUT = (
    "[source: Caffeine - Team Charter | uri: https://wiki/spaces/DEVKB/pages/5108498435/Caffeine "
    "| kind: page | score: 0.81]\n"
    "Agile Team Charter — Template v1.0. Members: Liu Maumasi (TL), Etheria Hill (DM).\n"
    "\n---\n\n"
    "[source: Narwhals - Team Charter | uri: https://wiki/spaces/DEVKB/pages/5107384329/Narwhals "
    "| kind: page | score: 0.74]\n"
    "This is a living document your team owns."
)


def test_parses_label_uri_kind_and_score():
    refs = parse_source_refs(SEARCH_OUTPUT)
    assert [r["label"] for r in refs] == ["Caffeine - Team Charter", "Narwhals - Team Charter"]
    assert refs[0]["uri"].endswith("/Caffeine")
    assert refs[0]["kind"] == "page" and refs[0]["score"] == 0.81


def test_snippet_is_the_source_own_text_and_stops_at_the_next_hit():
    """The excerpt shown under a title must come from THAT source — quoting the next
    hit's text under the previous hit's link would misattribute it."""
    first = parse_source_refs(SEARCH_OUTPUT)[0]["snippet"]
    assert first.startswith("Agile Team Charter")
    assert "Narwhals" not in first and "living document" not in first


def test_graph_expansion_bullets_are_parsed_too():
    """The RELATED section emits a one-line bullet with no kind/score — it is still a
    citable page and must still resolve to its uri."""
    out = (
        "RELATED via knowledge graph:\n"
        "- [source: Caffeine roster | uri: https://plumber/TeamDetails?team=Caffeine] "
        "— Caffeine works_on Provisioning"
    )
    (ref,) = parse_source_refs(out)
    assert ref["label"] == "Caffeine roster"
    assert ref["uri"] == "https://plumber/TeamDetails?team=Caffeine"
    assert "kind" not in ref and "score" not in ref


def test_repeated_hits_keep_the_first_occurrence():
    """search_memory returns hits in score order, so the first mention is the best
    snippet; later rounds re-returning the same document must not displace it."""
    out = (
        "[source: A | uri: u1 | kind: page | score: 0.9]\nbest excerpt\n\n---\n\n"
        "[source: A | uri: u1 | kind: page | score: 0.4]\nworse excerpt"
    )
    (ref,) = parse_source_refs(out)
    assert ref["snippet"] == "best excerpt"


def test_same_label_at_a_different_uri_is_a_distinct_source():
    """Two pages can share a title across systems; collapsing them would link one
    system's citation to another system's page."""
    out = "[source: Charter | uri: https://a/1]\nx\n\n---\n\n[source: Charter | uri: https://b/2]\ny"
    assert [r["uri"] for r in parse_source_refs(out)] == ["https://a/1", "https://b/2"]


def test_a_header_without_a_uri_is_still_listed_but_carries_no_link():
    """Worth naming in the sources panel; simply not linkable."""
    (ref,) = parse_source_refs("[source: Taught note | uri: ]\nsomething learned")
    assert ref["label"] == "Taught note" and ref["uri"] == ""


def test_snippets_are_bounded_and_cut_on_a_word_boundary():
    ref = parse_source_refs("[source: A | uri: u]\n" + "word " * 400)[0]
    assert len(ref["snippet"]) <= SNIPPET_CHARS + 1  # + the ellipsis
    assert ref["snippet"].endswith("…") and not ref["snippet"].endswith("wor…")


def test_non_source_text_yields_nothing_and_nothing_raises():
    assert parse_source_refs("") == []
    assert parse_source_refs("NO_RESULTS: nothing relevant found in learned memory.") == []
    # A graph tool's [evidence: …] shape carries no uri and is deliberately NOT a match.
    assert parse_source_refs("- Nautical --imports--> x [evidence: Nautical/src/a.js]") == []


def test_ref_count_is_capped():
    out = "\n\n".join(f"[source: S{i} | uri: u{i}]\ntext" for i in range(MAX_REFS + 40))
    assert len(parse_source_refs(out)) == MAX_REFS


def test_snippet_skips_the_contextual_chunking_breadcrumb():
    """Contextual chunking prepends "[source · title · uri]" to every chunk before
    embedding. Shown as an excerpt it just repeats the title and link already beside
    it, pushing the document's real first words out of the preview."""
    out = (
        "[source: Caffeine Team Charter | uri: https://wiki/Caffeine | kind: page | score: 0.8]\n"
        "[confluence:Appriver Confluence · Caffeine Team Charter · https://wiki/Caffeine]\n"
        "We only work on the most important things in the Top-10 only."
    )
    (ref,) = parse_source_refs(out)
    assert ref["snippet"].startswith("We only work on the most important")
    assert "confluence:Appriver" not in ref["snippet"]


def test_a_bracketed_line_that_is_not_a_breadcrumb_is_kept():
    """The strip is keyed on the breadcrumb's ' · ' separator, so ordinary bracketed
    prose at the start of a document survives into its excerpt."""
    out = "[source: Runbook | uri: u]\n[IMPORTANT] Restart the service before deploying."
    (ref,) = parse_source_refs(out)
    assert ref["snippet"].startswith("[IMPORTANT] Restart")


def test_graph_tool_evidence_resolves_like_a_search_hit():
    """Regression, user-reported: "list all teams with their members" is answered from
    graph_relations, whose evidence used to render as a bare "[evidence: <title>]" with
    the uri discarded — so the SAME document cited via the graph could not become a link
    while cited via search it could. Measured on the live corpus: only 5 of 11 citations
    in that answer resolved."""
    out = (
        "12 'works_on' relationship(s) (person -> team), grouped by the thing on the right:\n"
        "\nCaffeine (7): [evidence: Caffeine - Team Charter | uri: https://wiki/Caffeine]"
        " [evidence: Team Charters | uri: https://wiki/TeamCharters]\n"
        "  Liu Maumasi, Etheria Hill"
    )
    refs = parse_source_refs(out)
    assert [(r["label"], r["uri"]) for r in refs] == [
        ("Caffeine - Team Charter", "https://wiki/Caffeine"),
        ("Team Charters", "https://wiki/TeamCharters"),
    ]


def test_evidence_without_a_uri_is_not_mistaken_for_a_ref():
    """A same_as bridge cites no document; a code-graph edge's evidence is a file path
    with no uri. Neither is linkable and neither should appear as one."""
    out = (
        "- Caffeine --same_as--> caffeine [evidence: none — name-equality inference]\n"
        "- Nautical --imports--> react [evidence: Nautical/src/a.js]"
    )
    assert parse_source_refs(out) == []


def test_snippet_never_shows_a_sibling_citation_as_the_excerpt():
    """A graph_relations group cites several documents as consecutive brackets on one
    line, so the text after the first bracket is the SECOND bracket. Seen live: the
    sources panel rendered raw "[evidence: … | uri: …]" markup as an excerpt."""
    out = (
        "Caffeine (7): [evidence: Caffeine Team Charter | uri: https://wiki/Caffeine]"
        " [evidence: Development Knowledge Base | uri: https://wiki/DEVKB/overview]\n"
        "  Christopher Cotton, Enes Demirsoz, Etheria Hill"
    )
    first, second = parse_source_refs(out)
    assert first["label"] == "Caffeine Team Charter"
    assert "[evidence:" not in first["snippet"] and "uri:" not in first["snippet"]
    assert first["snippet"].startswith("Christopher Cotton")
    # ...and the second ref describes the same group without swallowing a third.
    assert "[evidence:" not in second["snippet"]


def test_octopus_and_web_page_uris_are_linkable_as_they_stand():
    """Both already carry real addresses — this pins that the link derivation leaves them
    alone rather than "helpfully" rewriting a working URL."""
    from quickjoiner.agent.weblinks import citable_link

    octo = "https://octopus.appriver.com/app#/projects/appriver-management-console"
    page = "https://plumber.appriver.corp/Details?id=222&project=AppRiver.Connector"
    assert citable_link(octo) == octo
    assert citable_link(page) == page


def test_a_live_octopus_line_is_citable():
    """The dashboard tool answers "where is X deployed" from the live server; its projects
    must cite to the same page the ingested project documents use."""
    from quickjoiner.connectors.live_tools import cite

    line = ("- AppRiver.Management.Console in Production: 0.1.14 — Success"
            + cite("Octopus project: AppRiver.Management.Console",
                   "https://octopus.appriver.com/app#/projects/appriver-management-console"))
    (ref,) = parse_source_refs(line)
    assert ref["label"] == "Octopus project: AppRiver.Management.Console"
    assert ref["uri"].endswith("/appriver-management-console")
