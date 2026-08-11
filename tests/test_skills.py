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
import sys

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


# -- a skill's name and its folder need not agree ------------------------------
#
# `load_skill` takes the name from FRONTMATTER and only falls back to the folder. The CLI
# explicitly invites dropping a folder into <workspace>/skills, so the two can differ —
# and deriving the delete target from the name then made such a skill undeletable from the
# CLI, the API and the UI at once, while the API row still reported removable: true. Zip
# installs always agree, which is why nothing caught it.

def test_a_skill_whose_folder_differs_from_its_name_is_still_removable(tmp_path):
    from quickjoiner.skills import discover, uninstall

    root = tmp_path / "ws" / "skills"
    (root / "some-folder").mkdir(parents=True)
    (root / "some-folder" / "SKILL.md").write_text(
        "---\nname: query-app-logs\ndescription: d\n---\nbody", encoding="utf-8")

    skill = discover([(root, "workspace")])[0]
    assert skill.name == "query-app-logs" and skill.path.name == "some-folder"

    assert uninstall(tmp_path / "ws", skill.name) is False       # the old, name-derived way
    assert (root / "some-folder").exists()
    assert uninstall(tmp_path / "ws", skill.name, skill.path) is True
    assert not (root / "some-folder").exists()


def test_a_supplied_path_outside_the_workspace_is_still_refused(tmp_path):
    """The path argument must not become a delete-anything primitive."""
    from quickjoiner.skills import uninstall

    (tmp_path / "ws" / "skills").mkdir(parents=True)
    elsewhere = tmp_path / "somebody-elses"
    elsewhere.mkdir()
    assert uninstall(tmp_path / "ws", "x", elsewhere) is False
    assert elsewhere.exists()


# -- who supplies a skill's values: what the author said beats what we detected --

def test_an_explicitly_empty_requires_env_keeps_a_skill_open(tmp_path):
    """`requires_env: []` is an author saying "needs nothing". Reading it the same as
    saying nothing at all let detection override it — and a script reading one
    non-credential base URL then made the skill UNAVAILABLE to everybody."""
    from quickjoiner.memory.catalog import Catalog
    from quickjoiner.skills import SCOPE_OPEN, discover, sync_registry

    root = tmp_path / "skills"
    _write_skill(root, "svc",
                 "---\nname: svc\ndescription: d\nrequires_env: []\n---\nbody",
                 scripts={"go.ps1": 'Invoke-RestMethod $env:SOME_BASE_URL'})
    catalog = Catalog(tmp_path / "ws")
    config = sync_registry(catalog, discover([(root, "workspace")]))[0]
    assert config.scope == SCOPE_OPEN and config.required_env == ()
    catalog.close()


def test_scope_open_frontmatter_overrides_detection(tmp_path):
    """The `scope:` key was documented in loader.py's own header from the start and never
    parsed. It is the escape hatch for a skill whose scripts read a tuning flag."""
    from quickjoiner.memory.catalog import Catalog
    from quickjoiner.skills import SCOPE_OPEN, discover, sync_registry

    root = tmp_path / "skills"
    _write_skill(root, "flagged",
                 "---\nname: flagged\ndescription: d\nscope: open\n---\nbody",
                 scripts={"go.ps1": 'Write-Output $env:LOG_LEVEL_FILTER'})
    catalog = Catalog(tmp_path / "ws")
    config = sync_registry(catalog, discover([(root, "workspace")]))[0]
    assert config.scope == SCOPE_OPEN and config.required_env == ()
    catalog.close()


def test_an_absent_requires_env_still_falls_back_to_detection(tmp_path):
    """The common case must be unchanged: most skills declare nothing."""
    from quickjoiner.memory.catalog import Catalog
    from quickjoiner.skills import SCOPE_USER, discover, sync_registry

    root = tmp_path / "skills"
    _write_skill(root, "creds", "---\nname: creds\ndescription: d\n---\nbody",
                 scripts={"go.ps1": 'Invoke-RestMethod -Headers @{k=$env:DEPLOY_TOKEN}'})
    catalog = Catalog(tmp_path / "ws")
    config = sync_registry(catalog, discover([(root, "workspace")]))[0]
    assert config.scope == SCOPE_USER and "DEPLOY_TOKEN" in config.required_env
    catalog.close()


