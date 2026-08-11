"""Skills as agent tools, following the format's progressive-disclosure design.

Only the NAME and DESCRIPTION of each available skill go into the system prompt — that is
what makes a large skill library affordable. The body is fetched when the model decides a
skill applies, and a skill's `references/` only when its body points at them.

Four tools, deliberately small:

  open_skill        - the SKILL.md body (the instructions)
  read_skill_file   - one bundled reference, on demand
  run_skill_script  - execute a bundled script as the calling user
  my_skill_secrets  - which required values the caller has supplied, and which are missing

The last one exists so the agent can answer "why can't you do that?" precisely — naming
the variables the person still needs to set — instead of failing vaguely.
"""

from __future__ import annotations

from quickjoiner.llm.base import AgentTool, ToolSpec
from quickjoiner.skills.loader import read_reference, skill_body
from quickjoiner.skills.registry import SkillConfig, runnable, skill_environment
from quickjoiner.skills.runner import run_script, split_args

MAX_CATALOG_SKILLS = 60
# A description rides in EVERY system prompt, so it is bounded: it only has to be enough
# to decide whether a skill applies, and `open_skill` then supplies the whole thing.
# (Measured on a real library: one skill's description alone ran to ~1,000 characters,
# and seven of them would have cost more prompt than most answers.)
MAX_DESCRIPTION_CHARS = 280


def _summarize(description: str) -> str:
    text = " ".join((description or "").split())
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    clipped = text[:MAX_DESCRIPTION_CHARS]
    cut = clipped.rfind(". ")
    if cut > MAX_DESCRIPTION_CHARS // 2:  # prefer a sentence boundary
        return clipped[: cut + 1]
    return clipped.rstrip() + "…"


def catalogue_prompt(configs: list[SkillConfig], user: str | None, store) -> str:
    """The skills section of the system prompt: one line each, plus whether it is ready.

    A skill whose credentials are missing is still LISTED — hiding it would leave the
    agent unable to explain why a capability the person can see in the UI isn't working.
    It is marked unavailable so the model offers the fix rather than attempting it.
    """
    usable = [c for c in configs if c.enabled][:MAX_CATALOG_SKILLS]
    if not usable:
        return ""
    lines = [
        "SKILLS — packaged expertise available to you. Each line is a name and when to use it.",
        "Call open_skill(name) to read one's full instructions BEFORE acting on its subject;",
        "the instructions may then point you at read_skill_file or run_skill_script.",
        "",
    ]
    for c in usable:
        ok, missing = runnable(c, store, user)
        state = "" if ok else f"  [UNAVAILABLE — {', '.join(missing)} not set by this user]"
        lines.append(f"- {c.name}: {_summarize(c.description) or '(no description)'}{state}")
    lines.append("")
    lines.append(
        "A skill marked UNAVAILABLE needs the named values set by THIS user in Settings → "
        "Skills. Say exactly which are missing; never guess a credential or use another "
        "user's."
    )
    return "\n".join(lines)


