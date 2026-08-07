"""Phase 3 verification: FastAPI endpoints, SSE chat, webhook receivers, scheduler."""

from __future__ import annotations

import hashlib
import hmac
import json
import queue

import pytest
import yaml
from fastapi.testclient import TestClient

import quickjoiner.app as app_module
from quickjoiner.api.app import create_app
from quickjoiner.api.hooks import verify_signature
from quickjoiner.app import AppContext, build_context
from quickjoiner.config import RetrievalConfig, SourceConfig
from quickjoiner.llm.base import AgentTool, ChatResult, ToolCall, ToolSpec

from tests.conftest import FakeEmbedder
from tests.test_agent_loop import ScriptedProvider, _echo_tool

SECRET = "hook-secret-123"

ISSUE_PAYLOAD = {
    "issue": {
        "number": 42,
        "title": "Friday deploys are flaky",
        "state": "open",
        "html_url": "https://github.com/acme/api/issues/42",
        "user": {"login": "meena"},
        "labels": [],
        "body": "The Friday deploy fails intermittently on the smoke stage.",
        "updated_at": "2026-07-07T00:00:00Z",
    }
}


@pytest.fixture
def api_workspace(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "deploys.md").write_text(
        "# Deploys\nWe deploy with Octopus on Fridays.", encoding="utf-8"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "org": "acme",
                "sources": [
                    {
                        "name": "handbook",
                        "type": "files",
                        "options": {"path": str(docs)},
                        "sync_interval_minutes": 30,
                    },
                    {
                        "name": "ghrepo",
                        "type": "github",
                        "options": {"repo": "acme/api", "webhook_secret": SECRET},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    return ws


@pytest.fixture
def client(api_workspace):
    return TestClient(create_app(api_workspace))


def sse_events(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: ")]


def test_suggest_endpoint_returns_source_aware_starters(client):
    r = client.get("/api/suggest", params={"q": ""})
    assert r.status_code == 200
    suggestions = r.json()["suggestions"]
    assert isinstance(suggestions, list) and suggestions
    # the fixture has a github source -> a repository starter is offered
    assert any("repositor" in s.lower() for s in suggestions)


def test_suggest_endpoint_completes_typed_prefix(client):
    r = client.get("/api/suggest", params={"q": "what", "limit": 5})
    assert r.status_code == 200
    suggestions = r.json()["suggestions"]
    assert len(suggestions) <= 5
    assert all(isinstance(s, str) for s in suggestions)


def test_learn_endpoint_teaches_fact(client):
    r = client.post("/api/learn", json={"fact": "The payments guild owns nautical.",
                                        "topic": "nautical ownership"})
    assert r.status_code == 200
    assert "Remembered" in r.json()["result"]
    # This checks the note is live memory, not grounding-threshold behavior — bypass
    # min_score so it stays robust to retuning (FakeEmbedder's breadcrumb dilution
    # scores real content lower than bge-small would for the same text).
    client.patch("/api/settings", json={"retrieval": {"min_score": 0.0}})
    hits = client.get("/api/search", params={"q": "payments guild owns nautical"}).json()
    assert any(h["uri"].startswith("note://") for h in hits)  # taught note is live memory


def test_learn_endpoint_rejects_empty_fact(client):
    assert client.post("/api/learn", json={"fact": "   "}).status_code == 400


def test_scrape_endpoint_streams_report(client, monkeypatch):
    from quickjoiner.connectors.base import Document
    from quickjoiner.connectors.browser.scraper import WebScrapeConnector

    def fake_sync(self, state):
        assert self.options["max_depth"] == 2  # the request's depth reaches the crawler
        yield Document(uri="https://ex.test/", title="Home", text="welcome " * 30, kind="doc")
        yield Document(uri="https://ex.test/docs/", title="Docs", text="docs " * 30, kind="doc")

    monkeypatch.setattr(WebScrapeConnector, "sync", fake_sync)
    resp = client.post("/api/scrape", json={"url": "https://ex.test/", "depth": 2})
    assert resp.status_code == 200
    events = sse_events(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "status" and types[-1] == "done"
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["pages"] == 2
    assert "## Site structure" in answer["data"] and "```mermaid" in answer["data"]
    assert answer["path"].endswith(".md")  # report persisted under <workspace>/scrapes/


def test_scrape_endpoint_rejects_non_http_url(client):
    assert client.post("/api/scrape", json={"url": "ftp://nope"}).status_code == 400


def test_settings_graph_and_retrieval_roundtrip(client):
    # The retrieval knobs + graph.extract_triples exposed to the UI must round-trip
    # through PATCH -> persisted -> GET (they drive the Settings drawer toggles).
    r = client.patch(
        "/api/settings",
        json={
            "retrieval": {
                "hybrid": False,
                "reranker": "none",
                "graph_expansion": False,
                "contextual_chunks": False,
            },
            "graph": {"extract_triples": True},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["retrieval"]["reranker"] == "none"
    assert body["retrieval"]["graph_expansion"] is False
    assert body["retrieval"]["contextual_chunks"] is False
    assert body["graph"]["extract_triples"] is True
    # A fresh GET reflects the persisted values.
    got = client.get("/api/settings").json()
    assert got["graph"]["extract_triples"] is True
    assert got["retrieval"]["reranker"] == "none"


def test_settings_change_reaches_the_live_pipeline_not_just_the_saved_config(api_workspace):
    """Reported live: `graph.extract_triples` was toggled on in Settings, a clean re-sync run,
    and the graph still came back with only the deterministic edges. The setting HAD saved —
    but `build_context` wires the triple extractor into the pipeline once at process start,
    so the running pipeline kept the extractor it was born with (None) until a restart. A
    settings write must re-derive the objects built from config, not just persist them."""
    ctx = build_context(api_workspace)
    client = TestClient(create_app(api_workspace, ctx=ctx))
    assert ctx.pipeline.extracts_triples is False
    assert client.get("/api/graph/pending").json()["extraction_enabled"] is False

    assert client.patch("/api/settings", json={"graph": {"extract_triples": True}}).status_code == 200
    assert ctx.pipeline.extracts_triples is True  # same process, no restart
    assert client.get("/api/graph/pending").json()["extraction_enabled"] is True

    # Query-side retrieval knobs live on the store object and must move too.
    client.patch("/api/settings", json={"retrieval": {"hybrid": False, "reranker": "none"}})
    assert ctx.store._retrieval.hybrid is False
    assert ctx.store._reranker is None

    # Turning it back off must also take effect immediately, not strand the extractor on.
    client.patch("/api/settings", json={"graph": {"extract_triples": False}})
    assert ctx.pipeline.extracts_triples is False


def test_settings_defaults_are_shipped_values_not_current_ones(client):
    """The Settings drawer marks which fields still sit at their default. The defaults must
    come from fresh config models — reading back the *saved* config would make every field
    look default forever, which is exactly the bug this endpoint exists to avoid."""
    before = client.get("/api/settings/defaults").json()
    assert before["retrieval"]["min_score"] == RetrievalConfig().min_score
    assert set(before) == {"llm", "embedding", "retrieval", "chat", "graph", "repos"}

    client.patch("/api/settings", json={"retrieval": {"min_score": 0.81}})
    after = client.get("/api/settings/defaults").json()
    assert after["retrieval"]["min_score"] == before["retrieval"]["min_score"]  # unmoved
    assert client.get("/api/settings").json()["retrieval"]["min_score"] == 0.81  # current did move


def test_settings_rejects_bad_retrieval(client):
    assert client.patch("/api/settings", json={"retrieval": {"min_score": "high"}}).status_code == 400


def test_settings_repos_autogen_roundtrip(client):
    # The auto-AGENTS.md toggle must round-trip through PATCH -> persisted -> GET
    # (it drives the Settings drawer "auto-generate architecture brief on sync" switch).
    assert client.get("/api/settings").json()["repos"]["auto_agents_md"] is False
    r = client.patch("/api/settings", json={"repos": {"auto_agents_md": True}})
    assert r.status_code == 200 and r.json()["repos"]["auto_agents_md"] is True
    assert client.get("/api/settings").json()["repos"]["auto_agents_md"] is True


def test_llm_test_endpoint_ok(client, monkeypatch):
    # /api/llm/test probes the provider with a one-token round-trip using unsaved
    # form overrides; a scripted provider stands in for a reachable backend.
    import quickjoiner.llm as llm_mod

    monkeypatch.setattr(llm_mod, "create_provider", lambda cfg: ScriptedProvider([ChatResult(text="OK")]))
    r = client.post("/api/llm/test", json={"llm": {"provider": "ollama", "model": "qwen3:4b"}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["model"] == "qwen3:4b"
    assert "OK" in body["message"]


def test_llm_test_endpoint_reports_failure(client, monkeypatch):
    import quickjoiner.llm as llm_mod

    def boom(cfg):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(llm_mod, "create_provider", boom)
    r = client.post("/api/llm/test", json={"llm": {"provider": "litellm"}})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "connection refused" in r.json()["message"]


def test_graph_endpoint_resolves_and_snapshots(client):
    # Teaching a fact that mentions a ticket key populates the graph via the
    # pipeline's ticket extractor — the endpoint then resolves it by name.
    client.post("/api/learn", json={"fact": "PAY-42 tracks the nautical checkout rewrite."})
    r = client.get("/api/graph", params={"entity": "PAY-42"})
    assert r.status_code == 200
    data = r.json()
    assert data["entity"]["type"] == "ticket"
    ref = next(e for e in data["edges"] if e["rel"] == "references")
    assert ref["evidence"]["title"]  # edges stay citable
    assert client.get("/api/graph").json()["edges"]  # unfiltered snapshot
    assert client.get("/api/graph", params={"entity": "warp-drive"}).status_code == 404


def test_graph_search_and_bridges_endpoints(client):
    client.post("/api/learn", json={"fact": "PET-25 tracks the nautical billing migration."})
    r = client.get("/api/graph/search", params={"q": "pet-25"})
    assert r.status_code == 200
    assert any(row["type"] == "ticket" for row in r.json())
    assert client.get("/api/graph/search", params={"q": "zzz-nothing"}).json() == []

    # A single taught fact only ever has one evidence source -> no bridges yet.
    assert client.get("/api/graph/bridges").json() == []


def test_document_file_endpoint_serves_local_repo_file_and_404s_otherwise(client, api_workspace):
    from quickjoiner.memory.catalog import Catalog

    cat = Catalog(api_workspace)
    repo_file = api_workspace / "repos" / "Connector" / "AppRiver.Connector.Web" / "appsettings.json"
    repo_file.parent.mkdir(parents=True)
    repo_file.write_text('{"Logging": {}}', encoding="utf-8")
    cat.upsert_document(
        doc_id="doc-with-local-file", source_id="git:Connector",
        uri="https://gitlab.example/x/connector.git::AppRiver.Connector.Web/appsettings.json",
        title="Connector/AppRiver.Connector.Web/appsettings.json", kind="doc",
        content_hash="h1", updated_at=None, chunk_count=1,
    )
    cat.upsert_document(
        doc_id="doc-no-local-file", source_id="confluence:eng",
        uri="https://x.atlassian.net/wiki/spaces/eng/1", title="Some wiki page", kind="doc",
        content_hash="h2", updated_at=None, chunk_count=1,
    )
    cat.close()

    r = client.get("/api/documents/doc-with-local-file/file")
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == '{"Logging": {}}'
    assert body["title"] == "Connector/AppRiver.Connector.Web/appsettings.json"

    assert client.get("/api/documents/doc-no-local-file/file").status_code == 404
    assert client.get("/api/documents/does-not-exist/file").status_code == 404


def test_graph_path_endpoint(client):
    client.post("/api/learn", json={"fact": "OPS-9 tracks the gateway migration."})
    r = client.get("/api/graph/path", params={"a": "OPS-9", "b": "user-taught"})
    assert r.status_code == 200
    path = r.json()["path"]
    assert len(path) == 1 and path[0]["rel"] == "references"
    assert path[0]["evidence"]["kind"] != "code"  # a taught note, not a code file
    assert client.get("/api/graph/path", params={"a": "OPS-9", "b": "nope"}).status_code == 404


def test_gaps_endpoints_list_and_resolve(client, api_workspace):
    # Seed refusals directly on the shared catalog file, then read them via the API.
    from quickjoiner.memory.catalog import Catalog

    cat = Catalog(api_workspace)
    cat.log_gap("how do we deploy with octopus", 0.3,
                [{"source_id": "files:handbook", "title": "Deploys", "score": 0.3}])
    cat.log_gap("how do we deploy with octopus", 0.3, [])
    cat.close()

    data = client.get("/api/gaps").json()
    assert data["open_count"] == 2
    assert len(data["clusters"]) == 1
    cluster = data["clusters"][0]
    assert cluster["count"] == 2 and "octopus" in cluster["suggested_connectors"]

    rr = client.post("/api/gaps/resolve",
                     json={"gap_ids": cluster["gap_ids"], "resolution": "connected:octopus"})
    assert rr.status_code == 200 and rr.json()["resolved"] == 2
    assert client.get("/api/gaps").json()["open_count"] == 0


def test_gaps_privacy_mode_hides_query(client, api_workspace):
    from quickjoiner.memory.catalog import Catalog

    cat = Catalog(api_workspace)
    cat.log_gap("confidential question about payroll numbers", 0.1, [], store_query=False)
    cat.close()
    data = client.get("/api/gaps").json()
    assert data["open_count"] == 1
    assert "payroll" not in json.dumps(data)  # query text never leaves in hash-only mode


def github_sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# -- basic endpoints ---------------------------------------------------------

def test_health_and_status(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["org"] == "acme"

    status = client.get("/api/status").json()
    assert status["llm"]["provider"] == "anthropic"
    assert status["stats"]["documents"] == 0


def test_index_serves_ui(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


def _wait_sync(client, name, timeout=15.0):
    """Poll the async sync job until it finishes; return its final summary."""
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        syncs = client.get("/api/syncs").json()["syncs"]
        job = next((s for s in syncs if s["source"] == name), None)
        if job and job["state"] in ("done", "error", "stopped"):
            return job
        _t.sleep(0.05)
    raise AssertionError(f"sync for {name!r} did not finish in {timeout}s")


def test_sync_then_sources_and_search(client):
    resp = client.post("/api/sync/handbook")
    assert resp.status_code == 200
    assert resp.json()["job"]["source"] == "handbook"  # async job started
    job = _wait_sync(client, "handbook")
    assert job["state"] == "done" and job["stats"]["added"] == 1

    rows = client.get("/api/sources").json()
    handbook = next(r for r in rows if r["name"] == "handbook")
    assert handbook["documents"] == 1 and handbook["configured"] is True

    # This checks the synced doc is searchable memory, not grounding-threshold behavior —
    # bypass min_score so it stays robust to retuning (see test_learn_endpoint_teaches_fact).
    # It matters more here than it looks: contextual chunking puts the document's **uri** in
    # the chunk's breadcrumb, and under FakeEmbedder's 32-bucket hash that random pytest
    # tmp path shifts the cosine by ±0.06 run to run — straddling the 0.64 gate, so the
    # assertion was a coin flip on the temp directory's name.
    client.patch("/api/settings", json={"retrieval": {"min_score": 0.0}})
    hits = client.get("/api/search", params={"q": "We deploy with Octopus on Fridays."}).json()
    assert hits and hits[0]["uri"].startswith("file://")
    assert "Octopus" in hits[0]["text"]

    # Second sync is idempotent: nothing re-added.
    assert client.post("/api/sync/handbook").status_code == 200
    again = _wait_sync(client, "handbook")
    assert again["stats"]["added"] == 0


def test_clean_resync_purges_then_repopulates(client):
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    assert next(r for r in client.get("/api/sources").json() if r["name"] == "handbook")["documents"] == 1
    # a clean resync purges first, then re-adds the same doc from scratch
    assert client.post("/api/sync/handbook", params={"clean": "true"}).status_code == 200
    job = _wait_sync(client, "handbook")
    assert job["state"] == "done" and job["stats"]["added"] == 1  # re-added, not "unchanged"
    assert next(r for r in client.get("/api/sources").json() if r["name"] == "handbook")["documents"] == 1


def test_notifications_report_finished_syncs(client):
    """The activity feed behind the bell menu: empty before anything runs, then one row
    per run carrying the outcome — read back from the persisted history, so it is still
    there after a page reload (and, unlike /api/syncs, after a server restart)."""
    assert client.get("/api/notifications").json() == {"notifications": [], "active": 0}

    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")

    body = client.get("/api/notifications").json()
    assert body["active"] == 0
    assert len(body["notifications"]) == 1
    row = body["notifications"][0]
    assert row["source"] == "handbook" and row["state"] == "done"
    assert row["stats"]["added"] == 1 and row["started_at"]

    # A second run is its own row, newest first — the feed is history, not a status dict.
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    rows = client.get("/api/notifications").json()["notifications"]
    assert len(rows) == 2
    assert rows[0]["started_at"] >= rows[1]["started_at"]
    assert len({r["id"] for r in rows}) == 2


def test_notifications_window_is_clamped(client):
    """hours is user input: absurd values must not turn into an unbounded scan."""
    assert client.get("/api/notifications", params={"hours": 0}).status_code == 200
    assert client.get("/api/notifications", params={"hours": 100000}).status_code == 200


def _wait_job(client, name, timeout=15.0):
    """Poll until no job (of any kind) is still running for this source."""
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        jobs = client.get("/api/syncs").json()["syncs"]
        job = next((s for s in jobs if s["source"] == name), None)
        if job and job["state"] not in ("running", "stopping"):
            return job
        _t.sleep(0.05)
    raise AssertionError(f"job for {name!r} did not finish in {timeout}s")


def test_cleanup_forgets_a_connectors_knowledge_but_keeps_it_configured(client):
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    assert next(r for r in client.get("/api/sources").json() if r["name"] == "handbook")["documents"] == 1

    job = client.post("/api/connectors/handbook/cleanup").json()["job"]
    assert job["kind"] == "cleanup"
    assert _wait_job(client, "handbook")["state"] == "done"

    assert client.get("/api/status").json()["stats"]["documents"] == 0  # knowledge forgotten
    assert any(c["name"] == "handbook" for c in client.get("/api/connectors").json())  # config kept
    # …and it stays a listed, connected system with zero documents — like a connector
    # that has been added but not yet synced. (It vanishing here was a real bug.)
    row = next((r for r in client.get("/api/sources").json() if r["name"] == "handbook"), None)
    assert row is not None and row["documents"] == 0 and row["configured"] is True
    # …and it can be re-synced from scratch, since the watermark went with it.
    client.post("/api/sync/handbook")
    assert _wait_sync(client, "handbook")["stats"]["added"] == 1


def test_delete_connector_cleans_up_its_knowledge_by_default(client):
    """A deleted connector's documents are unreachable — nothing can re-sync or purge them
    — so deletion purges by default rather than stranding them in memory."""
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    assert client.get("/api/status").json()["stats"]["documents"] == 1

    body = client.delete("/api/connectors/handbook").json()
    assert body["removed"] == "handbook" and body["job"]["kind"] == "cleanup"
    assert _wait_job(client, "handbook")["state"] == "done"

    assert client.get("/api/status").json()["stats"]["documents"] == 0
    assert not any(c["name"] == "handbook" for c in client.get("/api/connectors").json())
    assert not any(r["name"] == "handbook" for r in client.get("/api/sources").json())


def test_delete_connector_can_keep_memory_explicitly(client):
    """The opt-out: retire the connector, keep what it taught (the pre-2026-07-20 default)."""
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")

    body = client.delete("/api/connectors/handbook", params={"keep_memory": "true"}).json()
    assert body["job"] is None
    assert client.get("/api/status").json()["stats"]["documents"] == 1  # knowledge survives
    assert not any(c["name"] == "handbook" for c in client.get("/api/connectors").json())


def test_delete_connector_refuses_while_a_job_runs(client, monkeypatch):
    """Purging under a live ingest would race it."""
    import quickjoiner.sync_manager as sm

    monkeypatch.setattr(sm.SyncManager, "is_running", lambda self, name: True)
    assert client.delete("/api/connectors/handbook").status_code == 409
    assert any(c["name"] == "handbook" for c in client.get("/api/connectors").json())  # not removed


def test_pause_and_resume_roundtrip(client):
    """A running sync can be paused and resumed via the API; the state is reflected in the
    job summary and the activity feed, and it still finishes after resume."""
    r = client.post("/api/sync/handbook")
    assert r.status_code == 200
    # Pause may race the (fast, in-test) sync to completion; both outcomes are valid, but
    # if we DO catch it running, pause→resume must round-trip.
    p = client.post("/api/sync/handbook/pause")
    if p.status_code == 200:
        assert p.json()["job"]["state"] == "paused"
        # A paused job blocks a second start and a delete.
        assert client.post("/api/sync/handbook").status_code == 409
        assert client.delete("/api/connectors/handbook").status_code == 409
        assert client.post("/api/sync/handbook/resume").json()["job"]["state"] == "running"
    else:
        assert p.status_code == 409  # already finished
    job = _wait_sync(client, "handbook")
    assert job["state"] == "done"


def test_pause_nothing_running_is_409(client):
    assert client.post("/api/sync/handbook/pause").status_code == 409
    assert client.post("/api/sync/handbook/resume").status_code == 409


def test_reset_memory_wipes_knowledge_keeps_connectors(client):
    """Full-stack global reset: ingest, then reset → 0 documents/graph, connectors intact."""
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    assert client.get("/api/status").json()["stats"]["documents"] >= 1

    r = client.post("/api/memory/reset")
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["kind"] == "reset" and job["source"] == "all memory"
    # It runs as a background job — wait for it, then verify the wipe. It also lands in the
    # activity feed + streams logs (the observability the synchronous version lacked).
    done = _wait_job(client, "all memory")
    assert done["state"] == "done"
    assert any(n["kind"] == "reset" for n in client.get("/api/notifications").json()["notifications"])
    events = sse_events(client.get("/api/sync/all memory/logs").text)
    assert any(e.get("type") == "log" and "reset complete" in e.get("line", "") for e in events)

    st = client.get("/api/status").json()["stats"]
    assert st["documents"] == 0 and st["chunks"] == 0  # vectors gone too (store.reset)
    # Connectors remain configured and listed (at 0 docs), so they can be re-synced.
    names = [c["name"] for c in client.get("/api/connectors").json()]
    assert "handbook" in names and "ghrepo" in names
    hb = next(r for r in client.get("/api/sources").json() if r["name"] == "handbook")
    assert hb["documents"] == 0 and hb["configured"] is True
    # And search finds nothing now.
    assert client.get("/api/search", params={"q": "Octopus"}).json() == []

    # Re-sync repopulates from scratch (watermark was cleared).
    client.post("/api/sync/handbook")
    assert _wait_sync(client, "handbook")["stats"]["added"] == 1


def test_graph_pending_and_drain_endpoints(api_workspace):
    """AI #25: the queue is visible, and draining it runs as a real background job."""
    ctx = build_context(api_workspace)  # explicit ctx so the test can wire its extractor
    client = TestClient(create_app(api_workspace, ctx=ctx))
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    doc_id = ctx.catalog.documents_for_source("files:handbook")[0]["doc_id"]

    empty = client.get("/api/graph/pending").json()
    assert empty["total"] == 0 and empty["extraction_enabled"] is False
    # With extraction off there is nothing a drain could resolve, so it refuses outright
    # instead of quietly clearing the queue.
    assert client.post("/api/graph/drain").status_code == 409

    ctx.catalog.mark_graph_pending(doc_id, "files:handbook")
    listed = client.get("/api/graph/pending").json()
    assert listed["total"] == 1
    assert listed["by_source"] == [{"source_id": "files:handbook", "count": 1}]

    calls: list[str] = []
    ctx.config.graph.triple_min_chars = 1  # the fixture's doc is a couple of lines long
    ctx.pipeline._triple_extractor = lambda text, title, guidance="": calls.append(title) or []
    r = client.post("/api/graph/drain")
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["kind"] == "drain" and job["source"] == "graph relationships"
    done = _wait_job(client, "graph relationships")
    assert done["state"] == "done" and done["stats"]["documents"] == 1
    assert calls == ["deploys.md"]  # the queued document's text was re-read and mined
    assert client.get("/api/graph/pending").json()["total"] == 0


def test_drain_refuses_while_a_job_runs(api_workspace, monkeypatch):
    """It rewrites edges across sources, so it must not race a sync of one of them."""
    import quickjoiner.sync_manager as sm

    ctx = build_context(api_workspace)
    ctx.pipeline._triple_extractor = lambda text, title: []
    client = TestClient(create_app(api_workspace, ctx=ctx))
    monkeypatch.setattr(sm.SyncManager, "active_sources", lambda self: ["handbook"])
    assert client.post("/api/graph/drain").status_code == 409


def test_graph_rebuild_preview_reports_what_cannot_be_rebuilt_in_full(client):
    """A rebuild skips the fetch, and a connector's structural claims are computed DURING
    the fetch — so the preview says up front how many documents can only be preserved."""
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    preview = client.get("/api/graph/rebuild/preview").json()
    assert preview["documents"] > 0
    assert "missing_payload" in preview and "extraction_enabled" in preview

    scoped = client.get("/api/graph/rebuild/preview?source_id=files:handbook").json()
    assert scoped["documents"] > 0
    assert client.get("/api/graph/rebuild/preview?source_id=files:nope").json()["documents"] == 0


def test_graph_rebuild_runs_as_a_job_without_re_embedding(client):
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    before = client.get("/api/status").json()["stats"]

    r = client.post("/api/graph/rebuild")
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["kind"] == "regraph" and job["source"] == "knowledge graph"
    done = _wait_job(client, "knowledge graph")
    assert done["state"] == "done"
    assert done["stats"]["documents"] > 0
    # The whole point: documents and chunks are untouched — only edges were rebuilt.
    assert client.get("/api/status").json()["stats"] == before


def test_graph_rebuild_with_triples_refuses_when_extraction_is_off(client):
    """The deterministic rebuild needs no LLM, so only the opt-in half is gated."""
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    assert client.post("/api/graph/rebuild?with_triples=true").status_code == 409
    assert client.post("/api/graph/rebuild?with_triples=false").status_code == 200


def test_graph_rebuild_refuses_while_a_job_runs(api_workspace, monkeypatch):
    """It replaces edges for documents across sources, so it must not race a sync."""
    import quickjoiner.sync_manager as sm

    client = TestClient(create_app(api_workspace))
    monkeypatch.setattr(sm.SyncManager, "active_sources", lambda self: ["handbook"])
    assert client.post("/api/graph/rebuild").status_code == 409


def test_reset_memory_refuses_while_a_job_runs(client, monkeypatch):
    """Reset clears every source, so it must not race an in-flight job."""
    import quickjoiner.sync_manager as sm

    monkeypatch.setattr(sm.SyncManager, "active_sources", lambda self: ["handbook"])
    assert client.post("/api/memory/reset").status_code == 409


def test_sync_unknown_source_is_404(client):
    assert client.post("/api/sync/nope").status_code == 404


def test_rename_connector_before_first_sync(client):
    """A connector with 0 documents can be renamed (nothing is keyed to the old name yet);
    it stays listed under the new name and is gone under the old one."""
    r = client.patch("/api/connectors/handbook", json={"name": "playbook"})
    assert r.status_code == 200 and r.json()["name"] == "playbook"
    names = [c["name"] for c in client.get("/api/connectors").json()]
    assert "playbook" in names and "handbook" not in names
    # And it syncs fine under the new name.
    client.post("/api/sync/playbook")
    assert _wait_sync(client, "playbook")["stats"]["added"] == 1


def test_rename_connector_rejected_after_sync(client):
    """Once it has learned documents, the name keys them — renaming is refused."""
    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")
    r = client.patch("/api/connectors/handbook", json={"name": "playbook"})
    assert r.status_code == 409 and "before the first sync" in r.json()["detail"]
    assert any(c["name"] == "handbook" for c in client.get("/api/connectors").json())  # unchanged


def test_rename_connector_to_taken_name_conflicts(client):
    assert client.patch("/api/connectors/handbook", json={"name": "ghrepo"}).status_code == 409


def test_aka_editable_before_first_sync_locked_after(client):
    """`aka` (lock_after_sync in FORM_SPECS) follows the connector-name rule: free to set
    while 0 documents, 409 once anything has been ingested — an alias removal could not be
    applied consistently after the fact."""
    r = client.patch("/api/connectors/handbook", json={"options": {"aka": "playbook, the-handbook"}})
    assert r.status_code == 200 and r.json()["options"]["aka"] == "playbook, the-handbook"

    client.post("/api/sync/handbook")
    _wait_sync(client, "handbook")

    # Changing it after the sync is refused…
    r = client.patch("/api/connectors/handbook", json={"options": {"aka": "different"}})
    assert r.status_code == 409 and "before the first sync" in r.json()["detail"]
    # …but the edit form re-sending the UNCHANGED value (it submits every field, possibly
    # re-formatted as a list) must still save fine.
    assert client.patch("/api/connectors/handbook",
                        json={"options": {"aka": ["the-handbook", "playbook"]}}).status_code == 200
    # And the declared alias actually resolved to the source entity during the sync.
    hits = client.get("/api/graph/search", params={"q": "playbook"}).json()
    assert any(h["id"] == "repo:handbook" for h in hits)


def test_patch_connector_edits_options_schedule_and_merges(client):
    """The edit-connector modal saves via PATCH options: update fields, keep a secret
    sent back as its mask, preserve untouched fields, set the schedule, and remove a
    field cleared to empty."""
    client.patch("/api/connectors/ghrepo", json={"options": {"token": "sekret"}})  # a real secret
    resp = client.patch(
        "/api/connectors/ghrepo",
        json={"options": {"repo": "acme/renamed", "base_url": "https://ghe.acme/api/v3", "token": "•••"},
              "sync_interval_minutes": 120},
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["options"]["repo"] == "acme/renamed"
    assert row["options"]["base_url"] == "https://ghe.acme/api/v3"
    assert row["sync_interval_minutes"] == 120
    assert row["options"]["token"] == "•••"        # secret round-trips as its mask, not wiped
    assert row["options"]["webhook_secret"]        # an untouched field is preserved by the merge

    # empty value removes just that field; the masked secret is not deleted
    client.patch("/api/connectors/ghrepo", json={"options": {"base_url": "", "token": "•••"}})
    again = next(r for r in client.get("/api/connectors").json() if r["name"] == "ghrepo")
    assert again["options"]["repo"] == "acme/renamed"
    assert "base_url" not in again["options"]
    assert again["options"]["token"] == "•••"


# -- SSE chat ----------------------------------------------------------------

def test_chat_streams_tool_calls_and_answer(client, monkeypatch):
    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None, sources=None, user=None, role=None, scope=None):
        from quickjoiner.agent.agent import OnboardingAgent

        provider = ScriptedProvider(
            [
                ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={"value": "x"})]),
                ChatResult(text="grounded answer"),
            ]
        )
        return OnboardingAgent(provider, [_echo_tool()], system="sys")

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)

    resp = client.post("/api/chat", json={"message": "how do we deploy?"})
    assert resp.status_code == 200
    events = sse_events(resp.text)
    types = [e["type"] for e in events]
    # ScriptedProvider simulates streaming, so the final text also arrives as a delta.
    assert types == ["tool_call", "delta", "answer", "done"]
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["data"] == "grounded answer"
    assert answer["session_id"]  # returned so the client can continue the session


def test_chat_forwards_candidates_event(client, monkeypatch):
    """The chat SSE stream forwards a `candidates` event verbatim, between the
    streamed deltas and the final answer (plan 06 §C — no endpoint logic change,
    the forwarding lambda ships arbitrary event types)."""
    block_answer = (
        "Two readings exist.\n\n```candidates\n"
        "1. Weekly cadence | confidence=0.80 | sources: EchoDoc\n```"
    )

    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None, sources=None, user=None, role=None, scope=None):
        from quickjoiner.agent.agent import OnboardingAgent

        provider = ScriptedProvider([
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={})]),
            ChatResult(text=block_answer),
        ])
        tool = AgentTool(
            spec=ToolSpec(name="echo", description="d", input_schema={"type": "object", "properties": {}}),
            fn=lambda **kw: "[source: EchoDoc | uri: file://e.md | kind: doc | score: 0.9]\ntext",
        )
        return OnboardingAgent(provider, [tool], system="sys", score_ledger={"echodoc": 0.45})

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)

    events = sse_events(client.post("/api/chat", json={"message": "options?"}).text)
    types = [e["type"] for e in events]
    assert types == ["tool_call", "delta", "candidates", "answer", "done"]
    cands = json.loads(next(e for e in events if e["type"] == "candidates")["data"])
    assert cands == [{"rank": 1, "summary": "Weekly cadence", "confidence": 0.45,
                      "sources": ["EchoDoc"]}]
    answer = next(e for e in events if e["type"] == "answer")["data"]
    assert "```candidates" not in answer  # the rendered answer is the stripped prose


def test_chat_reuses_session_history(client, monkeypatch):
    providers: list[ScriptedProvider] = []

    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None, sources=None, user=None, role=None, scope=None):
        from quickjoiner.agent.agent import OnboardingAgent

        provider = ScriptedProvider([ChatResult(text=f"answer {len(providers)}")])
        providers.append(provider)
        return OnboardingAgent(provider, [], system="sys")

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)

    first = sse_events(client.post("/api/chat", json={"message": "q1"}).text)
    session_id = next(e for e in first if e["type"] == "answer")["session_id"]

    client.post("/api/chat", json={"message": "q2", "session_id": session_id})
    second_messages = providers[1].calls[0]["messages"]
    roles = [(m["role"], m["content"]) for m in second_messages]
    assert ("user", "q1") in roles and ("assistant", "answer 0") in roles


