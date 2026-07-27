from datetime import datetime, timezone
from pathlib import Path

import pytest

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.azure_devops import (
    _merge_graphs,
    _next_hierarchy_ids,
    _team_is_dormant,
    _workitem_id_from_url,
    build_map_document,
    builds_document,
    dev_link_graph,
    hierarchy_graph,
    select_recent_iterations,
    work_item_document,
)
from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.github import issue_document as gh_issue_document
from quickjoiner.connectors.github import pr_document, runs_document
from quickjoiner.connectors.gitlab import branch_sync_document, mr_document, mr_graph
from quickjoiner.connectors.jira import _adf_to_text, issue_document as jira_issue_document
from quickjoiner.connectors.registry import CONNECTOR_TYPES, _load_builtin_connectors, create_connector
from quickjoiner.connectors.util import resolve_secret


def test_registry_knows_all_phase2_types(tmp_path):
    _load_builtin_connectors()
    for type_ in ("files", "git", "github", "jira", "confluence", "azure_devops"):
        assert type_ in CONNECTOR_TYPES, f"missing connector type {type_}"
    connector = create_connector(
        SourceConfig(name="x", type="github", options={"repo": "o/r"}), tmp_path
    )
    assert connector.source_id == "github:x"
    assert Mode.LIVE in connector.modes


def test_unknown_connector_type_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown connector type"):
        create_connector(SourceConfig(name="x", type="nope"), tmp_path)


class _CancelAfter:
    """A SyncControl stand-in that raises SyncStopped once `check()` has been called n
    times, and records every stage() for assertions. Proves a connector's inner-loop
    `_checkpoint()`/`_stage()` are actually wired, not just present in the source."""

    def __init__(self, n):
        self.n = n
        self.checks = 0
        self.stages = []

    def check(self):
        from quickjoiner.sync_control import SyncStopped
        self.checks += 1
        if self.checks >= self.n:
            raise SyncStopped()

    def stage(self, name, done=None, total=None):
        self.stages.append((name, done, total))
        self.check()


def test_jira_sync_honors_stop_and_reports_progress(tmp_path, monkeypatch):
    """Jira paginates behind one HTTP call per page; with the control wired, a stop lands
    within a page instead of after the whole search, and it reports an accurate % from the
    API's `total`."""
    from quickjoiner.sync_control import SyncStopped

    pages = {0: {"issues": [{"key": f"PROJ-{i}", "fields": {"summary": "s"}} for i in range(50)], "total": 500}}

    def fake_get_json(url, **kw):
        start = kw.get("params", {}).get("startAt", 0)
        return pages.get(start, {"issues": [{"key": f"PROJ-{start}", "fields": {"summary": "s"}}], "total": 500})

    monkeypatch.setattr("quickjoiner.connectors.jira.get_json", fake_get_json)
    conn = create_connector(SourceConfig(name="j", type="jira", options={"base_url": "https://x"}), tmp_path)
    conn._control = _CancelAfter(n=3)  # stop after a few checkpoints
    with pytest.raises(SyncStopped):
        list(conn.sync({}))
    # It stopped early (didn't drain all 500), and reported the "issues" stage with a total.
    assert any(name == "issues" and total == 500 for name, _done, total in conn._control.stages)


def test_confluence_reports_accurate_percent_via_cql_count(tmp_path, monkeypatch):
    """Confluence's content-listing API gives no total, so the sync preflights the space's
    page count via the CQL `/rest/api/search` endpoint's `totalSize` (the content-listing
    `/rest/api/content` pull does not carry a total) and reports an accurate % from it."""
    calls = {"search": 0}

    def fake_get_json(url, **kw):
        if url.endswith("/rest/api/search"):
            calls["search"] += 1
            return {"totalSize": 3}  # the preflight count for the space
        start = kw["params"]["start"]
        if start == 0:
            return {"results": [
                {"id": str(i), "title": f"P{i}", "body": {"storage": {"value": "<p>x</p>"}},
                 "version": {"when": "2026-07-01T00:00:00Z"}, "_links": {"webui": "/x"}}
                for i in range(3)
            ]}
        return {"results": []}

    monkeypatch.setattr("quickjoiner.connectors.confluence.get_json", fake_get_json)
    conn = create_connector(
        SourceConfig(name="c", type="confluence", options={"base_url": "https://x", "spaces": ["ENG"]}),
        tmp_path,
    )
    ctrl = _CancelAfter(n=1000)  # capture stages, never cancel
    conn._control = ctrl
    docs = list(conn.sync({}))
    assert len(docs) == 3 and calls["search"] == 1  # counted once, up front
    # An accurate total (3) is reported for the ENG space — a real bar, not the shimmer.
    assert any(name == "pages · ENG" and total == 3 for name, _done, total in ctrl.stages)
    assert (3, 3) in [(done, total) for _n, done, total in ctrl.stages]  # reached 100%


def test_confluence_unscoped_pull_has_no_percent(tmp_path, monkeypatch):
    """With no space to count, there's no denominator — it reports the phase, no total."""
    def fake_get_json(url, **kw):
        return {"results": []}  # no pages; the search endpoint must never be hit

    monkeypatch.setattr("quickjoiner.connectors.confluence.get_json", fake_get_json)
    conn = create_connector(
        SourceConfig(name="c", type="confluence", options={"base_url": "https://x"}), tmp_path,
    )
    ctrl = _CancelAfter(n=1000)
    conn._control = ctrl
    list(conn.sync({}))
    assert ctrl.stages and all(total is None for _n, _d, total in ctrl.stages)  # shimmer, no %


def test_gitlab_sync_reports_phase_labels(tmp_path, monkeypatch):
    conn = create_connector(SourceConfig(name="g", type="gitlab", options={"project": "grp/app"}), tmp_path)
    conn._control = _CancelAfter(n=1)  # stop at the very first checkpoint
    from quickjoiner.sync_control import SyncStopped

    monkeypatch.setattr("quickjoiner.connectors.gitlab.get_json", lambda *a, **k: [])
    with pytest.raises(SyncStopped):
        list(conn.sync({}))
    # stage now carries the repo it's syncing ("merge requests · app") since one connector
    # can ingest several projects (group-scoped).
    assert conn._control.stages and conn._control.stages[0][0].startswith("merge requests")


