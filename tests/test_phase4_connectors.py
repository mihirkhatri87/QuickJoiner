"""Phase 4 connectors: gitlab, octopus, and the log-mining four (converters + events)."""

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.gitlab import (
    issue_document as gl_issue_document,
    mr_document,
    pipelines_document,
    push_document,
    wiki_document,
)
from quickjoiner.connectors.logsearch.datadog import monitor_document, webhook_document
from quickjoiner.connectors.logsearch.dynatrace import notification_document, problem_document
from quickjoiner.connectors.logsearch.elastic import indices_document
from quickjoiner.connectors.logsearch.grafana import alert_document, dashboard_document
from quickjoiner.connectors.octopus import (
    dashboard_document as octo_dashboard_document,
    event_document,
    project_document,
    releases_document,
)
from quickjoiner.connectors.registry import CONNECTOR_TYPES, _load_builtin_connectors, create_connector


def test_registry_knows_all_phase4_types(tmp_path):
    _load_builtin_connectors()
    for type_ in ("gitlab", "octopus", "grafana", "datadog", "dynatrace", "elastic"):
        assert type_ in CONNECTOR_TYPES, f"missing connector type {type_}"
    connector = create_connector(
        SourceConfig(name="gl", type="gitlab", options={"project": "grp/app"}), tmp_path
    )
    assert connector.source_id == "gitlab:gl"
    assert Mode.PULL in connector.modes and Mode.PUSH in connector.modes and Mode.LIVE in connector.modes


# -- gitlab --------------------------------------------------------------------

def test_gitlab_mr_document_api_shape():
    mr = {
        "iid": 12, "title": "Add retry budget", "state": "merged",
        "author": {"username": "priya"}, "source_branch": "feat/retry", "target_branch": "main",
        "labels": ["backend", "resilience"], "updated_at": "2026-07-01T10:00:00Z",
        "description": "Bounded exponential backoff.",
        "web_url": "https://gitlab.com/grp/app/-/merge_requests/12",
    }
    doc = mr_document("grp/app", mr)
    assert doc.kind == "ticket" and doc.uri.endswith("/merge_requests/12")
    assert "priya" in doc.text and "backend, resilience" in doc.text and "backoff" in doc.text


def test_gitlab_issue_document_webhook_label_shape():
    issue = {
        "iid": 7, "title": "Timeout on checkout", "state": "opened",
        "labels": [{"title": "bug"}], "updated_at": "2026-07-02T00:00:00Z",
        "description": None, "url": "https://gitlab.com/grp/app/-/issues/7",
    }
    doc = gl_issue_document("grp/app", issue)
    assert "bug" in doc.text and "(no description)" in doc.text
    assert doc.uri.endswith("/issues/7")


def test_gitlab_pipelines_wiki_and_push_documents():
    pdoc = pipelines_document(
        "grp/app",
        [{"id": 901, "ref": "main", "status": "failed", "updated_at": "2026-07-03T00:00:00Z"}],
        "https://gitlab.example.com",
    )
    assert pdoc.kind == "pipeline" and "failed" in pdoc.text
    assert pdoc.uri == "https://gitlab.example.com/grp/app/-/pipelines"

    wdoc = wiki_document("grp/app", {"slug": "onboarding", "title": "Onboarding", "content": "Start here."})
    assert wdoc.uri.endswith("/-/wikis/onboarding") and "Start here." in wdoc.text

    push = {
        "ref": "refs/heads/main",
        "user_name": "Chen",
        "commits": [{"id": "abcdef1234567890", "message": "fix: cap retries\n\ndetails", "author": {"name": "Chen"}}],
        "project": {"web_url": "https://gitlab.com/grp/app"},
    }
    pushdoc = push_document("grp/app", push)
    assert "abcdef12" in pushdoc.text and "fix: cap retries" in pushdoc.text
    assert pushdoc.uri == "https://gitlab.com/grp/app/-/commits/main"