def test_chat_provider_failure_becomes_error_event(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 200  # failures stream as events, not HTTP errors
    types = [e["type"] for e in sse_events(resp.text)]
    assert types == ["error", "done"]


def test_chat_session_persists_across_app_restarts(api_workspace, monkeypatch):
    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None, sources=None, user=None, role=None, scope=None):
        from quickjoiner.agent.agent import OnboardingAgent

        return OnboardingAgent(ScriptedProvider([ChatResult(text="persisted answer")]), [], system="sys")

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)

    first_client = TestClient(create_app(api_workspace))
    events = sse_events(first_client.post("/api/chat", json={"message": "q1"}).text)
    session_id = next(e for e in events if e["type"] == "answer")["session_id"]

    # A brand-new app instance (fresh process semantics) still knows the session.
    second_client = TestClient(create_app(api_workspace))
    session = second_client.get(f"/api/sessions/{session_id}").json()
    assert session["title"].startswith("q1")
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]


def test_projects_and_sessions_endpoints(client, monkeypatch):
    assert client.get("/api/projects").json() == []
    project = client.post(
        "/api/projects", json={"name": "Payments Ramp-up", "description": "onboarding"}
    ).json()
    assert project["id"] == "payments-ramp-up"

    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None, sources=None, user=None, role=None, scope=None):
        from quickjoiner.agent.agent import OnboardingAgent

        fake_build_agent.last_system = extra_system
        return OnboardingAgent(ScriptedProvider([ChatResult(text="ok")]), [], system="sys")

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)
    client.post("/api/chat", json={"message": "q", "project": "payments-ramp-up"})

    # The project frames the system prompt and groups the session.
    assert "Payments Ramp-up" in fake_build_agent.last_system
    rows = client.get("/api/sessions", params={"project": "payments-ramp-up"}).json()
    assert len(rows) == 1 and rows[0]["project_id"] == "payments-ramp-up"

    assert client.get("/api/sessions", params={"project": "nope"}).status_code == 404
    assert client.get("/api/sessions/nope").status_code == 404


