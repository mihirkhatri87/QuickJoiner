"""Agent Skills: discovery, scoping, per-user secrets, and refusal.

The security-shaped rules are what these pin: a user-scoped skill must never borrow the
server's credentials, a model-supplied path must never escape the skill folder, and a
secret must never be readable back out. Note what is deliberately NOT asserted anywhere —
a secret's VALUE. Tests print and compare layer names and key names only; an earlier
version of this file resolved values and printed real credentials from the process
environment into a log.
"""

import json
import os

import pytest

from quickjoiner.memory.catalog import Catalog
from quickjoiner.skills import (
    SCOPE_OPEN, SCOPE_USER, SecretStore, WORKSPACE_OWNER, build_skill_tools,
    catalogue_prompt, detect_env, discover, parse_frontmatter, runnable, sync_registry,
)

OPEN_SKILL = """---
name: query-logs
description: Query application logs. Use when asked to view or search logs.
---

# Query Logs

Always use index `filebeat-loglevel-6-or-7-*`.
"""

SCOPED_SKILL = """---
name: deploy-control
description: Control deployments.
requires_env: [DEPLOY_TOKEN, DEPLOY_URL]
---

# Deploy Control
Run scripts/deploy.ps1.
"""


def _write_skill(root, folder, body, scripts=None, references=None):
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for name, text in (scripts or {}).items():
        (d / "scripts").mkdir(exist_ok=True)
        (d / "scripts" / name).write_text(text, encoding="utf-8")
    for name, text in (references or {}).items():
        (d / "references").mkdir(exist_ok=True)
        (d / "references" / name).write_text(text, encoding="utf-8")
    return d


@pytest.fixture
def env(tmp_path):
    """A workspace with one open skill and one scoped skill."""
    root = tmp_path / "skills"
    _write_skill(root, "query-logs", OPEN_SKILL,
                 references={"examples.md": "## Example 1\nA worked query."})
    _write_skill(root, "deploy-control", SCOPED_SKILL,
                 scripts={"deploy.ps1": "Write-Output $env:DEPLOY_TOKEN"})
    catalog = Catalog(tmp_path / "catalog.db")
    store = SecretStore(catalog, tmp_path)
    configs = sync_registry(catalog, discover([(root, "workspace")]))
    return {"catalog": catalog, "store": store, "configs": configs, "root": root,
            "tmp": tmp_path}


# -- format compatibility -----------------------------------------------------

def test_reads_the_open_agent_skills_format():
    """The same layout Claude Code and GitHub Copilot read — so a skill written for
    either works here unmodified, and stays working there."""
    front, body = parse_frontmatter(OPEN_SKILL)
    assert front["name"] == "query-logs"
    assert body.strip().startswith("# Query Logs")


def test_a_file_without_frontmatter_is_still_a_skill():
    front, body = parse_frontmatter("# Just prose\nno frontmatter here")
    assert front == {} and body.startswith("# Just prose")


# -- scoping ------------------------------------------------------------------

def test_a_skill_needing_nothing_is_open_to_everyone(env):
    c = next(c for c in env["configs"] if c.name == "query-logs")
    assert c.scope == SCOPE_OPEN and not c.required_env
    assert runnable(c, env["store"], "anyone") == (True, [])


def test_a_skill_declaring_env_is_user_scoped_on_arrival(env):
    """The upload path: QuickJoiner decides the scope from what the skill needs, so an
    admin doesn't have to classify it by hand."""
    c = next(c for c in env["configs"] if c.name == "deploy-control")
    assert c.scope == SCOPE_USER
    assert c.required_env == ("DEPLOY_TOKEN", "DEPLOY_URL")


def test_undeclared_requirements_are_detected_from_the_scripts(tmp_path):
    """Real skills rarely declare anything, so the requirement is inferred from what
    their scripts actually read — which is what makes an existing Copilot skill land here
    already scoped."""
    root = tmp_path / "skills"
    _write_skill(root, "tfs", "---\nname: tfs\ndescription: d\n---\nbody",
                 scripts={"go.ps1": "$env:TFS_PAT; $env:TFS_COLLECTION; $localVar",
                          "helper.js": "process.env.OCTOPUS_URL"})
    configs = sync_registry(Catalog(tmp_path / "c.db"), discover([(root, "workspace")]))
    assert configs[0].scope == SCOPE_USER
    assert configs[0].required_env == ("OCTOPUS_URL", "TFS_COLLECTION", "TFS_PAT")