def test_gitlab_webhook_events(tmp_path):
    connector = create_connector(
        SourceConfig(name="gl", type="gitlab", options={"project": "grp/app"}), tmp_path
    )
    mr_event = {
        "object_kind": "merge_request",
        "user": {"username": "eve"},
        "project": {"path_with_namespace": "grp/app"},
        "labels": [{"title": "hotfix"}],
        "object_attributes": {
            "iid": 3, "title": "hook mr", "state": "opened",
            "source_branch": "b", "target_branch": "main",
            "url": "https://gitlab.com/grp/app/-/merge_requests/3",
            "updated_at": "2026-07-05T00:00:00Z", "description": "",
        },
    }
    docs = list(connector.handle_event(mr_event))
    assert len(docs) == 1 and "hook mr" in docs[0].title
    assert "eve" in docs[0].text and "hotfix" in docs[0].text

    push_event = {
        "object_kind": "push",
        "ref": "refs/heads/main",
        "user_name": "Eve",
        "commits": [{"id": "1234567890ab", "message": "docs", "author": {"name": "Eve"}}],
        "project": {"path_with_namespace": "grp/app", "web_url": "https://gitlab.com/grp/app"},
    }
    docs = list(connector.handle_event(push_event))
    assert len(docs) == 1 and "push to main" in docs[0].title

    assert list(connector.handle_event({"object_kind": "note"})) == []


# -- octopus -------------------------------------------------------------------

def test_octopus_documents():
    server = "https://octopus.example.com"
    project = {"Id": "Projects-1", "Name": "Payments API", "Slug": "payments-api",
               "LifecycleId": "Lifecycles-1", "Description": "Card processing service."}
    pdoc = project_document(server, project)
    assert pdoc.kind == "deployment" and "Card processing" in pdoc.text
    assert pdoc.uri.endswith("/projects/payments-api")

    rdoc = releases_document(server, project, [
        {"Version": "2.4.1", "Assembled": "2026-07-01T09:00:00Z", "ReleaseNotes": "Hotfix for retries."},
    ])
    assert "2.4.1" in rdoc.text and "Hotfix" in rdoc.text

    ddoc = octo_dashboard_document(
        server,
        [{"ProjectId": "Projects-1", "EnvironmentId": "Environments-2",
          "ReleaseVersion": "2.4.1", "State": "Success", "CompletedTime": "2026-07-04T10:00:00Z"}],
        {"Projects-1": "Payments API"},
        {"Environments-2": "Production"},
    )
    assert "Payments API" in ddoc.text and "Production" in ddoc.text and "Success" in ddoc.text


def test_octopus_webhook_unwraps_subscription_payload(tmp_path):
    connector = create_connector(
        SourceConfig(name="oct", type="octopus", options={"server_url": "https://octopus.example.com"}),
        tmp_path,
    )
    payload = {
        "Payload": {
            "Event": {
                "Category": "DeploymentSucceeded",
                "Message": "Deploy Payments API release 2.4.1 to Production succeeded",
                "Occurred": "2026-07-04T10:00:00Z",
                "RelatedDocumentIds": ["Deployments-123"],
            }
        }
    }
    docs = list(connector.handle_event(payload))
    assert len(docs) == 1
    assert "DeploymentSucceeded" in docs[0].title and "2.4.1" in docs[0].text
    assert list(connector.handle_event({"no": "event"})) == []


def test_octopus_event_document_direct():
    doc = event_document("https://o.example.com", {"Category": "MachineUnhealthy", "Message": "m1 down", "Occurred": "2026-07-05T00:00:00Z"})
    assert doc.kind == "deployment" and "m1 down" in doc.text


def _octopus(tmp_path, **options):
    options.setdefault("server_url", "https://octopus.example.com")
    return create_connector(SourceConfig(name="oct", type="octopus", options=options), tmp_path)