def test_delete_sessions_endpoints(client, monkeypatch):
    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None, sources=None, user=None, role=None, scope=None):
        from quickjoiner.agent.agent import OnboardingAgent

        return OnboardingAgent(ScriptedProvider([ChatResult(text="ok")]), [], system="sys")

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)

    # Two conversations: one plain, one under a project.
    client.post("/api/chat", json={"message": "first"})
    client.post("/api/projects", json={"name": "Ramp"})
    client.post("/api/chat", json={"message": "second", "project": "ramp"})
    rows = client.get("/api/sessions").json()
    assert len(rows) == 2

    # Delete a single conversation: the plain (non-project) one, so the
    # project-scoped assertions below still have their "ramp" session.
    victim = next(r["id"] for r in rows if not r.get("project_id"))
    assert client.delete(f"/api/sessions/{victim}").json() == {"deleted": victim}
    assert client.get(f"/api/sessions/{victim}").status_code == 404
    assert len(client.get("/api/sessions").json()) == 1
    assert client.delete("/api/sessions/gone").status_code == 404

    # Project-scoped clear only removes that project's sessions.
    assert client.delete("/api/sessions", params={"project": "ramp"}).json() == {"deleted": 1}
    assert client.delete("/api/sessions", params={"project": "nope"}).status_code == 404

    # Clear-all removes whatever remains.
    client.post("/api/chat", json={"message": "third"})
    assert client.delete("/api/sessions").json()["deleted"] >= 1
    assert client.get("/api/sessions").json() == []


