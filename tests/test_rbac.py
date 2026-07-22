"""RBAC foundation: capability sets, role→capability truth table, route-capability
resolution, and the Auth role methods over the catalog (plan 08 stage 1)."""

from __future__ import annotations

import pytest

from quickjoiner import rbac
from quickjoiner.auth import Auth
from quickjoiner.memory.catalog import Catalog


# -- pure capability / role model --------------------------------------------

def test_role_caps_are_subsets_of_declared_caps():
    for role, caps in rbac.ROLE_CAPS.items():
        assert role in rbac.ROLES
        assert caps <= rbac.CAPS, f"{role} grants an unknown capability"


def test_privilege_ordering_viewer_subset_editor_subset_admin():
    assert rbac.ROLE_CAPS["viewer"] < rbac.ROLE_CAPS["editor"] < rbac.ROLE_CAPS["admin"]


def test_admin_has_everything_including_danger_and_users_admin():
    admin = rbac.capabilities_for("admin")
    assert rbac.DANGER_CAPS <= admin
    assert "users:admin" in admin and "settings:write" in admin


def test_viewer_is_read_only():
    viewer = rbac.capabilities_for("viewer")
    assert all(not c.endswith((":write", ":delete", ":run")) and c not in rbac.DANGER_CAPS
               and c != "users:admin" for c in viewer)


def test_editor_cannot_reset_change_settings_or_admin_users():
    editor = rbac.capabilities_for("editor")
    assert "memory:reset" not in editor
    assert "settings:write" not in editor
    assert "users:admin" not in editor
    # but can do the everyday write work
    assert {"connectors:write", "sync:run", "memory:write", "gaps:write"} <= editor


def test_unknown_role_gets_least_privilege():
    assert rbac.capabilities_for("wat") == rbac.ROLE_CAPS[rbac.DEFAULT_ROLE]
    assert rbac.capabilities_for(None) == rbac.ROLE_CAPS[rbac.DEFAULT_ROLE]


# -- route → capability resolution -------------------------------------------

def test_required_capability_matches_templated_paths():
    rc = rbac.required_capability("DELETE", "/api/connectors/team-wiki")
    assert rc is not None
    assert rc.capability == "connectors:delete"
    assert rc.scope == "connector_write"
    assert rc.connector == "team-wiki"


def test_connector_read_scope_on_a_get():
    rc = rbac.required_capability("GET", "/api/sync/octopus/logs")
    assert rc.capability == "connectors:read"
    assert rc.scope == "connector_read"
    assert rc.connector == "octopus"


def test_query_string_and_trailing_slash_are_ignored():
    rc = rbac.required_capability("GET", "/api/gaps/?hours=24")
    assert rc is not None and rc.capability == "gaps:read"


def test_unknown_route_is_denied_not_allowed():
    assert rbac.required_capability("GET", "/api/nonexistent") is None
    assert rbac.can("admin", "GET", "/api/nonexistent") is False


def test_public_routes_allowed_for_any_role():
    for role in (None, "viewer", "editor", "admin"):
        assert rbac.can(role, "POST", "/api/auth/login")
        assert rbac.can(role, "GET", "/api/auth/status")


def test_can_truth_table_for_reset_and_delete():
    assert rbac.can("admin", "POST", "/api/memory/reset")
    assert not rbac.can("editor", "POST", "/api/memory/reset")
    assert not rbac.can("viewer", "POST", "/api/memory/reset")
    assert rbac.can("editor", "DELETE", "/api/connectors/x")
    assert not rbac.can("viewer", "DELETE", "/api/connectors/x")


def test_settings_write_is_admin_only():
    assert rbac.can("admin", "PATCH", "/api/settings")
    assert not rbac.can("editor", "PATCH", "/api/settings")


def test_danger_classification():
    assert rbac.is_danger("memory:reset")
    assert rbac.is_danger("connectors:delete")
    assert not rbac.is_danger("connectors:write")


# -- Auth role methods over the catalog --------------------------------------

def test_first_user_is_admin_regardless_of_requested_role(tmp_path):
    catalog = Catalog(tmp_path)
    auth = Auth(catalog)
    role = auth.create_user("root", "s3cret", role="viewer")
    assert role == "admin"
    assert auth.get_role("root") == "admin"
    catalog.close()


def test_later_users_default_to_viewer_and_role_can_be_set(tmp_path):
    catalog = Catalog(tmp_path)
    auth = Auth(catalog)
    auth.create_user("root", "s3cret")            # admin (first)
    r = auth.create_user("bob", "s3cret")          # default
    assert r == "viewer"
    r2 = auth.create_user("edna", "s3cret", role="editor")
    assert r2 == "editor"
    auth.set_role("bob", "editor")
    assert auth.get_role("bob") == "editor"
    with pytest.raises(ValueError):
        auth.set_role("bob", "superuser")
    with pytest.raises(ValueError):
        auth.set_role("ghost", "editor")
    catalog.close()


def test_role_of_open_mode_is_admin_then_stored(tmp_path):
    catalog = Catalog(tmp_path)
    auth = Auth(catalog)
    assert auth.role_of(None) == "admin"          # open mode: everyone admin
    assert auth.role_of("anyone") == "admin"
    auth.create_user("root", "s3cret")            # enables auth
    auth.create_user("viewer1", "s3cret")          # viewer
    assert auth.role_of("root") == "admin"
    assert auth.role_of("viewer1") == "viewer"
    assert auth.role_of(None) == "viewer"          # anonymous under enabled auth = least priv
    assert auth.role_of("ghost") == "viewer"       # unknown user = least priv
    catalog.close()