class _FakeOctopus:
    """Records requested URLs and serves canned JSON keyed by substring match, choosing
    the LONGEST matching needle so specific routes (…/Projects-1/releases) win over
    general ones (/projects). Exercises the paging loop and incremental gating without a
    real Octopus server."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, headers=None, params=None, **kw):
        self.calls.append(url)
        best = None
        for needle, payload in self.routes:
            if needle in url and (best is None or len(needle) > len(best[0])):
                best = (needle, payload)
        return best[1] if best else {"Items": []}


def test_octopus_sync_paginates_all_projects(tmp_path, monkeypatch):
    # Two project pages via Page.Next -> both pages ingest (the >100 truncation fix).
    routes = [
        ("projects?skip=100", {"Items": [{"Id": "Projects-2", "Name": "Beta", "Slug": "beta"}], "Links": {}}),
        ("/projects", {
            "Items": [{"Id": "Projects-1", "Name": "Alpha", "Slug": "alpha"}],
            "Links": {"Page.Next": "/api/Spaces-1/projects?skip=100&take=100"},
        }),
        ("Projects-1/releases", {"Items": [{"Version": "1.0", "Assembled": "2026-07-01T00:00:00Z"}]}),
        ("Projects-2/releases", {"Items": [{"Version": "2.0", "Assembled": "2026-07-02T00:00:00Z"}]}),
        ("/dashboard", {"Items": []}),
        ("/environments", {"Items": []}),
    ]
    fake = _FakeOctopus(routes)
    monkeypatch.setattr("quickjoiner.connectors.octopus.get_json", fake)
    docs = list(_octopus(tmp_path).sync({}))
    titles = [d.title for d in docs]
    assert "Octopus project: Alpha" in titles and "Octopus project: Beta" in titles
    # Both projects' releases were fetched (full pull, no incremental).
    assert any("Projects-1/releases" in u for u in fake.calls)
    assert any("Projects-2/releases" in u for u in fake.calls)


def test_octopus_incremental_skips_unchanged_project_releases(tmp_path, monkeypatch):
    routes = [
        ("/projects", {
            "Items": [
                {"Id": "Projects-1", "Name": "Alpha", "Slug": "alpha"},
                {"Id": "Projects-2", "Name": "Beta", "Slug": "beta"},
            ], "Links": {},
        }),
        # only Projects-2 shows up in events since the watermark
        ("/events", {"Items": [{"RelatedDocumentIds": ["Projects-2", "Deployments-9"]}], "Links": {}}),
        ("Projects-2/releases", {"Items": [{"Version": "2.0", "Assembled": "2026-07-02T00:00:00Z"}]}),
        ("Projects-1/releases", {"Items": [{"Version": "1.0", "Assembled": "2026-07-01T00:00:00Z"}]}),
        ("/dashboard", {"Items": []}),
        ("/environments", {"Items": []}),
    ]
    fake = _FakeOctopus(routes)
    monkeypatch.setattr("quickjoiner.connectors.octopus.get_json", fake)
    conn = _octopus(tmp_path, incremental="true")
    docs = list(conn.sync({"since": "2026-07-10T00:00:00Z"}))

    titles = [d.title for d in docs]
    # Both project metadata docs still emitted; only the changed project's releases fetched.
    assert "Octopus project: Alpha" in titles and "Octopus project: Beta" in titles
    assert any("Projects-2/releases" in u for u in fake.calls)
    assert not any("Projects-1/releases" in u for u in fake.calls)
    assert "Octopus releases: Beta" in titles and "Octopus releases: Alpha" not in titles


def test_octopus_incremental_falls_back_to_full_without_watermark(tmp_path, monkeypatch):
    # First sync (no `since`) must do a full pull even with incremental on.
    routes = [
        ("/projects", {"Items": [{"Id": "Projects-1", "Name": "Alpha", "Slug": "alpha"}], "Links": {}}),
        ("Projects-1/releases", {"Items": [{"Version": "1.0", "Assembled": "2026-07-01T00:00:00Z"}]}),
        ("/dashboard", {"Items": []}),
        ("/environments", {"Items": []}),
    ]
    fake = _FakeOctopus(routes)
    monkeypatch.setattr("quickjoiner.connectors.octopus.get_json", fake)
    list(_octopus(tmp_path, incremental="true").sync({}))
    assert any("Projects-1/releases" in u for u in fake.calls)
    assert not any("/events" in u for u in fake.calls)  # no watermark -> no events probe


# -- log-mining four -----------------------------------------------------------

def test_grafana_documents_and_alert_event(tmp_path):
    dash = dashboard_document(
        "https://grafana.example.com",
        {"title": "Payments overview", "uid": "abc", "url": "/d/abc/payments",
         "tags": ["payments"], "folderTitle": "Team Payments"},
    )
    assert dash.kind == "dashboard" and "Team Payments" in dash.text
    assert dash.uri == "https://grafana.example.com/d/abc/payments"

    alert = alert_document(
        "https://grafana.example.com",
        {"title": "High error rate", "status": "firing",
         "alerts": [{"status": "firing", "labels": {"alertname": "PaymentsErrors"},
                     "annotations": {"summary": "5xx above 2%"}}]},
    )
    assert "PaymentsErrors" in alert.text and "5xx above 2%" in alert.text

    connector = create_connector(
        SourceConfig(name="graf", type="grafana", options={"base_url": "https://grafana.example.com"}),
        tmp_path,
    )
    assert len(list(connector.handle_event({"title": "x", "alerts": []}))) == 1
    # Loki tool only appears when a datasource uid is configured.
    assert len(connector.tools()) == 1
    with_loki = create_connector(
        SourceConfig(name="graf2", type="grafana",
                     options={"base_url": "https://grafana.example.com", "loki_datasource_uid": "P8E"}),
        tmp_path,
    )
    assert len(with_loki.tools()) == 2


def test_datadog_documents():
    mdoc = monitor_document(
        "https://app.datadoghq.com",
        {"id": 5, "name": "Payments error rate", "type": "metric alert",
         "overall_state": "Alert", "tags": ["service:payments"],
         "query": "avg(last_5m):sum:errors > 10", "message": "Page on-call."},
    )
    assert mdoc.kind == "dashboard" and "service:payments" in mdoc.text and "Page on-call." in mdoc.text

    wdoc = webhook_document(
        "https://app.datadoghq.com",
        {"title": "Payments error rate", "alert_type": "error", "priority": "P1", "body": "Threshold crossed"},
    )
    assert "Threshold crossed" in wdoc.text and "P1" in wdoc.text


def test_dynatrace_documents():
    pdoc = problem_document(
        "https://env.live.dynatrace.com",
        {"problemId": "P-1", "displayId": "P-1", "title": "Response time degradation",
         "status": "OPEN", "severityLevel": "PERFORMANCE", "impactLevel": "SERVICE",
         "affectedEntities": [{"name": "payments-svc"}]},
    )
    assert "payments-svc" in pdoc.text and "PERFORMANCE" in pdoc.text

    ndoc = notification_document(
        "https://env.live.dynatrace.com",
        {"ProblemTitle": "CPU saturation", "State": "OPEN", "ProblemImpact": "INFRASTRUCTURE",
         "ProblemSeverity": "RESOURCE", "ProblemDetailsText": "host-7 at 98% CPU",
         "ProblemURL": "https://env.live.dynatrace.com/#problems/problemdetails;pid=X"},
    )
    assert "host-7 at 98% CPU" in ndoc.text and ndoc.uri.endswith("pid=X")


def test_elastic_indices_document_filters_system_indices():
    doc = indices_document(
        "https://es.example.com",
        [
            {"index": "logs-payments-2026.07", "docs.count": "120000", "store.size": "2gb", "health": "green"},
            {"index": ".internal-system", "docs.count": "5", "store.size": "1kb", "health": "green"},
        ],
    )
    assert "logs-payments-2026.07" in doc.text
    assert ".internal-system" not in doc.text
