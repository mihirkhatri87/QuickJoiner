"""Self-control tools (plan 08 stage 2): generic qj_api dispatch gated by RBAC + connector
scope + danger-confirm, the role-filtered reference, and the permanent control connector.
Also the route-coverage lockstep test (every /api route resolves to a capability)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

import quickjoiner.app as app_module
from quickjoiner import rbac
from quickjoiner.agent.control import build_control_tools
from quickjoiner.api.app import create_app
from quickjoiner.app import build_context

from tests.conftest import FakeEmbedder


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    ws = tmp_path / "ws"
    ctx = build_context(ws)
    app = create_app(ws, ctx=ctx)
    return ctx, app


def _tools(ctx, role):
    return {t.spec.name: t.fn for t in build_control_tools(ctx, user=None, role=role)}


# -- capability gating --------------------------------------------------------

def test_viewer_denied_write_but_allowed_read(env):
    ctx, _ = env
    qj = _tools(ctx, "viewer")["qj_api"]
    assert "PERMISSION DENIED" in qj("POST", "/api/connectors", {"name": "x", "type": "git"})
    assert "HTTP 200" in qj("GET", "/api/status")


def test_unknown_endpoint_denied(env):
    ctx, _ = env
    qj = _tools(ctx, "admin")["qj_api"]
    assert qj("GET", "/api/does-not-exist").startswith("DENIED")


def test_multipart_upload_endpoint_is_not_dispatchable_via_qj_api(env):
    """POST /api/uploads is multipart; qj_api can only send JSON, so it must refuse and point
    the model at the JSON local-path variant instead of failing cryptically (feature: uploads)."""
    ctx, _ = env
    qj = _tools(ctx, "editor")["qj_api"]
    out = qj("POST", "/api/uploads", {})
    assert "multipart" in out and "/api/uploads/local" in out
    # The JSON local-path variant IS dispatchable (a missing file 400s, proving it got through).
    assert "HTTP 400" in qj("POST", "/api/uploads/local", {"path": "/no/such/file.pdf"})


def test_editor_cannot_reset_or_change_settings(env):
    ctx, _ = env
    qj = _tools(ctx, "editor")["qj_api"]
    assert "PERMISSION DENIED" in qj("POST", "/api/memory/reset")
    assert "PERMISSION DENIED" in qj("PATCH", "/api/settings", {"retrieval": {"min_score": 0.6}})


# -- danger confirmation ------------------------------------------------------

def test_danger_requires_confirm_then_dispatches(env):
    ctx, _ = env
    qj = _tools(ctx, "admin")["qj_api"]
    # reset without confirm never dispatches — it asks.
    assert "CONFIRM REQUIRED" in qj("POST", "/api/memory/reset")
    # a danger call WITH confirm gets past the gate to real dispatch (here the control-connector
    # guard answers 409, proving confirm let it through rather than the confirm gate blocking it).
    out = qj("DELETE", "/api/connectors/quickjoiner", confirm=True)
    assert "HTTP 409" in out


def test_deleting_a_skill_from_chat_needs_a_typed_confirm(env, tmp_path):
    """Skill removal is reachable from chat, so "tidy up the old skills" must not become an
    unconfirmed rm. Same admin role as installing, one extra gate — `connectors:delete`'s
    precedent, and the reason DELETE has its own capability rather than sharing skills:write."""
    ctx, _ = env
    skill = ctx.workspace / "skills" / "throwaway"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: throwaway\ndescription: d\n---\nbody", encoding="utf-8")

    qj = _tools(ctx, "admin")["qj_api"]
    assert "CONFIRM REQUIRED" in qj("DELETE", "/api/skills/throwaway")
    assert skill.exists(), "an unconfirmed call must not have deleted anything"

    assert "HTTP 200" in qj("DELETE", "/api/skills/throwaway", confirm=True)
    assert not skill.exists()


def test_a_viewer_cannot_delete_a_skill_even_with_confirm(env):
    ctx, _ = env
    qj = _tools(ctx, "viewer")["qj_api"]
    assert "PERMISSION DENIED" in qj("DELETE", "/api/skills/anything", confirm=True)


def test_the_reference_surfaces_skills_so_the_agent_can_find_them(env):
    """Discoverability is the other half of "reachable from chat": an endpoint the model
    cannot find in qj_api_reference may as well not exist to it."""
    ctx, _ = env
    reference = _tools(ctx, "admin")["qj_api_reference"]("skills")
    assert "/api/skills" in reference
    assert "DELETE" in reference


# -- connector scope ----------------------------------------------------------

def test_connector_scope_denies_unknown_connector(env):
    ctx, _ = env
    qj = _tools(ctx, "admin")["qj_api"]
    out = qj("DELETE", "/api/connectors/ghost", confirm=True)
    assert "PERMISSION DENIED" in out and "ghost" in out


# -- reference filtering ------------------------------------------------------

def test_reference_is_role_filtered(env):
    ctx, _ = env
    viewer_ref = _tools(ctx, "viewer")["qj_api_reference"]("")
    admin_ref = _tools(ctx, "admin")["qj_api_reference"]("")
    assert "GET /api/status" in viewer_ref
    assert "POST /api/memory/reset" not in viewer_ref
    assert "PATCH /api/settings" not in viewer_ref
    assert "POST /api/memory/reset" in admin_ref


def test_reference_includes_connector_field_schema(env):
    ctx, _ = env
    ref = _tools(ctx, "admin")["qj_api_reference"]("connectors")
    assert "git:" in ref and "url*" in ref  # url is the required git field


# -- the permanent control connector ------------------------------------------

def test_control_connector_seeded_and_undeletable(env):
    ctx, app = env
    client = TestClient(app)
    names = [c["name"] for c in client.get("/api/connectors").json()]
    assert "quickjoiner" in names
    assert client.delete("/api/connectors/quickjoiner").status_code == 409
    assert client.post("/api/sync/quickjoiner").status_code == 409
    assert client.patch("/api/connectors/quickjoiner", json={"name": "renamed"}).status_code == 409


# -- lockstep: every /api route maps to a capability --------------------------

def test_every_api_route_resolves_to_a_capability(env):
    _, app = env
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if rbac.required_capability(method, route.path) is None:
                missing.append(f"{method} {route.path}")
    assert not missing, f"routes with no RBAC capability mapping: {missing}"
