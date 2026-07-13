"""Auth + per-user connector sharing: unit logic, CLI-facing Auth ops, and API flows."""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

import quickjoiner.app as app_module
from quickjoiner.api.app import MASKED, create_app
from quickjoiner.auth import Auth, can_manage, hash_password, verify_password, visible
from quickjoiner.config import SourceConfig
from quickjoiner.memory.catalog import Catalog

from tests.conftest import FakeEmbedder


# -- primitives ---------------------------------------------------------------

def test_password_hash_roundtrip():
    stored = hash_password("hunter22")
    assert verify_password("hunter22", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("hunter22", "garbage")


def test_auth_user_and_token_lifecycle(tmp_path):
    catalog = Catalog(tmp_path)
    auth = Auth(catalog)
    assert not auth.enabled

    auth.create_user("meena", "s3cret")
    assert auth.enabled
    token = auth.login("meena", "s3cret")
    assert auth.resolve(token) == "meena"
    auth.logout(token)
    assert auth.resolve(token) is None

    import pytest
    with pytest.raises(ValueError):
        auth.login("meena", "wrong-password")
    with pytest.raises(ValueError):
        auth.create_user("meena", "again")  # duplicate
    catalog.close()


def test_visibility_rules():
    commons = SourceConfig(name="a", type="files")            # owner None
    private = SourceConfig(name="b", type="files", owner="meena", shared=False)
    shared = SourceConfig(name="c", type="files", owner="meena", shared=True)

    # Open mode: everything visible and manageable by anyone.
    assert visible(private, None, False) and can_manage(private, None, False)
    # Auth on: commons stay visible; private is owner-only.
    assert visible(commons, "raj", True)
    assert visible(private, "meena", True) and not visible(private, "raj", True)
    assert visible(shared, "raj", True)
    # Shared still means only the owner manages it.
    assert can_manage(shared, "meena", True) and not can_manage(shared, "raj", True)


# -- API flows ------------------------------------------------------------------

def _client(tmp_path, monkeypatch) -> TestClient:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# Docs\nDeploys happen on Fridays.", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(yaml.safe_dump({"org": "acme", "sources": []}), encoding="utf-8")
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    return TestClient(create_app(ws))


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_open_mode_connector_crud(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/auth/status").json() == {"enabled": False, "user": None}

    docs = tmp_path / "docs"
    resp = client.post("/api/connectors", json={
        "name": "handbook", "type": "files", "options": {"path": str(docs)},
    })
    assert resp.status_code == 200
    row = resp.json()
    assert row["shared"] is True and row["owner"] is None and row["test"]["ok"]

    rows = client.get("/api/connectors").json()
    assert [r["name"] for r in rows] == ["handbook"]
    assert "pull" in rows[0]["modes"]

    assert client.post("/api/sync/handbook").status_code == 200
    assert client.delete("/api/connectors/handbook").status_code == 200
    assert client.get("/api/connectors").json() == []


def test_auth_enables_private_connectors_and_sharing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    docs = str(tmp_path / "docs")

    # Bootstrap first user (open mode allows it), then auth is ON.
    assert client.post("/api/auth/users", json={"username": "meena", "password": "pw44"}).status_code == 200
    assert client.get("/api/auth/status").json()["enabled"] is True

    # Anonymous management is now rejected.
    resp = client.post("/api/connectors", json={"name": "x", "type": "files", "options": {"path": docs}})
    assert resp.status_code == 401

    meena = client.post("/api/auth/login", json={"username": "meena", "password": "pw44"}).json()
    assert client.get("/api/auth/status", headers=_bearer(meena["token"])).json()["user"] == "meena"

    # Second user needs a signed-in creator now.
    assert client.post("/api/auth/users", json={"username": "raj", "password": "pw55"}).status_code == 401
    assert client.post(
        "/api/auth/users", json={"username": "raj", "password": "pw55"},
        headers=_bearer(meena["token"]),
    ).status_code == 200
    raj = client.post("/api/auth/login", json={"username": "raj", "password": "pw55"}).json()

    # Meena registers a private connector with a literal secret.
    resp = client.post("/api/connectors", json={
        "name": "meena-notes", "type": "files", "options": {"path": docs},
        "shared": False,
    }, headers=_bearer(meena["token"]))
    assert resp.status_code == 200
    assert resp.json()["owner"] == "meena" and resp.json()["shared"] is False

    # Raj can't see, sync, or remove it.
    assert client.get("/api/connectors", headers=_bearer(raj["token"])).json() == []
    assert client.post("/api/sync/meena-notes", headers=_bearer(raj["token"])).status_code == 404
    assert client.delete("/api/connectors/meena-notes", headers=_bearer(raj["token"])).status_code == 404
    # And can't squat the name.
    assert client.post("/api/connectors", json={
        "name": "meena-notes", "type": "files", "options": {"path": docs},
    }, headers=_bearer(raj["token"])).status_code == 409

    # Meena shares it -> Raj sees it and can sync, but still can't manage.
    resp = client.patch("/api/connectors/meena-notes", json={"shared": True},
                        headers=_bearer(meena["token"]))
    assert resp.status_code == 200 and resp.json()["shared"] is True
    rows = client.get("/api/connectors", headers=_bearer(raj["token"])).json()
    assert [r["name"] for r in rows] == ["meena-notes"]
    assert rows[0]["can_manage"] is False
    assert client.post("/api/sync/meena-notes", headers=_bearer(raj["token"])).status_code == 200
    assert client.patch("/api/connectors/meena-notes", json={"shared": False},
                        headers=_bearer(raj["token"])).status_code == 403


def test_secret_masking_and_masked_update_keeps_secret(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/connectors", json={
        "name": "gh", "type": "github",
        "options": {"repo": "acme/api", "token": "ghp_realsecret", "base_url": "https://ghe.acme.com/api/v3"},
        "skip_test": True,
    })
    assert resp.status_code == 200
    opts = resp.json()["options"]
    assert opts["token"] == MASKED and opts["repo"] == "acme/api"

    # env: references are safe to display.
    client.post("/api/connectors", json={
        "name": "gh2", "type": "github",
        "options": {"repo": "acme/api", "token": "env:GITHUB_TOKEN"}, "skip_test": True,
    })
    rows = {r["name"]: r for r in client.get("/api/connectors").json()}
    assert rows["gh2"]["options"]["token"] == "env:GITHUB_TOKEN"

    # PATCH sending the mask back must not clobber the stored secret.
    resp = client.patch("/api/connectors/gh", json={
        "options": {"token": MASKED, "base_url": "https://ghe2.acme.com/api/v3"},
    })
    assert resp.status_code == 200
    # Config now lives in SQLite — read the raw stored source back from the catalog.
    catalog = Catalog(tmp_path / "ws")
    gh = next(s for s in catalog.list_source_configs() if s.name == "gh")
    catalog.close()
    assert gh.options["token"] == "ghp_realsecret"
    assert gh.options["base_url"] == "https://ghe2.acme.com/api/v3"


def test_connector_sync_interval(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    docs = str(tmp_path / "docs")
    row = client.post("/api/connectors", json={
        "name": "handbook", "type": "files", "options": {"path": docs},
        "sync_interval_minutes": 30,
    }).json()
    assert row["sync_interval_minutes"] == 30

    # Change it.
    r = client.patch("/api/connectors/handbook", json={"sync_interval_minutes": 60})
    assert r.status_code == 200 and r.json()["sync_interval_minutes"] == 60

    # Clear it (back to manual) — None over JSON is ambiguous, so use the explicit flag.
    r = client.patch("/api/connectors/handbook", json={"clear_sync_interval": True})
    assert r.status_code == 200 and r.json()["sync_interval_minutes"] is None

    # Persisted to SQLite.
    catalog = Catalog(tmp_path / "ws")
    src = next(s for s in catalog.list_source_configs() if s.name == "handbook")
    catalog.close()
    assert src.sync_interval_minutes is None


def test_settings_get_patch_and_persist(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(yaml.safe_dump({"org": "acme", "sources": []}), encoding="utf-8")
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    client = TestClient(create_app(ws))

    s = client.get("/api/settings").json()
    assert s["retrieval"]["min_score"] == 0.55 and s["llm"]["provider"] == "anthropic"
    assert s["embedding_reindex_required"] is True

    # Open mode: no sign-in required to tune.
    resp = client.patch("/api/settings", json={
        "llm": {"provider": "ollama", "model": "gemma4:cloud", "max_tokens": 4096},
        "retrieval": {"min_score": 0.7, "top_k": 12},
        "chat": {"learn_from_conversations": False},
    })
    assert resp.status_code == 200
    got = resp.json()
    assert got["llm"]["model"] == "gemma4:cloud" and got["retrieval"]["min_score"] == 0.7

    # Invalid value -> 400, not a crash.
    assert client.patch("/api/settings", json={"retrieval": {"min_score": "high"}}).status_code == 400

    # Persisted to SQLite: a fresh app on the same workspace sees the change.
    client2 = TestClient(create_app(ws))
    s2 = client2.get("/api/settings").json()
    assert s2["llm"]["provider"] == "ollama" and s2["retrieval"]["top_k"] == 12
    assert s2["chat"]["learn_from_conversations"] is False


def test_settings_patch_requires_signin_when_auth_enabled(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/users", json={"username": "meena", "password": "pw44"})  # auth on
    assert client.patch("/api/settings", json={"retrieval": {"top_k": 5}}).status_code == 401
    tok = client.post("/api/auth/login", json={"username": "meena", "password": "pw44"}).json()["token"]
    assert client.patch("/api/settings", json={"retrieval": {"top_k": 5}},
                        headers=_bearer(tok)).status_code == 200


def test_connector_types_catalog(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    types = client.get("/api/connectors/types").json()
    by_type = {t["type"]: t for t in types}
    assert "gitlab" in by_type and "files" in by_type
    assert "live" in by_type["gitlab"]["modes"]
    keys = {f["key"] for f in by_type["gitlab"]["fields"]}
    assert {"project", "token", "base_url"} <= keys
