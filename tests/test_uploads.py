"""The rolling Uploads connector + the /api/uploads endpoints."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import quickjoiner.app as app_module
from quickjoiner.api.app import create_app
from quickjoiner.connectors.uploads import (
    UPLOADS_SOURCE_ID,
    UploadsConnector,
    is_uploads_source,
    save_upload,
    uploads_dir,
)
from tests.conftest import FakeEmbedder


def _docx(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def test_save_upload_is_idempotent_but_wont_clobber_different_content(tmp_path):
    p1 = save_upload(tmp_path, "report.pdf", b"AAAA")
    p2 = save_upload(tmp_path, "report.pdf", b"AAAA")  # identical bytes -> same path
    assert p1 == p2
    p3 = save_upload(tmp_path, "report.pdf", b"BBBB")  # different content -> new path
    assert p3 != p1 and p3.exists() and p1.read_bytes() == b"AAAA"


def test_save_upload_sanitizes_path_traversal(tmp_path):
    p = save_upload(tmp_path, "../../etc/evil.txt", b"x")
    assert p.parent == uploads_dir(tmp_path)  # stays inside the uploads folder
    assert p.name == "evil.txt"


def test_uploads_connector_reads_office_files_from_its_folder(tmp_path):
    folder = uploads_dir(tmp_path)
    (folder / "guide.docx").write_bytes(_docx("Deploy Nautical via Octopus."))
    (folder / "notes.md").write_text("# Runbook\nrestart the pod", encoding="utf-8")
    conn = UploadsConnector(name="uploads", options={}, workspace=tmp_path)
    assert conn.test().ok
    docs = list(conn.sync({}))
    titles = {d.title for d in docs}
    assert "guide.docx" in titles and "notes.md" in titles
    guide = next(d for d in docs if d.title == "guide.docx")
    assert "Deploy Nautical via Octopus." in guide.text


def test_is_uploads_source_matches_reserved_name_and_type():
    assert is_uploads_source(name="uploads")
    assert is_uploads_source(type_="uploads")
    assert not is_uploads_source(name="handbook")


@pytest.fixture
def client(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    return TestClient(create_app(ws)), ws


def test_uploads_connector_is_seeded_and_permanent(client):
    c, _ = client
    rows = c.get("/api/connectors").json()
    assert any(r["type"] == "uploads" and r["name"] == "uploads" for r in rows)
    assert c.delete("/api/connectors/uploads").status_code == 409
    assert c.patch("/api/connectors/uploads", json={"name": "x"}).status_code == 409
    assert c.post("/api/connectors", json={"name": "more", "type": "uploads", "options": {}}).status_code == 409


def _uploads_doc_count(c) -> int:
    row = next(r for r in c.get("/api/connectors").json() if r["name"] == "uploads")
    return row["documents"]


def test_upload_endpoint_ingests_into_the_uploads_source(client):
    c, _ = client
    r = c.post("/api/uploads", files={"files": ("guide.docx", _docx("Nautical deploys to Production."), "application/octet-stream")})
    assert r.status_code == 200
    body = r.json()
    assert body["ingested"] == 1 and body["uploaded"][0]["ingested"] is True
    assert _uploads_doc_count(c) == 1


def test_upload_endpoint_reports_unreadable_files_without_failing(client):
    c, _ = client
    r = c.post("/api/uploads", files={"files": ("photo.png", b"\x89PNG not-a-doc", "image/png")})
    assert r.status_code == 200
    row = r.json()["uploaded"][0]
    assert row["ingested"] is False and "unsupported" in row["reason"]


def test_upload_local_path_ingests_into_uploads(client):
    c, ws = client
    src = ws / "handbook.md"
    src.write_text("# Onboarding\nUse the VPN before connecting.", encoding="utf-8")
    r = c.post("/api/uploads/local", json={"path": str(src)})
    assert r.status_code == 200 and r.json()["ingested"] is True
    # It was copied into the rolling uploads folder (so future re-syncs keep it).
    assert (uploads_dir(ws) / "handbook.md").exists()


def test_upload_local_rejects_missing_path(client):
    c, ws = client
    assert c.post("/api/uploads/local", json={"path": str(ws / "nope.md")}).status_code == 400


def test_uploaded_docs_survive_a_resync_idempotently(client):
    c, _ = client
    c.post("/api/uploads", files={"files": ("guide.docx", _docx("Nautical deploys to Production."), "application/octet-stream")})
    assert _uploads_doc_count(c) == 1
    # A folder sync of the uploads connector re-reads the same file at the same uri -> unchanged,
    # so the count stays 1 (no duplicate document keyed to a different uri).
    assert c.post("/api/sync/uploads").status_code == 200
    assert _uploads_doc_count(c) == 1
    assert UPLOADS_SOURCE_ID == "uploads:uploads"