# -- webhook receivers ---------------------------------------------------------

def test_hook_github_scheme_ingests_and_registers_source(client):
    body = json.dumps(ISSUE_PAYLOAD).encode()
    resp = client.post(
        "/hooks/ghrepo", content=body, headers={"X-Hub-Signature-256": github_sig(body)}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True and data["documents"] == 1
    assert "1 added" in data["ingested"]

    # Pushed docs are searchable and the source shows up without a pull sync.
    rows = client.get("/api/sources").json()
    ghrepo = next(r for r in rows if r["name"] == "ghrepo")
    assert ghrepo["documents"] == 1

    # This checks the pushed doc is live memory, not grounding-threshold behavior —
    # bypass min_score so it stays robust to retuning (see test_learn_endpoint_teaches_fact).
    client.patch("/api/settings", json={"retrieval": {"min_score": 0.0}})
    hits = client.get(
        "/api/search", params={"q": "The Friday deploy fails intermittently on the smoke stage."}
    ).json()
    assert any("issues/42" in h["uri"] for h in hits)


def test_hook_generic_scheme(client):
    body = json.dumps(ISSUE_PAYLOAD).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post("/hooks/ghrepo", content=body, headers={"X-QJ-Signature": sig})
    assert resp.status_code == 200


def test_hook_rejects_bad_signature(client):
    body = json.dumps(ISSUE_PAYLOAD).encode()
    resp = client.post(
        "/hooks/ghrepo", content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}
    )
    assert resp.status_code == 403