def test_gitlab_mr_graph_links_mr_branch_and_repo():
    mr = {"iid": 88, "title": "Pre-seed usage", "source_branch": "feature/pre-seed",
          "target_branch": "develop"}
    g = mr_graph("AppRiver.Connector", mr)
    types = {e[0]: e[2] for e in g["entities"]}
    assert types["merge_request:appriver.connector/!88"] == "merge_request"
    assert types["branch:appriver.connector/feature/pre-seed"] == "branch"
    assert types["repo:appriver.connector"] == "repo"
    edges = {(s, r, d) for s, r, d, _ in g["edges"]}
    assert ("merge_request:appriver.connector/!88", "from_branch",
            "branch:appriver.connector/feature/pre-seed") in edges
    assert ("merge_request:appriver.connector/!88", "in_repo", "repo:appriver.connector") in edges
    assert ("branch:appriver.connector/feature/pre-seed", "belongs_to", "repo:appriver.connector") in edges


def test_gitlab_mr_branch_entity_matches_tfs_dev_link_branch():
    # THE JOIN: a GitLab MR's source-branch entity and a TFS work item's dev-link branch
    # entity resolve to the SAME id (name parity), so ticket --on_branch--> branch <--
    # from_branch-- merge_request connects a TFS ticket to its GitLab MR. This is the whole
    # point of "what's the MR for this TFS issue?".
    tfs_item = {"id": 320753, "relations": [
        {"rel": "ArtifactLink", "url": "vstfs:///Git/Ref/proj%2Fguid%2FGBfeature%2Fpre-seed",
         "attributes": {"name": "Branch"}}]}
    tfs = dev_link_graph(tfs_item, {"guid": "AppRiver.Connector"})
    mr = mr_graph("AppRiver.Connector", {"iid": 88, "source_branch": "feature/pre-seed"})
    tfs_branch = next(e[0] for e in tfs["entities"] if e[2] == "branch")
    mr_branch = next(e[0] for e in mr["entities"] if e[2] == "branch")
    assert tfs_branch == mr_branch == "branch:appriver.connector/feature/pre-seed"


def test_gitlab_ticket_in_branch_rule_links_mr_directly_to_ticket():
    # Org-specific opt-in rule: the branch/MR name starts with the TFS work-item id, so a
    # DIRECT merge_request --implements--> ticket:#N edge is emitted (and branch --for_ticket).
    import re
    mr = {"iid": 88, "title": "pre-seed usage", "source_branch": "320753-pre-seed"}
    g = mr_graph("AppRiver.Connector", mr, re.compile(r"(\d{4,})"))
    edges = {(s, r, d) for s, r, d, _ in g["edges"]}
    assert ("merge_request:appriver.connector/!88", "implements", "ticket:#320753") in edges
    assert ("branch:appriver.connector/320753-pre-seed", "for_ticket", "ticket:#320753") in edges
    # ticket:#320753 is exactly how the ADO connector keys the same work item -> they merge
    assert any(e[0] == "ticket:#320753" and e[2] == "ticket" for e in g["entities"])


def test_gitlab_ticket_in_branch_off_by_default():
    g = mr_graph("Repo", {"iid": 1, "source_branch": "320753-x"})  # no pattern -> rule off
    assert not any(r == "implements" for _, r, _, _ in g["edges"])


def test_gitlab_mr_document_attaches_graph_only_with_repo_name():
    mr = {"iid": 5, "title": "T", "source_branch": "topic/x"}
    assert not mr_document("grp/app", mr).metadata.get("graph")  # no repo_name -> no graph
    doc = mr_document("grp/app", mr, "MyRepo")
    assert any(e[1] == "from_branch" for e in doc.metadata["graph"]["edges"])


def test_gitlab_branch_sync_document_records_tfs_mirror():
    syncs = [
        {"branch": "feature/pre-seed", "pipeline_id": 4321,
         "pipeline_url": "https://gl/p/4321", "job": "sync-to-tfs", "finished_at": "2026-07-21T09:00:00Z"},
    ]
    doc = branch_sync_document("grp/app", "AppRiver.Connector", syncs)
    assert "feature/pre-seed" in doc.text and "#4321" in doc.text and "sync-to-tfs" in doc.text
    edges = {(s, r, d) for s, r, d, _ in doc.metadata["graph"]["edges"]}
    assert ("branch:appriver.connector/feature/pre-seed", "synced_to_tfs",
            "repo:appriver.connector") in edges


def test_resolve_secret_precedence(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "from-env-indirect")
    monkeypatch.setenv("FALLBACK_TOKEN", "from-fallback")
    assert resolve_secret({"token": "literal"}, "token", "FALLBACK_TOKEN") == "literal"
    assert resolve_secret({"token": "env:MY_TOKEN"}, "token", "FALLBACK_TOKEN") == "from-env-indirect"
    assert resolve_secret({}, "token", "FALLBACK_TOKEN") == "from-fallback"
    assert resolve_secret({}, "token") is None


def test_gitlab_list_tools_enumerate_live_by_state(tmp_path, monkeypatch):
    # "list all open MRs" is an ENUMERATION query — learned memory returns top-k by similarity,
    # never all-matching-a-filter, so it must go live. The connector exposes list tools for it.
    from quickjoiner.connectors.gitlab import GitLabConnector

    seen = {}

    def fake_get_json(url, **kw):
        seen["endpoint"] = url.rsplit("/", 1)[-1]
        seen["state"] = kw["params"]["state"]
        return [{"iid": 94, "state": "opened", "title": "Product card",
                 "source_branch": "feature/x", "author": {"username": "blambert"},
                 "web_url": "https://gl/mr/94"}]

    monkeypatch.setattr("quickjoiner.connectors.gitlab.get_json", fake_get_json)
    conn = GitLabConnector("Connector GitLab", {"project": "zix/appriver.connector"}, tmp_path)
    names = [t.spec.name for t in conn.tools()]
    # Consolidated (plan 09): type-level names, no per-source suffix, `project` selector optional
    # for a single connector.
    assert "gitlab_list_merge_requests" in names and "gitlab_list_issues" in names

    mr = next(t for t in conn.tools() if t.spec.name == "gitlab_list_merge_requests")
    out = mr.fn(state="opened", max_results=40)
    assert seen["endpoint"] == "merge_requests" and seen["state"] == "opened"
    assert "!94" in out and "opened" in out and "https://gl/mr/94" in out and "feature/x" in out
    # an invalid state falls back to opened (never sends a bad filter to the API)
    mr.fn(state="garbage")
    assert seen["state"] == "opened"
    # issues tool hits the issues endpoint
    iss = next(t for t in conn.tools() if t.spec.name == "gitlab_list_issues")
    iss.fn(state="closed")
    assert seen["endpoint"] == "issues" and seen["state"] == "closed"


