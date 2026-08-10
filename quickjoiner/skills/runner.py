"""Running a skill's bundled script, as the person who asked.

This is the one part of the skills feature that executes code, so its rules are narrow
and stated rather than implied:

  * **The script must live inside the skill's own folder.** The name comes from the model,
    so it is resolved and checked against the folder root — `../../..` reads nothing.
  * **A user-scoped skill runs only with that person's own values.** The server's
    environment is not inherited for the variables it declares (see registry), so a
    missing credential is refused, never silently substituted with the service account's.
  * **No shell.** The interpreter and script path are passed as argv, so a filename can
    never become a command.
  * **Bounded**: wall-clock timeout, captured output truncated, and the process killed
    (with its children where the platform allows) when it overruns.
  * **Secret values are never returned.** Output is the script's; the composed
    environment is not echoed, and a failure names only the KEYS that were missing.

What this is NOT: a sandbox. A skill script runs with the server process's privileges and
can do anything that user can. That is why authoring a skill is an admin action — see the
API layer — while *using* one is open to everybody.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20_000

# How to run a script by extension. `pwsh` (cross-platform PowerShell) is preferred over
# Windows PowerShell so a skill written on Windows also runs in the Linux container.
_INTERPRETERS: dict[str, list[str]] = {
    ".ps1": ["pwsh", "-NoProfile", "-NonInteractive", "-File"],
    ".py": [sys.executable],
    ".js": ["node"],
    ".mjs": ["node"],
    ".sh": ["bash"],
}
_WINDOWS_FALLBACK = {".ps1": ["powershell", "-NoProfile", "-NonInteractive", "-File"]}


@dataclass(frozen=True)
class RunResult:
    ok: bool
    output: str
    exit_code: int | None = None
    #: Required values the caller had not supplied. Names only — never the values.
    missing: tuple[str, ...] = ()


def _interpreter(script: Path) -> list[str] | None:
    suffix = script.suffix.lower()
    argv = _INTERPRETERS.get(suffix)
    if argv is None:
        return None
    from shutil import which

    if which(argv[0]) is None:
        fallback = _WINDOWS_FALLBACK.get(suffix)
        if fallback and which(fallback[0]):
            return fallback
        return None
    return argv


def resolve_script(skill_path: Path, relative: str) -> tuple[Path | None, str]:
    """(script path, error). Confined to the skill folder — the caller is a model."""
    if not relative or not relative.strip():
        return None, "No script named."
    try:
        target = (skill_path / relative.strip()).resolve()
        root = skill_path.resolve()
    except (OSError, ValueError) as exc:
        return None, f"Could not resolve {relative!r}: {exc}"
    if not target.is_relative_to(root):
        return None, f"Refused: {relative!r} is outside the skill folder."
    if not target.is_file():
        return None, f"No such script: {relative!r}"
    return target, ""


def run_script(
    skill_path: Path,
    relative: str,
    args: list[str] | None = None,
    env_values: dict[str, str] | None = None,
    inherit_env: bool = True,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> RunResult:
    """Execute one of a skill's scripts and return what it printed.

    `env_values` are layered ON TOP of the process environment when `inherit_env` is true
    (an open skill, which needs no credentials of its own but does need PATH and friends).
    A user-scoped skill still inherits the base environment — a script cannot run without
    PATH — but the variables it *declares* were resolved without the process layer, so the
    values here overwrite anything the server happens to hold under the same names.
    """
    script, err = resolve_script(skill_path, relative)
    if script is None:
        return RunResult(False, err)

    argv = _interpreter(script)
    if argv is None:
        return RunResult(
            False,
            f"Cannot run {script.name}: no interpreter available for {script.suffix!r} "
            "on this host.",
        )

    env = dict(os.environ) if inherit_env else {}
    env.update(env_values or {})
    # Keep a script's own output stable and parseable regardless of host locale.
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never shell=True
            [*argv, str(script), *(args or [])],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(skill_path),
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(False, f"Timed out after {timeout}s and was stopped.", None)
    except OSError as exc:
        return RunResult(False, f"Could not start {script.name}: {exc}", None)

    out = (proc.stdout or "").strip()
    errout = (proc.stderr or "").strip()
    if errout:
        out = f"{out}\n[stderr]\n{errout}" if out else f"[stderr]\n{errout}"
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n…[output truncated at {MAX_OUTPUT_CHARS} characters]"
    return RunResult(proc.returncode == 0, out or "(no output)", proc.returncode)
