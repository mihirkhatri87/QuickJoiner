"""Agent Skills — the open format Claude Code and GitHub Copilot both read.

A skill is a folder holding `SKILL.md`: YAML frontmatter (`name`, `description`) plus a
markdown body, optionally with `references/` for progressive disclosure and `scripts/`
for executables. Nothing here is QuickJoiner-specific, so a skill written for Copilot
works unmodified — and one written here stays usable there.

Two things QuickJoiner adds, both as OPTIONAL frontmatter keys, because unknown keys are
ignored by the other runtimes and so cannot break a skill elsewhere:

  requires_env: [ELASTIC_URL, ELASTIC_API_KEY]   # what it needs to run
  scope: user | shared                            # who supplies those values

`requires_env` is what makes a skill *scoped*: with none declared a skill is **open** and
anyone can use it; declaring any means it only runs for a person who has supplied them.
That distinction is the whole point — a query-syntax skill needs no credentials, while one
that reaches a real system needs each user's own.

Discovery is deliberately read-only and side-effect-free: this module finds and parses
skills, and never executes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SKILL_FILE = "SKILL.md"
# A skill body is injected into a prompt, so it is bounded like any other tool output.
MAX_BODY_CHARS = 40_000
MAX_REFERENCE_CHARS = 40_000
MAX_SKILLS = 200

# `name` addresses a skill in a tool call and in a URL path, so it is constrained to
# characters that need no escaping in either.
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    #: Environment variables this skill needs. `()` is an author's explicit "none", and
    #: **None means the key was absent** — the distinction decides whether `detect_env`'s
    #: heuristic gets to speak (see `registry.sync_registry`).
    requires_env: tuple[str, ...] | None = ()
    #: Optional `scope:` frontmatter — "open" or "user". An explicit statement of who
    #: supplies the values, which overrides both the declaration and detection.
    declared_scope: str = ""
    #: Where it was found, for the UI to explain why a skill is present.
    origin: str = "workspace"
    references: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()
    #: Parse problems, surfaced rather than silently dropping the skill.
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def scoped(self) -> bool:
        """True when it needs per-user credentials before it can run."""
        return bool(self.requires_env)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter mapping, body). A file with no frontmatter, or frontmatter that
    isn't a mapping, yields {} and the whole text — a skill is still usable as prose."""
    m = _FRONTMATTER.match(text or "")
    if not m:
        return {}, text or ""
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, text[m.end():]
    return (data if isinstance(data, dict) else {}), text[m.end():]


def _env_list(value) -> tuple[tuple[str, ...], list[str]]:
    """Normalize `requires_env` from a list or a comma-separated string. Names that
    aren't valid environment-variable identifiers are dropped WITH a warning — silently
    ignoring one would make a skill look open when it isn't.

    Returns `None` for an ABSENT key and `()` for an explicitly empty one. Collapsing the
    two is not cosmetic: `requires_env: []` is an author stating "this needs nothing", and
    if it reads the same as saying nothing at all then `detect_env`'s heuristic overrides
    it — see `registry.sync_registry`.
    """
    if value is None:
        return None, []
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return (), [f"requires_env should be a list, got {type(value).__name__}"]
    out, warnings = [], []
    for raw in items:
        name = str(raw).strip()
        if not name:
            continue
        if not _ENV_NAME.match(name):
            warnings.append(f"ignored invalid environment variable name {name!r}")
            continue
        if name not in out:
            out.append(name)
    return tuple(out), warnings