def test_gitlab_type_tools_consolidate_across_projects_with_selector(tmp_path, monkeypatch):
    # Plan 09 Phase 0: N GitLab connectors -> ONE tool set (not N×), each tool taking a `project`
    # selector that routes to the right connector; an unresolved selector returns a "which?" hint.
    from quickjoiner.connectors.gitlab import GitLabConnector

    hit = {}

    def fake_get_json(url, **kw):
        hit["url"] = url
        return []

    monkeypatch.setattr("quickjoiner.connectors.gitlab.get_json", fake_get_json)
    conns = [
        GitLabConnector("Connector GitLab", {"project": "grp/appriver.connector"}, tmp_path / "a"),
        GitLabConnector("Nautical GitLab", {"project": "grp/appriver.nautical"}, tmp_path / "b"),
    ]
    tools = GitLabConnector.type_tools(conns)
    names = [t.spec.name for t in tools]
    # one consolidated set (~13), type-level names, no per-source suffix
    assert len(tools) >= 12 and names.count("gitlab_list_merge_requests") == 1
    mr = next(t for t in tools if t.spec.name == "gitlab_list_merge_requests")
    # selector routes to the matching project
    mr.fn(project="nautical", state="opened")
    assert "appriver.nautical" in hit["url"]
    # ambiguous (no selector, multiple connectors) -> helpful hint, no call made
    out = mr.fn(state="opened")
    assert "Specify which project" in out


def test_gitlab_group_connector_resolves_repos_by_name_and_enumerates(tmp_path, monkeypatch):
    # Plan 09 Phase 3: ONE group-scoped connector reaches any repo in the group by name, and
    # enumerates group-wide — so you connect the org once instead of one connector per repo.
    from quickjoiner.connectors.gitlab import GitLabConnector

    calls = {}

    def fake_get_json(url, **kw):
        calls["url"] = url
        if "/groups/zix/projects" in url:  # dynamic repo resolution
            return [{"id": 42, "name": "appriver.connector", "path": "appriver.connector",
                     "path_with_namespace": "zix/dev/appriver.connector"}]
        if "/groups/zix/merge_requests" in url:  # group-wide enumeration
            return [{"iid": 9, "state": "opened", "title": "X", "source_branch": "b",
                     "author": {"username": "u"}, "web_url": "https://gl/9"}]
        return [{"iid": 9, "state": "opened", "title": "X", "author": {"username": "u"},
                 "web_url": "https://gl/9", "source_branch": "b"}]

    monkeypatch.setattr("quickjoiner.connectors.gitlab.get_json", fake_get_json)
    gc = GitLabConnector("Gitlab Zix", {"base_url": "https://gl", "group": "zix"}, tmp_path)
    tools = {t.spec.name: t for t in GitLabConnector.type_tools([gc])}
    # name a repo -> resolves via the group projects search, then hits that project
    out = tools["gitlab_list_merge_requests"].fn(project="connector", state="opened")
    assert "/projects/42/merge_requests" in calls["url"]
    # no repo named -> group-wide enumeration across zix
    out2 = tools["gitlab_list_merge_requests"].fn(state="opened")
    assert "across group zix" in out2 and "/groups/zix/merge_requests" in calls["url"]
    # ingest list drives what a group connector syncs (tools reach the whole group regardless)
    assert GitLabConnector("g", {"group": "zix", "projects": ["a", "b"]}, tmp_path)._ingest_projects() == ["a", "b"]


def test_github_type_tools_expose_the_pr_family(tmp_path, monkeypatch):
    from quickjoiner.connectors.github import GitHubConnector

    seen = {}

    def fake_get_json(url, **kw):
        seen["url"] = url
        seen["state"] = (kw.get("params") or {}).get("state")
        return [{"number": 7, "state": "open", "title": "Fix", "head": {"ref": "topic"},
                 "user": {"login": "octocat"}, "html_url": "https://gh/pr/7"}]

    monkeypatch.setattr("quickjoiner.connectors.github.get_json", fake_get_json)
    conn = GitHubConnector("gh", {"repo": "acme/platform"}, tmp_path)
    names = [t.spec.name for t in conn.tools()]
    for n in ("github_list_pull_requests", "github_get_pull_request", "github_workflow_runs",
              "github_list_issues", "github_compare", "github_repo_info"):
        assert n in names
    pr = next(t for t in conn.tools() if t.spec.name == "github_list_pull_requests")
    out = pr.fn(state="open")
    assert seen["url"].endswith("/pulls") and seen["state"] == "open"
    assert "#7" in out and "topic" in out and "https://gh/pr/7" in out


def test_gitlab_pipeline_status_tool_is_live_and_links(tmp_path, monkeypatch):
    # "did the build succeed / link to the build" is LIVE state, not learned memory.
    from quickjoiner.connectors.gitlab import GitLabConnector

    def fake_get_json(url, **kw):
        assert url.endswith("/pipelines") and kw["params"]["ref"] == "feature/x"
        return [{"id": 13208669, "status": "success", "updated_at": "2026-06-26T14:37:15Z",
                 "web_url": "https://gl/pipelines/13208669"}]

    monkeypatch.setattr("quickjoiner.connectors.gitlab.get_json", fake_get_json)
    conn = GitLabConnector("g", {"project": "grp/app"}, tmp_path)
    tool = next(t for t in conn.tools() if "pipeline_status" in t.spec.name)
    out = tool.fn("feature/x")
    assert "#13208669" in out and "success" in out and "https://gl/pipelines/13208669" in out
    assert tool.fn("") == "Provide a branch/ref name."  # empty ref guarded