def test_detection_ignores_system_variables_and_language_syntax(tmp_path):
    """`${x}` is a shell env read but JavaScript template interpolation, and LOCALAPPDATA
    is not a credential. Both flooded the first version's suggestions."""
    root = tmp_path / "skills"
    skill = _write_skill(root, "noisy", "---\nname: noisy\ndescription: d\n---\nb",
                         scripts={"a.js": "`${localName}` + process.env.REAL_TOKEN",
                                  "b.ps1": "$env:LOCALAPPDATA; $env:APPDATA"})
    (found,) = discover([(root, "workspace")])
    assert detect_env(found) == ("REAL_TOKEN",)
    assert skill.exists()


# -- per-user gating ----------------------------------------------------------

def test_a_user_scoped_skill_refuses_until_that_user_supplies_its_values(env):
    c = next(c for c in env["configs"] if c.name == "deploy-control")
    ok, missing = runnable(c, env["store"], "alice")
    assert not ok and missing == ["DEPLOY_TOKEN", "DEPLOY_URL"]

    env["store"].set("alice", "DEPLOY_TOKEN", "x")
    env["store"].set("alice", "DEPLOY_URL", "y")
    assert runnable(c, env["store"], "alice") == (True, [])


def test_one_users_secrets_do_not_enable_another(env):
    """The whole point of user scope: everyone acts as themselves."""
    c = next(c for c in env["configs"] if c.name == "deploy-control")
    env["store"].set("alice", "DEPLOY_TOKEN", "x")
    env["store"].set("alice", "DEPLOY_URL", "y")
    assert runnable(c, env["store"], "bob")[0] is False


def test_a_user_scoped_skill_never_borrows_the_servers_credentials(env, monkeypatch):
    """A value the SERVER holds is not this person's. Falling back to it would run the
    skill as somebody else while looking like success, so process env is excluded."""
    monkeypatch.setenv("DEPLOY_TOKEN", "the-service-account-token")
    monkeypatch.setenv("DEPLOY_URL", "https://server-default")
    c = next(c for c in env["configs"] if c.name == "deploy-control")
    ok, missing = runnable(c, env["store"], "alice")
    assert not ok and missing == ["DEPLOY_TOKEN", "DEPLOY_URL"]


def test_workspace_values_cover_shared_settings_and_a_user_can_override(env):
    """An admin supplies the shared endpoint once; each person still brings their own
    credential, and may override the shared value."""
    store = env["store"]
    store.set(WORKSPACE_OWNER, "DEPLOY_URL", "workspace")
    store.set("alice", "DEPLOY_TOKEN", "alice-token")
    c = next(c for c in env["configs"] if c.name == "deploy-control")
    assert runnable(c, store, "alice") == (True, [])

    values, _ = store.resolve("alice", ["DEPLOY_URL"], include_process_env=False)
    assert values["DEPLOY_URL"] == "workspace"
    store.set("alice", "DEPLOY_URL", "alice-override")
    values, _ = store.resolve("alice", ["DEPLOY_URL"], include_process_env=False)
    assert values["DEPLOY_URL"] == "alice-override"


# -- storage ------------------------------------------------------------------

def test_secrets_are_encrypted_at_rest(env):
    """A copied catalog.db must not hand over anyone's credentials — the key lives in a
    separate file that such copies don't carry."""
    env["store"].set("alice", "DEPLOY_TOKEN", "super-secret-value")
    rows = env["catalog"]._conn.execute("SELECT value_enc FROM skill_secrets").fetchall()
    assert rows and all("super-secret-value" not in r[0] for r in rows)


def test_the_store_exposes_key_names_but_never_values(env):
    env["store"].set("alice", "DEPLOY_TOKEN", "v")
    assert env["store"].keys("alice") == ["DEPLOY_TOKEN"]
    assert not hasattr(env["store"], "get")  # no read-a-secret-back API exists


def test_admin_configuration_survives_rediscovery(env):
    """`tfs-control` detects nine variables, several of them optional tuning knobs, so an
    admin narrows the list in the UI. Re-running discovery must not revert that."""
    env["catalog"].update_skill_config("deploy-control", required_env=json.dumps(["DEPLOY_TOKEN"]))
    again = sync_registry(env["catalog"], discover([(env["root"], "workspace")]))
    c = next(c for c in again if c.name == "deploy-control")
    assert c.required_env == ("DEPLOY_TOKEN",)