def test_an_unrecognised_scope_is_warned_about_rather_than_obeyed(tmp_path):
    from quickjoiner.skills import discover

    root = tmp_path / "skills"
    _write_skill(root, "odd", "---\nname: odd\ndescription: d\nscope: nonsense\n---\nb")
    skill = discover([(root, "workspace")])[0]
    assert skill.declared_scope == ""
    assert any("nonsense" in w for w in skill.warnings)


# -- argument splitting --------------------------------------------------------
#
# Neither shlex mode is right alone, and each is wrong SILENTLY: posix=True eats the
# backslashes out of a Windows path, posix=False leaves the quote characters inside the
# token. The second shipped, and a value with a space arrived wrapped in literal quotes.

@pytest.mark.parametrize("text, expected", [
    ('-SearchTerm "subscription installed"', ["-SearchTerm", "subscription installed"]),
    ("-SearchTerm 'a b'", ["-SearchTerm", "a b"]),
    (r"-Path C:\Users\khatrim\docs", ["-Path", r"C:\Users\khatrim\docs"]),
    (r'-Path "C:\Program Files\x" -Q 1', ["-Path", r"C:\Program Files\x", "-Q", "1"]),
    ("""-Json '{"a":1}'""", ["-Json", '{"a":1}']),   # only the OUTER pair is stripped
    ('-Empty ""', ["-Empty", ""]),                   # a deliberate empty argument
    ("", []),
    ("   ", []),
    ('unbalanced "quote', ["unbalanced", '"quote']),  # degrades, never raises
    # The `=`-joined GNU/.NET spelling: the quote is interior, so shlex split on the space
    # inside it and the value silently arrived in two pieces.
    ('--path="C:\\a b" --n=2', ["--path=C:\\a b", "--n=2"]),
    ("--msg='hello there'", ["--msg=hello there"]),
    ("-Out=\"a b\"", ["-Out=a b"]),
    # The joined FORM is preserved, not rewritten to two arguments: a parser accepting
    # only `--key=value` would break if we split it.
    ('--key="v"', ["--key=v"]),
])
def test_split_args_handles_quotes_and_windows_paths(text, expected):
    from quickjoiner.skills.runner import split_args

    assert split_args(text) == expected


def test_a_quoted_argument_reaches_the_script_without_its_quotes(env, tmp_path):
    """End-to-end through the tool, which is where the bug was observable."""
    _write_skill(env["root"], "argv", "---\nname: argv\ndescription: d\n---\nb",
                 scripts={"show.py": "import sys, json; print(json.dumps(sys.argv[1:]))"})
    configs = sync_registry(env["catalog"], discover([(env["root"], "workspace")]))
    tools = {t.spec.name: t for t in build_skill_tools(configs, "amy", env["store"])}
    out = tools["run_skill_script"].fn(
        name="argv", script="scripts/show.py", args='-SearchTerm "subscription installed"')
    assert json.loads(out.strip()) == ["-SearchTerm", "subscription installed"]


# -- stdin: the escape hatch for a script that prompts -------------------------

def test_stdin_answers_a_prompting_script(env):
    """An agent has no console. A script that cannot be edited is answered blind — a last
    resort, and the tool description says so, but it beats the capability being unusable."""
    _write_skill(env["root"], "asker", "---\nname: asker\ndescription: d\n---\nb",
                 scripts={"ask.py": "a = input('Range? '); b = input('Term? ');"
                                    " print(f'GOT {a}/{b}')"})
    configs = sync_registry(env["catalog"], discover([(env["root"], "workspace")]))
    tools = {t.spec.name: t for t in build_skill_tools(configs, "amy", env["store"])}
    out = tools["run_skill_script"].fn(name="asker", script="scripts/ask.py",
                                       stdin="7d\nsubscription\n")
    assert "GOT 7d/subscription" in out


def test_a_final_answer_without_a_newline_is_still_consumed(env):
    from quickjoiner.skills.runner import run_script

    _write_skill(env["root"], "asker2", "---\nname: asker2\ndescription: d\n---\nb",
                 scripts={"ask.py": "print('GOT ' + input())"})
    result = run_script(env["root"] / "asker2", "scripts/ask.py", stdin="no-trailing-newline")
    assert result.ok and "GOT no-trailing-newline" in result.output