def test_suggest_api_connector_detects_github_and_gitlab():
    from quickjoiner.connectors.git_repo import suggest_api_connector

    gh = suggest_api_connector("https://github.com/acme/platform.git")
    assert gh == {"type": "github", "options": {"repo": "acme/platform"},
                  "label": "GitHub repo acme/platform"}
    # self-managed GitLab keeps its full group path + base_url
    gl = suggest_api_connector("https://gitlab.otxlab.net/zix/Development/secure-cloud/appriver.connector.git")
    assert gl["type"] == "gitlab"
    assert gl["options"] == {"project": "zix/Development/secure-cloud/appriver.connector",
                             "base_url": "https://gitlab.otxlab.net"}
    # scp form
    scp = suggest_api_connector("git@github.com:acme/platform.git")
    assert scp["type"] == "github" and scp["options"]["repo"] == "acme/platform"
    # GitHub Enterprise carries base_url
    ent = suggest_api_connector("https://github.acme.com/org/repo.git")
    assert ent["options"] == {"repo": "org/repo", "base_url": "https://github.acme.com"}


def test_suggest_api_connector_returns_none_for_unknown_hosts():
    from quickjoiner.connectors.git_repo import suggest_api_connector

    assert suggest_api_connector("https://git.company.com/team/thing.git") is None
    assert suggest_api_connector("https://bitbucket.org/team/repo.git") is None
    assert suggest_api_connector("") is None
    assert suggest_api_connector("not a url") is None


def test_pushed_branch_reads_the_ref_field():
    from quickjoiner.connectors.git_repo import pushed_branch

    assert pushed_branch({"ref": "refs/heads/main"}) == "main"
    assert pushed_branch({"ref": "refs/heads/feature/x"}) == "feature/x"
    assert pushed_branch({"ref": "refs/tags/v1.0.0"}) is None  # a tag push, not a branch
    assert pushed_branch({}) is None
    assert pushed_branch({"ref": ""}) is None


def test_git_connector_wants_resync_only_for_the_mapped_branch(tmp_path):
    from quickjoiner.config import SourceConfig
    from quickjoiner.connectors.registry import create_connector

    mapped = create_connector(
        SourceConfig(name="repo", type="git", options={"url": "https://x/y.git", "branch": "main"}),
        tmp_path,
    )
    assert mapped.wants_resync({"ref": "refs/heads/main"}) is True
    assert mapped.wants_resync({"ref": "refs/heads/other"}) is False
    assert mapped.wants_resync({"ref": "refs/tags/v1"}) is False  # not a branch push at all
    assert list(mapped.handle_event({"ref": "refs/heads/main"})) == []  # nothing yielded directly

    # No branch configured -> the connector tracks the remote's default, so ANY branch push
    # is potentially relevant (we can't know which branch is "default" from a webhook alone).
    unmapped = create_connector(
        SourceConfig(name="repo2", type="git", options={"url": "https://x/y.git"}), tmp_path,
    )
    assert unmapped.wants_resync({"ref": "refs/heads/whatever"}) is True


def test_github_pr_document():
    pr = {
        "number": 42,
        "title": "Add rate limiting",
        "state": "open",
        "html_url": "https://github.com/o/r/pull/42",
        "user": {"login": "alice"},
        "labels": [{"name": "backend"}],
        "head": {"ref": "feat/rl"},
        "base": {"ref": "main"},
        "updated_at": "2026-07-01T10:00:00Z",
        "body": "Adds a token bucket.",
    }
    doc = pr_document("o/r", pr)
    assert doc.kind == "ticket"
    assert "Add rate limiting" in doc.text and "alice" in doc.text and "token bucket" in doc.text
    assert doc.uri.endswith("/pull/42")


def test_github_issue_and_runs_documents():
    issue = {
        "number": 7, "title": "Flaky test", "state": "open",
        "html_url": "https://github.com/o/r/issues/7",
        "user": {"login": "bob"}, "labels": [], "updated_at": "2026-07-02T00:00:00Z",
        "body": None,
    }
    doc = gh_issue_document("o/r", issue)
    assert "Flaky test" in doc.title and "(no description)" in doc.text

    runs = [{"name": "CI", "head_branch": "main", "status": "completed",
             "conclusion": "failure", "run_number": 9, "updated_at": "2026-07-03T00:00:00Z"}]
    rdoc = runs_document("o/r", runs)
    assert rdoc.kind == "pipeline" and "failure" in rdoc.text


def test_jira_issue_document_with_adf_description():
    issue = {
        "key": "PAY-123",
        "fields": {
            "summary": "Migrate ledger to Postgres 16",
            "status": {"name": "In Progress"},
            "assignee": {"displayName": "Priya"},
            "issuetype": {"name": "Story"},
            "labels": ["db"],
            "updated": "2026-07-01T08:00:00.000+0000",
            "priority": {"name": "High"},
            "parent": {"key": "PAY-100"},
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Upgrade path notes."}]}
                ],
            },
        },
    }
    doc = jira_issue_document("https://acme.atlassian.net", issue)
    assert doc.uri == "https://acme.atlassian.net/browse/PAY-123"
    assert "In Progress" in doc.text and "Priya" in doc.text
    assert "Upgrade path notes." in doc.text
    assert "PAY-100" in doc.text
    # Display metadata (the document browser's Epic/Story/Task tree reads this) — id/
    # parent_id are Jira KEYS (strings), not numbers. team/sprint are intentionally blank:
    # Jira's board/sprint data lives in a separate Agile API this connector doesn't speak.
    assert doc.metadata["display"] == {
        "id": "PAY-123", "work_item_type": "Story", "state": "In Progress",
        "sprint": "", "team": "", "changed_date": "2026-07-01T08:00:00.000+0000",
        "closed_date": "", "parent_id": "PAY-100", "assigned_to": "Priya", "tags": ["db"],
    }


def test_jira_issue_document_asserts_related_to_edges_from_issuelinks():
    issue = {
        "key": "PAY-1",
        "fields": {
            "summary": "S",
            "issuelinks": [
                {"type": {"name": "Relates", "outward": "relates to"},
                 "outwardIssue": {"key": "PAY-2"}},
                {"type": {"name": "Blocks", "inward": "is blocked by"},
                 "inwardIssue": {"key": "PAY-3"}},
                {"type": {"name": "Relates"}},  # malformed — neither issue ref — skipped
            ],
        },
    }
    doc = jira_issue_document("https://acme.atlassian.net", issue)
    edges = {(s, r, d) for s, r, d, _ in doc.metadata["graph"]["edges"]}
    assert ("ticket:pay-1", "related_to", "ticket:pay-2") in edges
    assert ("ticket:pay-1", "related_to", "ticket:pay-3") in edges
    assert len(edges) == 3  # part_of->project + the 2 related_to (no edge for the malformed link)