def test_hook_rejects_unsigned_request(client):
    resp = client.post("/hooks/ghrepo", content=b"{}")
    assert resp.status_code == 403


def test_hook_refuses_source_without_secret(client):
    resp = client.post("/hooks/handbook", content=b"{}")
    assert resp.status_code == 403
    assert "webhook_secret" in resp.json()["detail"]


def test_hook_unknown_source_is_404(client):
    assert client.post("/hooks/nope", content=b"{}").status_code == 404


def test_hook_invalid_json_is_400(client):
    body = b"this is not json"
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post("/hooks/ghrepo", content=body, headers={"X-QJ-Signature": sig})
    assert resp.status_code == 400


def test_verify_signature_schemes():
    body = b'{"a": 1}'
    digest = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert verify_signature("s", body, {"x-hub-signature-256": f"sha256={digest}"})
    assert verify_signature("s", body, {"x-gitlab-token": "s"})
    assert verify_signature("s", body, {"x-qj-signature": digest})
    assert not verify_signature("s", body, {"x-qj-signature": "wrong"})
    assert not verify_signature("s", body, {})
    # The query-token scheme — for a sender (Azure DevOps Service Hooks, Octopus
    # subscriptions) that can only configure a bare callback URL, no custom headers.
    assert verify_signature("s", body, {}, token="s")
    assert not verify_signature("s", body, {}, token="wrong")