# -- tools --------------------------------------------------------------------

def test_only_names_and_descriptions_ride_the_prompt(env):
    """Progressive disclosure is what makes a large library affordable: the 20k-character
    body is fetched on demand, never injected up front."""
    prompt = catalogue_prompt(env["configs"], "alice", env["store"])
    assert "query-logs" in prompt and "deploy-control" in prompt
    assert "filebeat-loglevel-6-or-7-*" not in prompt  # the body stayed out
    assert "UNAVAILABLE" in prompt  # and the scoped one is marked, not hidden


def test_open_skill_returns_the_body_and_lists_its_references(env):
    tools = {t.spec.name: t for t in build_skill_tools(env["configs"], "alice", env["store"])}
    body = tools["open_skill"].run(name="query-logs")
    assert "filebeat-loglevel-6-or-7-*" in body
    assert "references/examples.md" in body


def test_reading_a_file_outside_the_skill_folder_is_refused(env):
    """`path` comes from the model, so traversal has to be impossible, not unlikely."""
    tools = {t.spec.name: t for t in build_skill_tools(env["configs"], "alice", env["store"])}
    out = tools["read_skill_file"].run(name="query-logs", path="../../../../etc/passwd")
    assert "Refused" in out


def test_running_a_scoped_script_without_credentials_names_what_is_missing(env):
    tools = {t.spec.name: t for t in build_skill_tools(env["configs"], "alice", env["store"])}
    out = tools["run_skill_script"].run(name="deploy-control", script="scripts/deploy.ps1")
    assert "DEPLOY_TOKEN" in out and "DEPLOY_URL" in out
    assert "Settings" in out  # tells them where to fix it


def test_an_unknown_skill_lists_what_does_exist(env):
    tools = {t.spec.name: t for t in build_skill_tools(env["configs"], "alice", env["store"])}
    out = tools["open_skill"].run(name="nope")
    assert "query-logs" in out and "deploy-control" in out


def test_secret_status_reports_readiness_without_values(env):
    env["store"].set("alice", "DEPLOY_TOKEN", "v")
    tools = {t.spec.name: t for t in build_skill_tools(env["configs"], "alice", env["store"])}
    out = tools["my_skill_secrets"].run()
    assert "DEPLOY_URL" in out and "MISSING" in out
    assert "v" not in out.replace("deploy-control", "").replace("query-logs", "")


# -- execution ----------------------------------------------------------------

def test_a_script_runs_with_the_users_values(env, tmp_path):
    """Python is always available here; the shipped interpreter table also covers
    PowerShell/Node/bash where the host provides them."""
    from quickjoiner.skills.runner import run_script

    _write_skill(env["root"], "echoer", "---\nname: echoer\ndescription: d\n---\nb",
                 scripts={"show.py": "import os; print('SEEN:' + os.environ.get('DEPLOY_URL', 'none'))"})
    result = run_script(env["root"] / "echoer", "scripts/show.py",
                        env_values={"DEPLOY_URL": "from-the-user"})
    assert result.ok and "SEEN:from-the-user" in result.output


def test_a_script_outside_the_folder_is_refused(env):
    from quickjoiner.skills.runner import run_script

    result = run_script(env["root"] / "query-logs", "../../../evil.py")
    assert not result.ok and "outside the skill folder" in result.output


# -- installation --------------------------------------------------------------

def _zip_bytes(entries: dict) -> bytes:
    """A zip built in memory. `entries` maps archive path -> text."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_an_uploaded_zip_becomes_a_discoverable_skill(tmp_path):
    from quickjoiner.skills import discover, install_zip

    installed = install_zip(tmp_path, _zip_bytes({
        "query-logs/SKILL.md": OPEN_SKILL,
        "query-logs/references/examples.md": "worked examples",
    }))
    assert installed.name == "query-logs" and not installed.replaced
    found = discover([(tmp_path / "skills", "workspace")])
    assert [s.name for s in found] == ["query-logs"]
    assert found[0].references == ("references/examples.md",)


def test_a_zip_made_from_inside_the_folder_installs_too(tmp_path):
    """`SKILL.md` at the archive root is as normal as `my-skill/SKILL.md`, so the name
    comes from the frontmatter rather than from a folder that isn't there."""
    from quickjoiner.skills import install_zip

    installed = install_zip(tmp_path, _zip_bytes({"SKILL.md": OPEN_SKILL}))
    assert installed.name == "query-logs"
    assert (tmp_path / "skills" / "query-logs" / "SKILL.md").is_file()