def test_jira_sync_walks_up_to_fetch_a_missing_parent(tmp_path, monkeypatch):
    # PAY-1 was recently updated and points at parent PAY-100, which was NOT — without a
    # walk-up, PAY-100 (and the part_of edge pointing at it) would never reach memory.
    calls = []

    def fake_get_json(url, **kw):
        params = kw.get("params", {})
        calls.append(params.get("jql", ""))
        if "key in" in params.get("jql", ""):
            assert "PAY-100" in params["jql"]
            return {"issues": [{"key": "PAY-100", "fields": {"summary": "Epic"}}]}
        return {"issues": [{"key": "PAY-1", "fields": {"summary": "S", "parent": {"key": "PAY-100"}}}],
                "total": 1}

    monkeypatch.setattr("quickjoiner.connectors.jira.get_json", fake_get_json)
    conn = create_connector(SourceConfig(name="j", type="jira", options={"base_url": "https://x"}), tmp_path)
    docs = list(conn.sync({}))
    keys = {d.uri.rsplit("/", 1)[-1] for d in docs}
    assert keys == {"PAY-1", "PAY-100"}
    assert any("key in" in c for c in calls)  # the walk-up actually happened


def test_jira_get_issue_tool_returns_full_detail_with_links_and_comments(tmp_path, monkeypatch):
    def fake_get_json(url, **kw):
        assert url.endswith("/rest/api/2/issue/PAY-1")
        return {"fields": {
            "summary": "Migrate ledger", "status": {"name": "In Progress"},
            "assignee": {"displayName": "Priya"}, "issuetype": {"name": "Story"},
            "priority": {"name": "High"}, "labels": ["db"], "parent": {"key": "PAY-100"},
            "description": "Plain text description.",
            "issuelinks": [
                {"type": {"name": "Blocks", "outward": "blocks"}, "outwardIssue": {
                    "key": "PAY-2", "fields": {"summary": "Downstream job"}}},
            ],
            "comment": {"comments": [{"author": {"displayName": "Chen"}, "body": "LGTM"}]},
        }}

    monkeypatch.setattr("quickjoiner.connectors.jira.get_json", fake_get_json)
    conn = create_connector(SourceConfig(name="j", type="jira", options={"base_url": "https://x"}), tmp_path)
    tool = next(t for t in conn.tools() if t.spec.name.startswith("jira_get_issue"))
    out = tool.fn(issue_key="PAY-1")
    assert "Migrate ledger" in out and "Priya" in out and "High" in out and "PAY-100" in out
    assert "Plain text description." in out
    assert "blocks: PAY-2 - Downstream job" in out
    assert "Chen: LGTM" in out


def test_adf_flattening_nested_lists():
    adf = {
        "type": "doc",
        "content": [
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "item one"}]}
                ]},
            ]},
        ],
    }
    assert "item one" in _adf_to_text(adf)


def test_azure_devops_documents():
    item = {
        "id": 1001,
        "fields": {
            "System.WorkItemType": "Bug",
            "System.Title": "Nightly settlement job times out",
            "System.State": "Active",
            "System.AssignedTo": {"displayName": "Chen"},
            "System.AreaPath": "Payments\\Settlement",
            "System.IterationPath": "Sprint 42",
            "System.Tags": "prod",
            "System.ChangedDate": "2026-07-04T00:00:00Z",
            "System.Description": "<div>Job exceeds <b>30m</b> budget.</div>",
        },
    }
    doc = work_item_document("https://dev.azure.com/acme", item, team="Payments")
    assert doc.kind == "ticket"
    assert "Nightly settlement job times out" in doc.title
    assert "Chen" in doc.text and "30m" in doc.text and "<div>" not in doc.text
    assert not doc.metadata.get("graph")  # no relations -> no dev-link/hierarchy graph
    # Display metadata (the document browser's tree view reads this, not the graph) is
    # always attached for a ticket doc, independent of whether it has any relations.
    assert doc.metadata["display"] == {
        "id": 1001, "work_item_type": "Bug", "state": "Active", "team": "Payments",
        "sprint": "Sprint 42", "changed_date": "2026-07-04T00:00:00Z",
        "closed_date": "", "parent_id": None,
        "assigned_to": "Chen", "tags": ["prod"],
    }


def test_azure_devops_document_splits_multiple_tags_and_defaults_unassigned():
    item = {
        "id": 2002,
        "fields": {
            "System.WorkItemType": "Task",
            "System.Title": "T",
            "System.Tags": "prod; security ; needs-review",
        },
    }
    doc = work_item_document("https://dev.azure.com/acme", item)
    assert doc.metadata["display"]["tags"] == ["prod", "security", "needs-review"]
    assert doc.metadata["display"]["assigned_to"] == "unassigned"


def test_azure_devops_document_team_defaults_blank_for_walked_up_parents():
    # A Feature/Epic reached only by walking up the hierarchy has no single owning team
    # from that pull — must be left blank, never guessed.
    doc = work_item_document("https://dev.azure.com/acme",
                              {"id": 2, "fields": {"System.Title": "Parent feature"}})
    assert doc.metadata["display"]["team"] == ""


