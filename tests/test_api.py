"""Phase 3 verification: FastAPI endpoints, SSE chat, webhook receivers, scheduler."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import yaml
from fastapi.testclient import TestClient

import quickjoiner.app as app_module
from quickjoiner.api.app import create_app
from quickjoiner.api.hooks import verify_signature
from quickjoiner.app import AppContext, build_context
from quickjoiner.llm.base import ChatResult, ToolCall

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


def test_sync_then_sources_and_search(client):
    resp = client.post("/api/sync/handbook")
    assert resp.status_code == 200
    assert "1 added" in resp.json()["result"]

    rows = client.get("/api/sources").json()
    handbook = next(r for r in rows if r["name"] == "handbook")
    assert handbook["documents"] == 1 and handbook["configured"] is True

    hits = client.get("/api/search", params={"q": "We deploy with Octopus on Fridays."}).json()
    assert hits and hits[0]["uri"].startswith("file://")
    assert "Octopus" in hits[0]["text"]

    # Second sync is idempotent: nothing re-added.
    again = client.post("/api/sync/handbook").json()
    assert "0 added" in again["result"] or "unchanged" in again["result"]


def test_sync_unknown_source_is_404(client):
    assert client.post("/api/sync/nope").status_code == 404


# -- SSE chat ----------------------------------------------------------------

def test_chat_streams_tool_calls_and_answer(client, monkeypatch):
    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None):
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


def test_chat_reuses_session_history(client, monkeypatch):
    providers: list[ScriptedProvider] = []

    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None):
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
    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None):
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

    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None):
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


def test_scheduler_returns_none_without_interval_sources(tmp_path, monkeypatch):
    from quickjoiner.scheduler import start_scheduler

    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    ctx = build_context(tmp_path / "empty-ws")
    try:
        assert start_scheduler(ctx) is None
    finally:
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
