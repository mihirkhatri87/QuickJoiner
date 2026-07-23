from pathlib import Path

import pytest

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.azure_devops import (
    build_map_document,
    builds_document,
    select_recent_iterations,
    work_item_document,
)
from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.github import issue_document as gh_issue_document
from quickjoiner.connectors.github import pr_document, runs_document
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
    assert conn._control.stages and conn._control.stages[0][0] == "merge requests"


def test_resolve_secret_precedence(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "from-env-indirect")
    monkeypatch.setenv("FALLBACK_TOKEN", "from-fallback")
    assert resolve_secret({"token": "literal"}, "token", "FALLBACK_TOKEN") == "literal"
    assert resolve_secret({"token": "env:MY_TOKEN"}, "token", "FALLBACK_TOKEN") == "from-env-indirect"
    assert resolve_secret({}, "token", "FALLBACK_TOKEN") == "from-fallback"
    assert resolve_secret({}, "token") is None


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
    doc = work_item_document("https://dev.azure.com/acme", item)
    assert doc.kind == "ticket"
    assert "Nightly settlement job times out" in doc.title
    assert "Chen" in doc.text and "30m" in doc.text and "<div>" not in doc.text


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
        "body": {"storage": {"value": "<h1>Sev1</h1><p>Page the on-call via PagerDuty.</p>"}},
        "version": {"when": "2026-06-30T00:00:00Z"},
        "_links": {"webui": "/spaces/ENG/pages/98765"},
    }
    doc = page_document("https://acme.atlassian.net/wiki", page)
    assert doc.title == "Incident response runbook"
    assert "PagerDuty" in doc.text and "<p>" not in doc.text
    assert doc.uri == "https://acme.atlassian.net/wiki/spaces/ENG/pages/98765"


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
    assert names == ["ado_query_work_items_ado", "ado_search_code_ado", "ado_get_file_ado",
                     "ado_build_status_ado"]


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