def test_ado_dev_link_graph_links_ticket_to_repo_and_branch():
    # A work item's Development links (its `relations`) tie the ticket to the code that
    # implements it. The repo GUID in each artifact URL is resolved to a name via repo_names,
    # so the edges key by NAME and merge with the GitLab connector's repo:<name> entity.
    repo_names = {"aaaa1111-bbbb": "AppRiver.Connector", "cccc2222-dddd": "AppRiver.Nautical"}
    item = {
        "id": 320753,
        "fields": {"System.Title": "Pre-seed usage data", "System.WorkItemType": "Product Backlog Item"},
        "relations": [
            {"rel": "ArtifactLink",
             "url": "vstfs:///Git/Ref/proj%2Faaaa1111-bbbb%2FGBfeature%2Fpre-seed",
             "attributes": {"name": "Branch"}},
            {"rel": "ArtifactLink",
             "url": "vstfs:///Git/Commit/proj%2Faaaa1111-bbbb%2Fdeadbeefcafe",
             "attributes": {"name": "Fixed in Commit"}},
            {"rel": "ArtifactLink",  # unknown repo GUID -> skipped, never guessed
             "url": "vstfs:///Git/Ref/proj%2Fzzzz9999-eeee%2FGBmain",
             "attributes": {"name": "Branch"}},
            {"rel": "Hyperlink", "url": "https://wiki/x"},  # not an artifact link -> ignored
        ],
    }
    g = dev_link_graph(item, repo_names)
    edges = {(s, r, d) for s, r, d, _ in g["edges"]}
    types = {e[0]: e[2] for e in g["entities"]}
    assert types["ticket:#320753"] == "ticket"
    assert types["repo:appriver.connector"] == "repo"
    assert types["branch:appriver.connector/feature/pre-seed"] == "branch"
    # ticket -> repo (branch + commit both resolve to the same repo edge, deduped)
    assert ("ticket:#320753", "implemented_in", "repo:appriver.connector") in edges
    # ticket -> branch, branch -> repo
    assert ("ticket:#320753", "on_branch", "branch:appriver.connector/feature/pre-seed") in edges
    assert ("branch:appriver.connector/feature/pre-seed", "belongs_to", "repo:appriver.connector") in edges
    # the unknown-GUID repo produced nothing
    assert not any("zzzz9999" in e[2] or "main" in e[2] for e in edges)


def test_ado_dev_link_graph_empty_without_git_links():
    assert dev_link_graph({"id": 1, "relations": []}, {"g": "Repo"}) == {}
    assert dev_link_graph({"id": 1}, {}) == {}
    # a PR artifact link still ties the ticket to its repo
    item = {"id": 5, "relations": [
        {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/proj%2Fg1%2F42",
         "attributes": {"name": "Pull Request"}}]}
    g = dev_link_graph(item, {"g1": "MyRepo"})
    assert ("ticket:#5", "implemented_in", "repo:myrepo") in {(s, r, d) for s, r, d, _ in g["edges"]}


def test_ado_work_item_document_attaches_dev_link_graph():
    item = {
        "id": 77, "fields": {"System.Title": "T"},
        "relations": [{"rel": "ArtifactLink",
                       "url": "vstfs:///Git/Ref/p%2Fg1%2FGBmain", "attributes": {"name": "Branch"}}],
    }
    doc = work_item_document("https://tfs/AppRiver", item, {"g1": "Widgets"})
    assert doc.metadata["graph"]["edges"]
    assert any(e[1] == "implemented_in" for e in doc.metadata["graph"]["edges"])


def test_workitem_id_from_url_parses_the_trailing_id():
    assert _workitem_id_from_url("https://dev.azure.com/acme/_apis/wit/workItems/12345") == 12345
    assert _workitem_id_from_url("https://dev.azure.com/acme/_apis/wit/workItems/12345/") == 12345
    assert _workitem_id_from_url("vstfs:///Git/Ref/p%2Fg1%2FGBmain") is None  # not a workItems url
    assert _workitem_id_from_url("") is None