def test_without_stdin_a_prompting_script_fails_fast_instead_of_hanging(env):
    """Standard input is /dev/null, not the server's own: inheriting it lets a prompt
    block for the whole 120s timeout and report nothing about the prompt."""
    import time as _time

    from quickjoiner.skills.runner import run_script

    _write_skill(env["root"], "hanger", "---\nname: hanger\ndescription: d\n---\nb",
                 scripts={"ask.py": "print('PROMPT'); input(); print('never')"})
    started = _time.monotonic()
    result = run_script(env["root"] / "hanger", "scripts/ask.py", timeout=30)
    assert not result.ok
    assert _time.monotonic() - started < 15      # EOF at once, not a timeout
    assert "PROMPT" in result.output             # and the prompt text is visible to retry


def test_stdin_is_bounded_and_says_when_it_clipped(env):
    """A silently clipped answer is a plausible WRONG answer to a prompt, not an error."""
    from quickjoiner.skills.runner import MAX_STDIN_CHARS, run_script

    _write_skill(env["root"], "counter", "---\nname: counter\ndescription: d\n---\nb",
                 scripts={"n.py": "import sys; print(len(sys.stdin.read()))"})
    result = run_script(env["root"] / "counter", "scripts/n.py", stdin="x" * 99_999)
    assert result.ok
    assert int(result.output.splitlines()[0].strip()) <= MAX_STDIN_CHARS + 1  # +1 newline
    assert "cut off" in result.output

    within = run_script(env["root"] / "counter", "scripts/n.py", stdin="short\n")
    assert "cut off" not in within.output


# -- the encoding trap under the Windows PowerShell fallback -------------------
#
# Windows PowerShell 5.1 has no `-Encoding` for `-File`: a BOM-less script is read in the
# system ANSI codepage, where a UTF-8 em dash ends in U+201D — a smart quote PowerShell
# accepts as a string delimiter. One em dash inside a double-quoted string therefore ends
# it early, and the parser blames an unrelated line for a missing brace. Hit while
# testing a real skill; nothing in the error mentions encoding.

def test_bom_less_utf8_earns_an_encoding_note(tmp_path):
    from quickjoiner.skills.runner import encoding_hint

    script = tmp_path / "s.ps1"
    script.write_bytes('Write-Output "a — b"\n'.encode("utf-8"))
    assert "UTF-8 with no BOM" in encoding_hint(script)


@pytest.mark.parametrize("data, why", [
    ('Write-Output "a - b"\n'.encode("utf-8"), "pure ASCII cannot be misread"),
    ('Write-Output "a — b"\n'.encode("utf-8-sig"), "a BOM is honoured by 5.1"),
    (b"Write-Output 'caf\xe9'\n", "already ANSI, so ANSI is the right reading"),
])
def test_no_note_when_the_encoding_is_not_the_problem(tmp_path, data, why):
    """A false note is cheap but a false REFUSAL would not be — which is why this is only
    ever appended to a failure, never used to block a run."""
    from quickjoiner.skills.runner import encoding_hint

    script = tmp_path / "s.ps1"
    script.write_bytes(data)
    assert encoding_hint(script) == "", why


def test_the_note_rides_a_failure_and_survives_output_truncation(tmp_path, monkeypatch):
    """It is appended AFTER truncation: a script that fails verbosely must not push the
    one line explaining why off the end."""
    import quickjoiner.skills.runner as runner_module

    skill = tmp_path / "sk"
    (skill / "scripts").mkdir(parents=True)
    script = skill / "scripts" / "s.ps1"
    script.write_bytes(('Write-Output "x — y"\n' * 50).encode("utf-8"))

    # Stand in for PowerShell so the test runs anywhere: the primary interpreter is
    # missing, so the WINDOWS FALLBACK path is taken — which is the branch that earns the
    # note. run_script appends the script path, which `python -c` ignores.
    noisy_failure = [sys.executable, "-c", "import sys; sys.stderr.write('E'*500); sys.exit(1)"]
    monkeypatch.setattr(runner_module, "_INTERPRETERS", {".ps1": ["definitely-not-a-real-shell"]})
    monkeypatch.setattr(runner_module, "_WINDOWS_FALLBACK", {".ps1": noisy_failure})
    monkeypatch.setattr(runner_module, "MAX_OUTPUT_CHARS", 50)

    result = runner_module.run_script(skill, "scripts/s.ps1")
    assert not result.ok
    assert "UTF-8 with no BOM" in result.output
    assert "output truncated" in result.output


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