def test_hook_query_token_scheme(client):
    # No header at all — just the URL's ?token=, the scheme a sender with no signing
    # capability falls back to.
    body = json.dumps(ISSUE_PAYLOAD).encode()
    resp = client.post("/hooks/ghrepo", content=body, params={"token": SECRET})
    assert resp.status_code == 200


def test_hook_git_push_triggers_a_resync_not_direct_ingest(tmp_path, monkeypatch):
    # A git connector's push webhook carries commit metadata, never file contents — the
    # only way to actually ingest what changed is a real sync() run (git pull + re-read),
    # not the direct handle_event->ingest path every other connector uses.
    from quickjoiner.sync_manager import SyncJob

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        yaml.safe_dump({
            "org": "acme",
            "sources": [
                {"name": "gitrepo", "type": "git",
                 "options": {"url": "https://example.com/x.git", "branch": "main",
                             "webhook_secret": SECRET}},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    started = []

    def fake_start(self, source_name, clean=False):
        started.append(source_name)
        return SyncJob(id="sync-1-test", source_name=source_name, source_id=f"git:{source_name}")

    monkeypatch.setattr("quickjoiner.sync_manager.SyncManager.start", fake_start)
    hook_client = TestClient(create_app(ws))

    body = json.dumps({"ref": "refs/heads/main"}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = hook_client.post("/hooks/gitrepo", content=body, headers={"X-QJ-Signature": sig})
    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True and data["resync"] is True
    assert started == ["gitrepo"]  # a real sync() was kicked off, not a direct-ingest call

    # A push to a DIFFERENT branch is correctly ignored — no resync, no error either.
    started.clear()
    body2 = json.dumps({"ref": "refs/heads/other"}).encode()
    sig2 = hmac.new(SECRET.encode(), body2, hashlib.sha256).hexdigest()
    resp2 = hook_client.post("/hooks/gitrepo", content=body2, headers={"X-QJ-Signature": sig2})
    assert resp2.status_code == 200 and "resync" not in resp2.json()
    assert started == []


# -- briefs --------------------------------------------------------------------

def test_briefs_endpoints(client):
    assert client.get("/api/briefs").json() == []
    assert client.post("/api/briefs/nope").status_code == 404

    # Nothing learned yet: returns the refusal without needing an LLM provider.
    resp = client.post("/api/briefs/architecture")
    assert resp.status_code == 200
    data = resp.json()
    assert data["generated"] is False and "haven't learned enough" in data["brief"]


# -- scheduler -----------------------------------------------------------------

def test_scheduler_registers_interval_jobs(api_workspace):
    from quickjoiner.scheduler import start_scheduler

    ctx = build_context(api_workspace)
    scheduler = start_scheduler(ctx)
    try:
        assert scheduler is not None
        job = scheduler.get_job("sync-handbook")
        assert job is not None
        assert scheduler.get_job("sync-ghrepo") is None  # no interval configured
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        ctx.catalog.close()


def test_scheduler_runs_context_cleanup_even_without_interval_sources(tmp_path, monkeypatch):
    from quickjoiner.scheduler import start_scheduler

    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    ctx = build_context(tmp_path / "empty-ws")
    scheduler = start_scheduler(ctx)
    try:
        # No interval sources, but the standing context-attachment cleanup sweep is always present.
        assert scheduler is not None
        assert scheduler.get_job("context-attachment-cleanup") is not None
        assert scheduler.get_job("sync-handbook") is None
    finally:
        scheduler.shutdown(wait=False)
        ctx.catalog.close()


def test_sync_job_ingests_and_records_state(api_workspace):
    from quickjoiner.scheduler import _sync_job

    ctx = build_context(api_workspace)
    try:
        _sync_job(ctx, "handbook")
        assert ctx.catalog.stats()["documents"] == 1
        assert "since" in ctx.catalog.get_sync_state("files:handbook")
        # A missing source name must be a no-op, not an exception.
        _sync_job(ctx, "does-not-exist")
    finally:
        ctx.catalog.close()


# ------------------------------------------------------------ OneDrive / SharePoint
# The connector's Graph behaviour is covered offline in tests/test_onedrive.py; these
# pin the HTTP surface: what it refuses, and that it never leaks a token.

def _make_onedrive(client, name="drive"):
    return client.post("/api/connectors", json={
        "name": name, "type": "onedrive",
        "options": {"client_id": "00000000-0000-0000-0000-000000000000"},
    })


def test_onedrive_connector_can_be_created_before_signing_in(client):
    # test() reports "not signed in" as ok on purpose: the token is keyed by source_id,
    # so it cannot exist until the connector does. A failing test would deadlock that.
    resp = _make_onedrive(client)
    assert resp.status_code == 200, resp.text
    assert "not signed in yet" in resp.json()["test"]["message"]


def test_onedrive_oauth_status_reports_signed_out_and_never_returns_tokens(client):
    _make_onedrive(client)
    body = client.get("/api/connectors/drive/oauth/status").json()
    assert body == {"signed_in": False, "account": "", "scopes": [], "learned": 0}
    assert "token" not in json.dumps(body)


def test_onedrive_oauth_start_returns_a_microsoft_authorize_url_with_pkce(client):
    _make_onedrive(client)
    body = client.post("/api/connectors/drive/oauth/start").json()
    assert body["authorize_url"].startswith("https://login.microsoftonline.com/organizations")
    assert "code_challenge=" in body["authorize_url"] and "code_challenge_method=S256" in body["authorize_url"]
    assert body["redirect_uri"].endswith("/api/oauth/callback")
    assert "offline_access" in body["authorize_url"]  # no refresh token without it


def test_oauth_callback_rejects_an_unknown_state(client):
    # The callback is unauthenticated by necessity (a browser redirect from Microsoft),
    # so the single-use server-minted `state` is the whole guard.
    resp = client.get("/api/oauth/callback", params={"state": "forged", "code": "x"})
    assert resp.status_code == 400 and "single use" in resp.text


def test_onedrive_learn_requires_targets_and_refuses_non_onedrive_connectors(client):
    _make_onedrive(client)
    assert client.post("/api/connectors/drive/onedrive/learn", json={"targets": []}).status_code == 400
    # "handbook" is the files connector the fixture configures.
    resp = client.post("/api/connectors/handbook/onedrive/learn", json={"targets": ["x"]})
    assert resp.status_code == 400 and "not a OneDrive" in resp.json()["detail"]


def test_deleting_a_onedrive_connector_removes_its_stored_refresh_token(client, api_workspace):
    from quickjoiner.connectors.msgraph import TokenBundle, save_token, token_path

    _make_onedrive(client, "gone")
    save_token(api_workspace, "onedrive:gone", TokenBundle(refresh_token="live-token"))
    assert token_path(api_workspace, "onedrive:gone").exists()
    assert client.delete("/api/connectors/gone").status_code == 200
    # A refresh token is redeemable on its own — it must not outlive its connector.
    assert not token_path(api_workspace, "onedrive:gone").exists()


# ------------------------------------------------- browser sign-in for gated scraping

def test_browser_session_endpoints_report_and_guard(api_workspace, monkeypatch):
    """A credential-gated scrape connector's session is inspectable and re-signinable from
    the UI. The session check must FETCH, not just look for a profile directory — that is
    the trap that made a broken connector report healthy."""
    from quickjoiner.connectors.browser import login_jobs

    ctx = build_context(api_workspace)
    ctx.catalog.write_source(SourceConfig(
        name="gated", type="web_scrape",
        options={"start_urls": "https://gated.test/", "use_browser": "true"}))
    ctx.config = ctx.catalog.load_config()
    client = TestClient(create_app(api_workspace, ctx=ctx))

    monkeypatch.setattr("quickjoiner.connectors.browser.session.verify_session",
                        lambda ws, url, ignore_https_errors=False: (
                            False, "landed on https://sso.test/SignIn — that is a sign-in page."))
    body = client.get("/api/connectors/gated/browser/session").json()
    assert body["url"] == "https://gated.test/"
    assert body["signed_in"] is False and "sign-in page" in body["detail"]

    monkeypatch.setattr("quickjoiner.connectors.browser.session.verify_session",
                        lambda ws, url, ignore_https_errors=False: (
                            True, "https://gated.test/ returned 4210 characters"))
    assert client.get("/api/connectors/gated/browser/session").json()["signed_in"] is True

    # Starting a sign-in is a background job; the URL comes from the connector's own
    # config, never the request (this route must not open arbitrary pages on the server).
    started = {}

    def fake_start(ws, name, url, ignore_https_errors=False):
        started["url"] = url
        return login_jobs.LoginJob(source=name, url=url)

    monkeypatch.setattr(login_jobs, "start", fake_start)
    r = client.post("/api/connectors/gated/browser/login")
    assert r.status_code == 200
    assert started["url"] == "https://gated.test/"
    assert r.json()["login"]["source"] == "gated"

    # A host with no display refuses with an explanation rather than hanging.
    def boom(ws, name, url, ignore_https_errors=False):
        raise RuntimeError("This server has no display")

    monkeypatch.setattr(login_jobs, "start", boom)
    r = client.post("/api/connectors/gated/browser/login")
    assert r.status_code == 409 and "no display" in r.json()["detail"]


def test_browser_session_rejects_a_non_scrape_connector(api_workspace):
    client = TestClient(create_app(api_workspace))
    r = client.get("/api/connectors/handbook/browser/session")
    assert r.status_code == 400 and "not a web_scrape" in r.json()["detail"]


def _gated_scrape_client(api_workspace):
    ctx = build_context(api_workspace)
    ctx.catalog.write_source(SourceConfig(
        name="gated", type="web_scrape",
        options={"start_urls": "https://gated.test/", "use_browser": "true"}))
    ctx.config = ctx.catalog.load_config()
    return TestClient(create_app(api_workspace, ctx=ctx))


def test_browser_session_reports_remote_capable_on_a_headless_host(api_workspace, monkeypatch):
    """`remote_capable` reflects `login_mode()` independent of any running job, so the
    frontend can decide up front whether "Sign in" opens a local window or the remote modal."""
    from quickjoiner.connectors.browser import login_jobs

    client = _gated_scrape_client(api_workspace)
    monkeypatch.setattr(login_jobs, "login_mode", lambda: "remote")
    monkeypatch.setattr("quickjoiner.connectors.browser.session.verify_session",
                        lambda ws, url, ignore_https_errors=False: (True, "ok"))
    body = client.get("/api/connectors/gated/browser/session").json()
    assert body["remote_capable"] is True
    # display_hint()/can_open_window call login_mode() themselves, so they see the same patch.
    assert body["can_open_window"] is True and body["display_hint"] is None


def test_browser_session_frames_streams_meta_frames_then_done(api_workspace, monkeypatch):
    from quickjoiner.connectors.browser import login_jobs

    client = _gated_scrape_client(api_workspace)
    q = queue.Queue()
    q.put("frame-one")
    q.put("frame-two")
    q.put(login_jobs.FRAME_DONE)
    monkeypatch.setattr(login_jobs, "subscribe_frames", lambda name: q)

    r = client.get("/api/connectors/gated/browser/session/frames")
    assert r.status_code == 200
    events = [json.loads(line[len("data: "):]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[0] == {"type": "meta", "width": 1280, "height": 800}
    assert [e["data"] for e in events if e["type"] == "frame"] == ["frame-one", "frame-two"]
    assert events[-1] == {"type": "done"}


def test_browser_session_frames_404s_when_no_remote_session_is_running(api_workspace, monkeypatch):
    from quickjoiner.connectors.browser import login_jobs

    client = _gated_scrape_client(api_workspace)
    monkeypatch.setattr(login_jobs, "subscribe_frames", lambda name: None)
    r = client.get("/api/connectors/gated/browser/session/frames")
    assert r.status_code == 404


def test_browser_session_input_routes_through_and_guards_a_local_job(api_workspace, monkeypatch):
    from quickjoiner.connectors.browser import login_jobs

    client = _gated_scrape_client(api_workspace)

    monkeypatch.setattr(login_jobs, "push_input", lambda name, event: False)
    r = client.post("/api/connectors/gated/browser/session/input", json={"type": "mousemove", "x": 1, "y": 2})
    assert r.status_code == 404

    captured = {}
    monkeypatch.setattr(login_jobs, "push_input",
                        lambda name, event: captured.setdefault("event", event) or True)
    r = client.post("/api/connectors/gated/browser/session/input", json={"type": "keydown", "key": "Enter"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert captured["event"] == {"type": "keydown", "key": "Enter"}

    # A local-mode job in flight has no remote session to interact with — 409, not a no-op.
    monkeypatch.setattr(login_jobs, "status", lambda name: login_jobs.LoginJob(
        source=name, url="https://gated.test/", state="waiting", mode="local"))
    r = client.post("/api/connectors/gated/browser/session/input", json={"type": "mousemove", "x": 1, "y": 2})
    assert r.status_code == 409


def test_browser_session_done_routes_through_and_guards_a_local_job(api_workspace, monkeypatch):
    from quickjoiner.connectors.browser import login_jobs

    client = _gated_scrape_client(api_workspace)

    monkeypatch.setattr(login_jobs, "signal_done", lambda name: False)
    r = client.post("/api/connectors/gated/browser/session/done")
    assert r.status_code == 404

    monkeypatch.setattr(login_jobs, "signal_done", lambda name: True)
    r = client.post("/api/connectors/gated/browser/session/done")
    assert r.status_code == 200 and r.json() == {"ok": True}

    monkeypatch.setattr(login_jobs, "status", lambda name: login_jobs.LoginJob(
        source=name, url="https://gated.test/", state="waiting", mode="local"))
    r = client.post("/api/connectors/gated/browser/session/done")
    assert r.status_code == 409