def test_next_hierarchy_ids_walks_up_always_and_down_only_for_containers():
    # A Feature/Epic's real child list is what a completed one otherwise loses entirely —
    # walking down from it is what fixes that (reported live: a Feature with ~10 real
    # children only showed the 3 that independently happened to still be in a recent
    # sprint; another Feature with none recent enough was entirely absent).
    feature = {
        "id": 100,
        "fields": {"System.WorkItemType": "Feature"},
        "relations": [
            {"rel": "System.LinkTypes.Hierarchy-Reverse",
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/1"},  # its Epic
            {"rel": "System.LinkTypes.Hierarchy-Forward",
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/101"},  # a child story
            {"rel": "System.LinkTypes.Hierarchy-Forward",
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/102"},  # another
        ],
    }
    assert _next_hierarchy_ids(feature, seen=set()) == {1, 101, 102}

    # Already-seen ids are never re-requested.
    assert _next_hierarchy_ids(feature, seen={1, 101}) == {102}

    # A Story/Bug/Task is NOT walked down — only its parent matters. Walking down from
    # every leaf would reopen the flat-300k-item problem the sprint window exists to avoid.
    story = {
        "id": 200,
        "fields": {"System.WorkItemType": "Product Backlog Item"},
        "relations": [
            {"rel": "System.LinkTypes.Hierarchy-Reverse",
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/100"},
            {"rel": "System.LinkTypes.Hierarchy-Forward",
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/201"},  # a task — ignored
        ],
    }
    assert _next_hierarchy_ids(story, seen=set()) == {100}


def test_hierarchy_graph_asserts_part_of_and_related_to():
    item = {
        "id": 500,
        "relations": [
            {"rel": "System.LinkTypes.Hierarchy-Reverse",  # this item's PARENT
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/400",
             "attributes": {"name": "Parent"}},
            {"rel": "System.LinkTypes.Related",
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/501"},
            {"rel": "ArtifactLink",  # dev-link — not this function's concern
             "url": "vstfs:///Git/Ref/p%2Fg1%2FGBmain"},
        ],
    }
    g = hierarchy_graph(item)
    edges = {(s, r, d) for s, r, d, _ in g["edges"]}
    assert ("ticket:#500", "part_of", "ticket:#400") in edges
    assert ("ticket:#500", "related_to", "ticket:#501") in edges
    types = {e[0]: e[2] for e in g["entities"]}
    assert types["ticket:#500"] == types["ticket:#400"] == "ticket"  # uniform entity type


def test_hierarchy_graph_empty_without_hierarchy_or_related_links():
    assert hierarchy_graph({"id": 1, "relations": []}) == {}
    assert hierarchy_graph({"id": 1}) == {}
    # a lone ArtifactLink (dev-link) relation isn't this function's concern either
    assert hierarchy_graph({"id": 1, "relations": [{"rel": "ArtifactLink", "url": "vstfs:///Git/Ref/x"}]}) == {}


def test_merge_graphs_dedupes_entities_and_concatenates_edges():
    a = {"entities": [("ticket:#1", "#1", "ticket"), ("repo:x", "x", "repo")],
         "aliases": [], "edges": [("ticket:#1", "implemented_in", "repo:x", "d1")]}
    b = {"entities": [("ticket:#1", "#1", "ticket"), ("ticket:#2", "#2", "ticket")],
         "aliases": [], "edges": [("ticket:#1", "part_of", "ticket:#2", "d2")]}
    merged = _merge_graphs(a, b, {})
    assert len(merged["entities"]) == 3  # ticket:#1 deduped, not doubled
    assert len(merged["edges"]) == 2
    assert _merge_graphs({}, {}) == {}


def test_ado_work_item_document_merges_dev_link_and_hierarchy_graphs():
    # A work item can have BOTH a Development link and a hierarchy parent — both must
    # survive onto the document's single "graph" metadata block.
    item = {
        "id": 900, "fields": {"System.Title": "T"},
        "relations": [
            {"rel": "ArtifactLink", "url": "vstfs:///Git/Ref/p%2Fg1%2FGBmain"},
            {"rel": "System.LinkTypes.Hierarchy-Reverse",
             "url": "https://dev.azure.com/acme/_apis/wit/workItems/800"},
        ],
    }
    doc = work_item_document("https://dev.azure.com/acme", item, {"g1": "Widgets"})
    edges = {(s, r, d) for s, r, d, _ in doc.metadata["graph"]["edges"]}
    assert ("ticket:#900", "implemented_in", "repo:widgets") in edges
    assert ("ticket:#900", "part_of", "ticket:#800") in edges
    assert doc.metadata["display"]["parent_id"] == 800


def test_ado_select_recent_iterations_takes_last_started_sprints():
    iters = [
        {"id": "1", "name": "Sprint 1", "attributes": {"startDate": "2026-01-01T00:00:00Z", "timeFrame": "past"}},
        {"id": "3", "name": "Sprint 3", "attributes": {"startDate": "2026-03-01T00:00:00Z", "timeFrame": "current"}},
        {"id": "2", "name": "Sprint 2", "attributes": {"startDate": "2026-02-01T00:00:00Z", "timeFrame": "past"}},
        {"id": "f", "name": "Sprint 4", "attributes": {"startDate": "2026-04-01T00:00:00Z", "timeFrame": "future"}},
        {"id": "b", "name": "Backlog", "attributes": {}},  # no start date -> ignored
    ]
    picked = select_recent_iterations(iters, 2)
    # last 2 that have started (future Sprint 4 excluded, backlog excluded), in order
    assert [it["name"] for it in picked] == ["Sprint 2", "Sprint 3"]
    assert [it["name"] for it in select_recent_iterations(iters, 0)] == ["Sprint 1", "Sprint 2", "Sprint 3"]


def test_team_is_dormant_when_the_checked_window_is_old():
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    old_sprint = {"attributes": {"finishDate": "2025-01-15T00:00:00Z"}}  # well over a year back
    recent_sprint = {"attributes": {"finishDate": "2026-07-01T00:00:00Z"}}  # just a quiet sprint

    assert _team_is_dormant(old_sprint, 365, now) is True
    assert _team_is_dormant(recent_sprint, 365, now) is False
    # No iteration to judge from, or the check disabled -> never labelled dormant.
    assert _team_is_dormant(None, 365, now) is False
    assert _team_is_dormant(old_sprint, 0, now) is False
    # A malformed/missing finish date is treated as "can't tell", not "assume dormant".
    assert _team_is_dormant({"attributes": {}}, 365, now) is False
    assert _team_is_dormant({"attributes": {"finishDate": "not-a-date"}}, 365, now) is False


def test_ado_build_map_document_links_pipelines_to_repos():
    defs = [
        {"name": "AppRiver.SecureTide.API Publish", "repository": {"name": "AppRiver.SecureTide.API",
         "defaultBranch": "refs/heads/develop"}},
        {"name": "No-repo pipeline", "repository": {}},  # skipped
    ]
    doc = build_map_document("https://tfs.appriver.com/tfs/AppRiver", "AppRiver", defs)
    assert doc.kind == "pipeline"
    assert "AppRiver.SecureTide.API" in doc.text and "develop" in doc.text
    graph = doc.metadata["graph"]
    # a repo entity (matches the same-named GitLab repo) and a builds edge exist
    assert ("repo:appriver.securetide.api", "AppRiver.SecureTide.API", "repo") in graph["entities"]
    edges = graph["edges"]
    assert any(rel == "builds" and dst == "repo:appriver.securetide.api" for _s, rel, dst, _d in edges)


def test_ado_builds_document_shows_source_branch_and_result():
    builds = [
        {"definition": {"name": "AppRiver.ExampleApp CI"}, "sourceBranch": "refs/heads/feature/add_gitlab_ci",
         "repository": {"name": "AppRiver.ExampleApp"}, "result": "succeeded", "finishTime": "2026-07-16T10:00:00Z"},
    ]
    doc = builds_document("https://tfs.appriver.com/tfs/AppRiver", "AppRiver", builds)
    assert doc.kind == "pipeline"
    # short branch name (matches GitLab), repo, and outcome all surface
    assert "feature/add_gitlab_ci" in doc.text and "refs/heads" not in doc.text
    assert "AppRiver.ExampleApp" in doc.text and "succeeded" in doc.text
    assert "match GitLab" in doc.text


def test_confluence_page_document():
    from quickjoiner.connectors.confluence import page_document

    page = {
        "id": "98765",
        "title": "Incident response runbook",
        "body": {"view": {"value": "<h1>Sev1</h1><p>Page the on-call via PagerDuty.</p>"}},
        "version": {"when": "2026-06-30T00:00:00Z"},
        "_links": {"webui": "/spaces/ENG/pages/98765"},
    }
    doc = page_document("https://acme.atlassian.net/wiki", page)
    assert doc.title == "Incident response runbook"
    assert "PagerDuty" in doc.text and "<p>" not in doc.text
    assert doc.uri == "https://acme.atlassian.net/wiki/spaces/ENG/pages/98765"


def test_confluence_view_body_captures_rendered_user_mentions():
    # The bug this fixes: storage-format @mentions are empty <ri:user> elements that
    # strip to nothing, so a team roster ingested with BLANK names. body.view renders
    # each mention to the person's display name, so get_text captures it.
    from quickjoiner.connectors.confluence import page_document

    page = {
        "id": "5108498435",
        "title": "Caffeine - Team Charter",
        "body": {"view": {"value": (
            "<table><tr><th>Role</th><th>Name</th></tr>"
            '<tr><td>Team Lead</td><td><a class="user-mention">Kasper Vervaecke</a></td></tr>'
            "</table>"
        )}},
        "version": {"when": "2026-06-30T00:00:00Z"},
        "_links": {"webui": "/spaces/DEVKB/pages/5108498435/Caffeine"},
    }
    doc = page_document("https://acme.atlassian.net/wiki", page)
    assert "Kasper Vervaecke" in doc.text and "Team Lead" in doc.text


def test_confluence_page_document_falls_back_to_storage():
    # A webhook payload may carry only storage; page_document must still read it.
    from quickjoiner.connectors.confluence import page_document

    page = {
        "id": "1",
        "title": "P",
        "body": {"storage": {"value": "<p>fallback body</p>"}},
        "version": {"when": "2026-06-30T00:00:00Z"},
        "_links": {"webui": "/x"},
    }
    doc = page_document("https://acme.atlassian.net/wiki", page)
    assert "fallback body" in doc.text


def test_confluence_live_and_push_modes(tmp_path):
    connector = create_connector(
        SourceConfig(
            name="wiki",
            type="confluence",
            options={"base_url": "https://acme.atlassian.net", "spaces": ["ENG"]},
        ),
        tmp_path,
    )
    assert Mode.PUSH in connector.modes and Mode.LIVE in connector.modes
    assert [t.spec.name for t in connector.tools()] == ["confluence_search_wiki"]
    assert "ENG" in connector.tools()[0].spec.description  # space scoping surfaced to the agent

    # A webhook payload that carries the full storage body converts without HTTP.
    payload = {
        "page": {
            "id": "1",
            "title": "Deploy guide",
            "body": {"storage": {"value": "<p>Use Octopus on Fridays.</p>"}},
            "version": {"when": "2026-07-07T00:00:00Z"},
            "_links": {"webui": "/spaces/ENG/pages/1"},
        }
    }
    docs = list(connector.handle_event(payload))
    assert len(docs) == 1 and "Octopus" in docs[0].text
    assert docs[0].uri.endswith("/spaces/ENG/pages/1")

    assert list(connector.handle_event({})) == []  # unrecognized payloads are ignored


def test_azure_devops_live_tools(tmp_path):
    connector = create_connector(
        SourceConfig(
            name="ado",
            type="azure_devops",
            options={"organization": "acme", "project": "Payments"},
        ),
        tmp_path,
    )
    names = [t.spec.name for t in connector.tools()]
    assert set(names) == {"ado_get_work_item_ado", "ado_build_details_ado", "ado_build_log_ado",
                          "ado_list_repos_ado", "ado_list_pipelines_ado", "ado_list_commits_ado",
                          "ado_test_results_ado", "ado_query_work_items_ado", "ado_search_code_ado",
                          "ado_get_file_ado", "ado_build_status_ado"}


def _ado(tmp_path, **options):
    return create_connector(
        SourceConfig(name="ado", type="azure_devops", options=options), tmp_path
    )


def test_azure_devops_saas_url_defaults_unchanged(tmp_path):
    # Regression guard: existing SaaS configs keep the cloud hosts, verify, and api 7.0.
    c = _ado(tmp_path, organization="acme", project="Payments")
    assert c._org_url() == "https://dev.azure.com/acme"
    assert c._search_url() == "https://almsearch.dev.azure.com/acme"
    assert c._api() == "api-version=7.0"
    assert c._verify() is True


def test_azure_devops_onprem_url_construction(tmp_path):
    # On-prem Azure DevOps Server / TFS: {server_url}/{collection}, code search on the
    # same host (no almsearch.*), api-version overridable, TLS verify off for self-signed.
    c = _ado(
        tmp_path,
        server_url="https://tfs.appriver.com/tfs/",  # trailing slash tolerated
        collection="AppRiver",
        project="AppRiver",
        api_version="7.1",
        verify_tls="false",
    )
    assert c._org_url() == "https://tfs.appriver.com/tfs/AppRiver"
    assert c._search_url() == "https://tfs.appriver.com/tfs/AppRiver"  # same host, not almsearch
    assert c._api() == "api-version=7.1"
    assert c._verify() is False


def test_azure_devops_verify_tls_coercion(tmp_path):
    # Form values arrive as strings; only explicit false-y strings disable verification.
    assert _ado(tmp_path, organization="a", project="p", verify_tls="false")._verify() is False
    assert _ado(tmp_path, organization="a", project="p", verify_tls="False")._verify() is False
    assert _ado(tmp_path, organization="a", project="p", verify_tls="0")._verify() is False
    assert _ado(tmp_path, organization="a", project="p", verify_tls="true")._verify() is True
    assert _ado(tmp_path, organization="a", project="p")._verify() is True  # default


def test_azure_devops_test_requires_org_or_server(tmp_path):
    # Neither organization nor server_url -> a clear validation failure, no HTTP attempted.
    status = _ado(tmp_path, project="Payments").test()
    assert status.ok is False and "organization" in status.message and "server_url" in status.message


def test_github_webhook_event_to_document(tmp_path):
    connector = create_connector(
        SourceConfig(name="x", type="github", options={"repo": "o/r"}), tmp_path
    )
    payload = {
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 5, "title": "hook pr", "state": "open",
            "html_url": "https://github.com/o/r/pull/5",
            "user": {"login": "eve"}, "labels": [],
            "head": {"ref": "b"}, "base": {"ref": "main"},
            "updated_at": "2026-07-05T00:00:00Z", "body": "",
        },
    }
    docs = list(connector.handle_event(payload))
    assert len(docs) == 1 and "hook pr" in docs[0].title


def test_files_connector_shared_reader(tmp_path):
    from quickjoiner.connectors.files import read_file_document

    f = tmp_path / "readme.md"
    f.write_text("# Hello", encoding="utf-8")
    doc = read_file_document(f, tmp_path)
    assert doc is not None and doc.title == "readme.md" and doc.kind == "doc"

    binary = tmp_path / "img.png"
    binary.write_bytes(b"\x89PNG")
    assert read_file_document(binary, tmp_path) is None