def test_a_zip_that_escapes_the_target_folder_writes_nothing_outside_it(tmp_path):
    """The traversal case: a member naming `../../` must not land outside the skill."""
    from quickjoiner.skills import install_zip

    install_zip(tmp_path, _zip_bytes({
        "evil/SKILL.md": "---\nname: evil\ndescription: d\n---\nbody",
        "evil/../../escaped.txt": "should never be written",
    }))
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert (tmp_path / "skills" / "evil" / "SKILL.md").is_file()


def test_a_zip_without_a_skill_manifest_is_refused(tmp_path):
    from quickjoiner.skills import InstallError, install_zip

    with pytest.raises(InstallError, match="no SKILL.md"):
        install_zip(tmp_path, _zip_bytes({"notes/readme.md": "hello"}))


def test_reinstalling_replaces_the_folder_so_no_stale_script_survives(tmp_path):
    """A re-upload is total: a script the new version no longer mentions must not remain
    behind to be run by a body that has forgotten it."""
    from quickjoiner.skills import install_zip

    install_zip(tmp_path, _zip_bytes({
        "query-logs/SKILL.md": OPEN_SKILL,
        "query-logs/scripts/old.py": "print('stale')",
    }))
    assert (tmp_path / "skills" / "query-logs" / "scripts" / "old.py").is_file()
    again = install_zip(tmp_path, _zip_bytes({"query-logs/SKILL.md": OPEN_SKILL}))
    assert again.replaced
    assert not (tmp_path / "skills" / "query-logs" / "scripts" / "old.py").exists()


def test_admin_configuration_survives_a_reinstall(tmp_path):
    """The files are replaced; QuickJoiner's decision about the skill is not."""
    from quickjoiner.skills import discover, install_zip, sync_registry

    catalog = Catalog(tmp_path / "catalog.db")
    root = tmp_path / "skills"
    install_zip(tmp_path, _zip_bytes({"query-logs/SKILL.md": OPEN_SKILL}))
    sync_registry(catalog, discover([(root, "workspace")]))
    catalog.update_skill_config("query-logs", scope=SCOPE_USER,
                                required_env=json.dumps(["LOG_TOKEN"]))

    install_zip(tmp_path, _zip_bytes({"query-logs/SKILL.md": OPEN_SKILL}))
    config = sync_registry(catalog, discover([(root, "workspace")]))[0]
    assert config.scope == SCOPE_USER and config.required_env == ("LOG_TOKEN",)


def test_uninstall_removes_the_folder_and_refuses_a_path_outside_it(tmp_path):
    from quickjoiner.skills import install_zip, uninstall

    install_zip(tmp_path, _zip_bytes({"query-logs/SKILL.md": OPEN_SKILL}))
    assert uninstall(tmp_path, "query-logs")
    assert not (tmp_path / "skills" / "query-logs").exists()
    assert not uninstall(tmp_path, "../../etc")


def test_one_unsafe_member_does_not_reject_an_otherwise_valid_skill(tmp_path):
    """An absolute path alongside real files must be DROPPED, not fatal.

    It defeats the common-root detection if it is still in the list when that is computed,
    which rejected the whole archive with a misleading 'no SKILL.md' — for an archive that
    plainly has one.
    """
    from quickjoiner.skills import discover, install_zip

    installed = install_zip(tmp_path, _zip_bytes({
        "query-logs/SKILL.md": OPEN_SKILL,
        "query-logs/references/examples.md": "worked examples",
        "/etc/passwd": "root:x:0:0",
    }))
    assert installed.files == 2  # the third is dropped, not written
    assert [s.name for s in discover([(tmp_path / "skills", "workspace")])] == ["query-logs"]
    assert not (tmp_path.parent / "etc").exists()


def test_an_archive_of_nothing_but_unsafe_paths_is_refused(tmp_path):
    from quickjoiner.skills import InstallError, install_zip

    with pytest.raises(InstallError, match="unsafe to extract"):
        install_zip(tmp_path, _zip_bytes({"/tmp/evil.sh": "rm -rf /"}))
