"""Per-question chat file attachments: upload/download/expiry, chat context injection,
message stamping, and strict isolation from the Uploads connector / learned memory."""

import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import quickjoiner.app as app_module
from quickjoiner import chat_attachments
from quickjoiner.agent.agent import OnboardingAgent
from quickjoiner.app import AppContext, build_context
from quickjoiner.api.app import create_app
from quickjoiner.llm.base import ChatResult
from tests.conftest import FakeEmbedder
from tests.test_agent_loop import ScriptedProvider


def _docx(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    ws = tmp_path / "ws"
    ctx = build_context(ws)
    client = TestClient(create_app(ws, ctx=ctx))
    return ctx, client


# -- storage / lifecycle ------------------------------------------------------

def test_store_extracts_text_and_stays_out_of_memory(env):
    ctx, client = env
    meta = chat_attachments.store_attachment(ctx, "spec.docx", _docx("Falcon runs 5 replicas."))
    assert meta["char_count"] > 0 and meta["filename"] == "spec.docx"
    # It is a context file, NOT a document: nothing in the documents/vector store or Uploads source.
    assert ctx.catalog.get_context_attachment(meta["id"]) is not None
    assert ctx.catalog.list_sources() == [] or all(
        s["name"] != "uploads" or s["doc_count"] == 0 for s in ctx.catalog.list_sources()
    )
    assert chat_attachments.attachment_original_path(ctx, meta["id"]) is not None


def test_cleanup_deletes_bytes_but_keeps_row_with_deleted_at(env):
    ctx, client = env
    meta = chat_attachments.store_attachment(ctx, "note.txt", b"restart the pod")
    aid = meta["id"]
    # Fresh: not swept.
    assert chat_attachments.cleanup_expired(ctx) == 0
    # Backdate past the 7-day window, then sweep.
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    ctx.catalog._write("UPDATE context_attachments SET uploaded_at = ? WHERE id = ?", (old, aid))
    assert chat_attachments.cleanup_expired(ctx) == 1
    row = ctx.catalog.get_context_attachment(aid)
    assert row is not None and row["deleted_at"]  # row kept, marked deleted
    assert chat_attachments.attachment_original_path(ctx, aid) is None  # bytes gone


# -- endpoints ----------------------------------------------------------------

def test_upload_download_and_gone_after_expiry(env):
    ctx, client = env
    r = client.post("/api/chat/attachments",
                    files={"files": ("spec.docx", _docx("Falcon scales to 5."), "application/octet-stream")})
    assert r.status_code == 200
    aid = r.json()["attachments"][0]["id"]
    assert client.get(f"/api/chat/attachments/{aid}/download").status_code == 200
    assert client.get("/api/chat/attachments/nope/download").status_code == 404
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    ctx.catalog._write("UPDATE context_attachments SET uploaded_at = ? WHERE id = ?", (old, aid))
    chat_attachments.cleanup_expired(ctx)
    gone = client.get(f"/api/chat/attachments/{aid}/download")
    assert gone.status_code == 410 and "deleted" in gone.json()["detail"].lower()


def test_upload_rejects_empty(env):
    _, client = env
    assert client.post("/api/chat/attachments", files={"files": ("x.txt", b"", "text/plain")}).status_code == 400


def test_attachments_never_enter_uploads_connector_or_search(env):
    ctx, client = env
    client.post("/api/chat/attachments",
                files={"files": ("secret.docx", _docx("The Kraken deploys nightly."), "application/octet-stream")})
    uploads = next(r for r in client.get("/api/connectors").json() if r["name"] == "uploads")
    assert uploads["documents"] == 0  # NOT ingested into the Uploads connector
    assert client.get("/api/search", params={"q": "Kraken deploys"}).json() == []  # not learned memory


# -- chat integration ---------------------------------------------------------

def test_chat_injects_attachment_text_and_stamps_the_message(env, monkeypatch):
    ctx, client = env
    up = client.post("/api/chat/attachments",
                     files={"files": ("design.docx", _docx("The Falcon service owner is Priya."), "application/octet-stream")})
    aid = up.json()["attachments"][0]["id"]

    captured = {}

    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None,
                         sources=None, user=None, role=None):
        captured["extra_system"] = extra_system or ""
        return OnboardingAgent(ScriptedProvider([ChatResult(text="Priya owns Falcon.")]), [], system="sys")

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)

    events = _sse(client.post("/api/chat", json={"message": "who owns Falcon?", "attachment_ids": [aid]}).text)
    answer = next(e for e in events if e["type"] == "answer")
    sid = answer["session_id"]
    # The attachment's extracted text was injected into the turn's system context, marked as a file.
    assert "Priya" in captured["extra_system"] and "design.docx" in captured["extra_system"]
    assert "ATTACHED FILES" in captured["extra_system"]

    # Reloaded history shows the attachment stamped on THIS user message, with a live download.
    session = client.get(f"/api/sessions/{sid}").json()
    user_msg = next(m for m in session["messages"] if m["role"] == "user")
    assert user_msg["attachments"][0]["id"] == aid
    assert user_msg["attachments"][0]["filename"] == "design.docx"
    assert not user_msg["attachments"][0]["deleted_at"]  # downloadable
    assert ctx.catalog.get_context_attachment(aid)["session_id"] == sid  # bound to the session

    # After expiry the same history row flips to a deleted marker (name kept for the warning UI).
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    ctx.catalog._write("UPDATE context_attachments SET uploaded_at = ? WHERE id = ?", (old, aid))
    chat_attachments.cleanup_expired(ctx)
    session2 = client.get(f"/api/sessions/{sid}").json()
    umsg2 = next(m for m in session2["messages"] if m["role"] == "user")
    assert umsg2["attachments"][0]["deleted_at"] and umsg2["attachments"][0]["filename"] == "design.docx"


def test_chat_without_attachments_is_unchanged(env, monkeypatch):
    _, client = env
    captured = {}

    def fake_build_agent(self, provider_override=None, model_override=None, extra_system=None,
                         sources=None, user=None, role=None):
        captured["extra_system"] = extra_system
        return OnboardingAgent(ScriptedProvider([ChatResult(text="hi")]), [], system="sys")

    monkeypatch.setattr(AppContext, "build_agent", fake_build_agent)
    _sse(client.post("/api/chat", json={"message": "hello"}).text)
    assert "ATTACHED FILES" not in (captured["extra_system"] or "")


def _sse(text: str) -> list[dict]:
    import json

    out = []
    for part in text.split("\n\n"):
        if part.startswith("data: "):
            out.append(json.loads(part[6:]))
    return out