def _listing(folder: Path, suffixes: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Relative paths of the files in one of a skill's subfolders, sorted. Bounded and
    non-recursive beyond a sane depth; a missing folder is simply empty."""
    if not folder.is_dir():
        return ()
    found = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        if suffixes and p.suffix.lower() not in suffixes:
            continue
        found.append(p.relative_to(folder.parent).as_posix())
        if len(found) >= 100:
            break
    return tuple(found)


def load_skill(folder: Path, origin: str = "workspace") -> Skill | None:
    """Read one skill folder, or None when it holds no readable SKILL.md.

    Never raises: discovery runs on every request that builds an agent, and one
    malformed folder must not take the whole catalogue down with it.
    """
    try:
        md = folder / SKILL_FILE
        if not md.is_file():
            return None
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    front, _body = parse_frontmatter(text)
    warnings: list[str] = []
    name = str(front.get("name") or folder.name).strip()
    if not _NAME.match(name):
        # Fall back to the folder name, which the filesystem already constrained.
        if _NAME.match(folder.name):
            warnings.append(f"frontmatter name {name!r} is unusable; using folder name")
            name = folder.name
        else:
            return None

    description = str(front.get("description") or "").strip()
    if not description:
        warnings.append("no description — the agent cannot tell when to use this skill")

    requires_env, env_warnings = _env_list(front.get("requires_env"))
    warnings.extend(env_warnings)

    # `scope:` was documented in this module's own header from the start and never read.
    # It is the author's explicit statement of who supplies the values, so it outranks
    # both the declaration and detection.
    declared_scope = str(front.get("scope") or "").strip().lower()
    if declared_scope and declared_scope not in ("open", "user", "shared"):
        warnings.append(f"ignored unrecognised scope {declared_scope!r}")
        declared_scope = ""
    if declared_scope == "shared":  # the header's older spelling of workspace-wide values
        declared_scope = "user"

    return Skill(
        name=name,
        description=description,
        path=folder,
        requires_env=requires_env,
        declared_scope=declared_scope,
        origin=origin,
        references=_listing(folder / "references", (".md", ".txt", ".json", ".yaml", ".yml")),
        scripts=_listing(folder / "scripts"),
        warnings=tuple(warnings),
    )


# Environment variables a script reads because the OS provides them — never credentials,
# and suggesting them as "secrets you must supply" would bury the real ones in noise.
# (Measured against real skills: LOCALAPPDATA and ProgramFiles were the two most common
# reads across the whole set.)
_SYSTEM_ENV = frozenset({
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "USERNAME", "USER", "HOSTNAME", "COMPUTERNAME",
    "TEMP", "TMP", "TMPDIR", "PWD", "CD", "SHELL", "COMSPEC", "OS", "PROCESSOR_ARCHITECTURE",
    "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "SYSTEMROOT",
    "WINDIR", "SYSTEMDRIVE", "PYTHONPATH", "VIRTUAL_ENV", "LANG", "LC_ALL", "TERM",
})

# How each language reads an environment variable, keyed by the file types skills are
# actually written in. Scoping the pattern to the language matters: `${NAME}` is a shell
# env read but in JavaScript it is template interpolation, and matching it everywhere
# turned a real skill's suggestions into 90 lines of local variable names.
_ENV_READS: dict[tuple[str, ...], re.Pattern] = {
    (".ps1", ".psm1"): re.compile(r"\$[Ee]nv:([A-Za-z_][A-Za-z0-9_]*)"),
    (".js", ".mjs", ".cjs", ".ts"): re.compile(
        r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)|process\.env\[[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\]"),
    (".py",): re.compile(
        r"os\.environ(?:\.get)?[\[(][\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
        r"|os\.getenv\([\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"),
    (".sh", ".bash", ".zsh"): re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?"),
    (".bat", ".cmd"): re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%"),
}

# Environment variables are conventionally SCREAMING_SNAKE. Requiring it is what keeps a
# language's own syntax (a JS `process.env` read is fine, but PowerShell `$env:` sits
# beside hundreds of `$localVariable` uses) from flooding the suggestions.
_ENV_SHAPE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def detect_env(skill: Skill, max_files: int = 40) -> tuple[str, ...]:
    """Environment variables a skill's scripts appear to read.

    A SUGGESTION, never a declaration: `requires_env` in the frontmatter is what actually
    scopes a skill, and this only exists so a person adopting an existing skill is shown
    "this looks like it needs TFS_PAT, TFS_COLLECTION" instead of having to read its
    scripts. Presented as detected, so a wrong guess costs a dismissed suggestion.
    """
    names: list[str] = []
    root = skill.path / "scripts"
    if not root.is_dir():
        return ()
    try:
        files = [p for p in sorted(root.rglob("*")) if p.is_file()][:max_files]
    except OSError:
        return ()
    for p in files:
        pattern = next((r for exts, r in _ENV_READS.items() if p.suffix.lower() in exts), None)
        if pattern is None:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in pattern.finditer(text):
            name = next((g for g in m.groups() if g), None)
            if not name or not _ENV_SHAPE.match(name):
                continue
            if name.upper() in _SYSTEM_ENV or name in names:
                continue
            names.append(name)
    return tuple(sorted(names))


def skill_body(skill: Skill) -> str:
    """The SKILL.md body (frontmatter stripped), bounded. This is what gets injected
    when a skill is actually invoked, so it is read fresh — editing a skill takes effect
    on the next call rather than needing a restart."""
    try:
        text = (skill.path / SKILL_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(could not read {SKILL_FILE}: {exc})"
    _front, body = parse_frontmatter(text)
    body = body.strip()
    if len(body) > MAX_BODY_CHARS:
        return body[:MAX_BODY_CHARS] + f"\n\n…[truncated at {MAX_BODY_CHARS} characters]"
    return body


def read_reference(skill: Skill, relative: str) -> str:
    """One of a skill's bundled files, for progressive disclosure.

    The path is resolved and checked to be INSIDE the skill folder: `relative` comes from
    the model, and "../../.." would otherwise read anything the server can.
    """
    try:
        target = (skill.path / relative).resolve()
        root = skill.path.resolve()
        if not target.is_relative_to(root):
            return f"Refused: {relative!r} is outside the skill folder."
        if not target.is_file():
            return f"No such file in {skill.name}: {relative!r}"
        text = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return f"(could not read {relative!r}: {exc})"
    if len(text) > MAX_REFERENCE_CHARS:
        return text[:MAX_REFERENCE_CHARS] + f"\n\n…[truncated at {MAX_REFERENCE_CHARS} characters]"
    return text


def discover(roots: list[tuple[Path, str]]) -> list[Skill]:
    """Every skill under the given (folder, origin) roots, first origin winning a name
    clash — so a workspace-managed skill deliberately shadows a same-named personal one
    rather than the order depending on the filesystem."""
    found: dict[str, Skill] = {}
    for root, origin in roots:
        try:
            if not root.is_dir():
                continue
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            if len(found) >= MAX_SKILLS:
                return list(found.values())
            skill = load_skill(child, origin)
            if skill and skill.name not in found:
                found[skill.name] = skill
    return list(found.values())