def build_skill_tools(configs: list[SkillConfig], user: str | None, store) -> list[AgentTool]:
    by_name = {c.name: c for c in configs if c.enabled}
    if not by_name:
        return []

    def _get(name: str) -> tuple[SkillConfig | None, str]:
        c = by_name.get((name or "").strip())
        if c is None:
            known = ", ".join(sorted(by_name)) or "none"
            return None, f"No skill named {name!r}. Available: {known}."
        return c, ""

    def open_skill(name: str) -> str:
        c, err = _get(name)
        if c is None:
            return err
        body = skill_body(c.skill)
        extras = []
        if c.skill.references:
            extras.append(
                "Bundled reference files (read with read_skill_file when the "
                f"instructions call for them): {', '.join(c.skill.references)}")
        if c.skill.scripts:
            ok, missing = runnable(c, store, user)
            if ok:
                extras.append(
                    f"Bundled scripts (run with run_skill_script): {', '.join(c.skill.scripts)}")
            else:
                extras.append(
                    "Bundled scripts CANNOT run for this user until they set: "
                    f"{', '.join(missing)}. Tell them exactly that.")
        return body + ("\n\n---\n" + "\n".join(extras) if extras else "")

    def read_skill_file(name: str, path: str) -> str:
        c, err = _get(name)
        return err if c is None else read_reference(c.skill, path)

    def run_skill_script(name: str, script: str, args: str = "", stdin: str = "") -> str:
        c, err = _get(name)
        if c is None:
            return err
        ok, missing = runnable(c, store, user)
        if not ok:
            return (
                f"Cannot run {c.name}: this skill runs with each user's own credentials, "
                f"and {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set "
                "for you. Set them in Settings → Skills, then try again. "
                "(Refusing rather than falling back to the server's own credentials, "
                "which would act as somebody else.)"
            )
        try:
            env = skill_environment(c, store, user)
        except Exception as exc:
            return f"Cannot run {c.name}: its secrets could not be read ({exc})."
        # Arguments arrive as one string because tool schemas vary in list support across
        # providers. `split_args` is quoting- AND Windows-path-correct; see its docstring
        # for why neither shlex mode alone is.
        result = run_script(c.skill.path, script, split_args(args), env_values=env,
                            stdin=stdin or None)
        header = "" if result.ok else f"[exit {result.exit_code}] "
        return header + result.output

    def my_skill_secrets() -> str:
        """What the calling user has supplied, and what each skill still needs."""
        lines = []
        for c in by_name.values():
            if not c.required_env:
                lines.append(f"- {c.name}: open (no credentials needed)")
                continue
            ok, missing = runnable(c, store, user)
            lines.append(
                f"- {c.name}: needs {', '.join(c.required_env)} — "
                + ("all set" if ok else f"MISSING {', '.join(missing)}")
            )
        return "\n".join(lines) or "No skills are configured."

    def _tool(name, desc, fn, props=None, required=None):
        return AgentTool(
            spec=ToolSpec(
                name=name, description=desc,
                input_schema={
                    "type": "object",
                    "properties": props or {},
                    "required": required or [],
                },
            ),
            fn=fn,
        )

    return [
        _tool("open_skill",
              "Read a skill's full instructions. Call this BEFORE acting on a subject a "
              "skill covers — its instructions carry specifics (query syntax, field names, "
              "required workflows) you would otherwise have to guess.",
              open_skill, {"name": {"type": "string", "description": "The skill's name."}},
              ["name"]),
        _tool("read_skill_file",
              "Read one of a skill's bundled reference files, when its instructions point "
              "at it. Paths are relative to the skill folder (e.g. 'references/examples.md').",
              read_skill_file,
              {"name": {"type": "string"}, "path": {"type": "string"}},
              ["name", "path"]),
        _tool("run_skill_script",
              "Run one of a skill's bundled scripts as the current user, with their own "
              "credentials. Only use a script the skill's instructions tell you to run. "
              "There is no console: a script that prompts will fail unless you answer it "
              "via `stdin`.",
              run_skill_script,
              {"name": {"type": "string"},
               "script": {"type": "string", "description": "e.g. 'scripts/Query-AppLogs.ps1'"},
               "args": {"type": "string",
                        "description": "Command-line arguments as one string. Quote values "
                                       "containing spaces: -SearchTerm \"disk full\"."},
               "stdin": {"type": "string",
                         "description": "LAST RESORT, for a script that prompts and cannot "
                                        "be changed: the answers, one per line, in the order "
                                        "asked. Prefer passing parameters — answering blind "
                                        "guesses the prompt order and takes the wrong answer "
                                        "if the script's questions differ. If a run fails on "
                                        "a prompt, read the prompt text in the output and "
                                        "retry with the answers here."}},
              ["name", "script"]),
        _tool("my_skill_secrets",
              "Which skills are ready for this user and which are still missing required "
              "values. Use when a skill is unavailable, to say exactly what they must set.",
              my_skill_secrets),
    ]
