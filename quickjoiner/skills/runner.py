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
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20_000
# A prompting script is answered from this, not from the console. Bounded because it is
# model-authored text going to a process's stdin.
MAX_STDIN_CHARS = 4_000

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


def split_args(text: str) -> list[str]:
    """One argument string -> argv, correct for BOTH quoting and Windows paths.

    Neither `shlex` mode is right on its own, and picking either alone is a silent
    corruption rather than an error:

      * `posix=True` treats a backslash as an escape, so `-Path C:\\Users\\x` arrives as
        `C:Usersx` — every Windows path in a skill's arguments quietly mangled.
      * `posix=False` preserves backslashes but leaves the QUOTE CHARACTERS inside the
        token, so `-SearchTerm "subscription installed"` passes a value that literally
        begins and ends with `"`. Measured end-to-end through `run_skill_script`: the
        script received `['-SearchTerm', '"subscription installed"']`.

    So: split non-posix to keep backslashes, then strip ONE matching pair of surrounding
    quotes per token. `""` stays a deliberate empty argument; a lone quote is left alone;
    an inner quote (`'{"a":1}'`) survives because only the outer pair is removed.

    Unbalanced quotes fall back to a whitespace split rather than raising — an argument
    string is model-authored, and a bad quote should degrade, not fail the whole call.
    """
    text = (text or "").strip()
    if not text:
        return []
    text = _rejoin_equals_quoted(text)
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        tokens = text.split()
    out = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        out.append(token)
    return out


#: `--key="a b"` — the GNU/.NET `=`-joined spelling. `shlex` only opens a quoted token
#: when the quote STARTS one, so here the quote is interior, the split lands on the space
#: inside it, and the value silently arrives in two pieces.
_EQUALS_QUOTED = re.compile(r"""(^|\s)(--?[A-Za-z0-9][\w.-]*)=(["'])(.*?)\3""")


def _rejoin_equals_quoted(text: str) -> str:
    """Re-quote `--key="a b"` around the WHOLE argument, so it splits as one token.

    Deliberately not rewritten to `--key "a b"`: that is a different argument form, and a
    parser that only accepts the joined spelling would then fail. Quoting the whole thing
    keeps `--key=a b` intact, which is what was written.
    """
    return _EQUALS_QUOTED.sub(lambda m: f'{m.group(1)}"{m.group(2)}={m.group(4)}"', text)


@dataclass(frozen=True)
class RunResult:
    ok: bool
    output: str
    exit_code: int | None = None
    #: Required values the caller had not supplied. Names only — never the values.
    missing: tuple[str, ...] = ()


def _interpreter(script: Path) -> tuple[list[str], bool] | None:
    """(argv, is_windows_powershell_fallback), or None when nothing can run it."""
    suffix = script.suffix.lower()
    argv = _INTERPRETERS.get(suffix)
    if argv is None:
        return None
    from shutil import which

    if which(argv[0]) is None:
        fallback = _WINDOWS_FALLBACK.get(suffix)
        if fallback and which(fallback[0]):
            return fallback, True
        return None
    return argv, False


def encoding_hint(script: Path) -> str:
    """Why a Windows PowerShell 5.1 run may have failed for a reason it did not name.

    5.1 has no `-Encoding` for `-File`: a script with no BOM is read in the system ANSI
    codepage. A UTF-8 em dash is then three cp1252 characters ending in U+201D — a SMART
    DOUBLE QUOTE, which PowerShell accepts as a string delimiter. So one em dash inside a
    double-quoted string silently terminates it, and the parser reports "Missing closing
    '}'" against a completely unrelated line further down. Nothing in that error mentions
    encoding, so it reads as a brace bug that isn't there (hit while testing a real skill).

    Returned only as an ADDITION to a failure, never as a refusal: a genuinely
    ANSI-encoded script containing the same bytes runs correctly, and a false refusal
    would be worse than a redundant note.
    """
    try:
        raw = script.read_bytes()
    except OSError:
        return ""
    if raw.startswith(b"\xef\xbb\xbf") or all(b < 128 for b in raw):
        return ""  # a BOM is honoured, and pure ASCII cannot be misread
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""  # not UTF-8, so ANSI is very likely the right reading
    return (
        f"\n[note] {script.name} is UTF-8 with no BOM and contains non-ASCII characters, "
        "and it ran under Windows PowerShell 5.1 because `pwsh` is not on PATH. 5.1 reads "
        "such a file in the system ANSI codepage, where an em dash or curly quote becomes "
        "a smart quote that silently ends a string — the parse error it reports then names "
        "an unrelated line. Re-save the script as UTF-8 with BOM (or ASCII-only), or "
        "install PowerShell 7."
    )


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
    stdin: str | None = None,
) -> RunResult:
    """Execute one of a skill's scripts and return what it printed.

    `env_values` are layered ON TOP of the process environment when `inherit_env` is true
    (an open skill, which needs no credentials of its own but does need PATH and friends).
    A user-scoped skill still inherits the base environment — a script cannot run without
    PATH — but the variables it *declares* were resolved without the process layer, so the
    values here overwrite anything the server happens to hold under the same names.

    `stdin` is the escape hatch for a script that PROMPTS and that you cannot edit: its
    text is fed to the process's standard input, one answer per line. It is a last resort,
    not the design — an agent has no console, so a script it can reach should take
    parameters and default the rest. Answering blind means guessing the prompts' order,
    and a script that changes its questions will silently take the wrong answers.

    With no `stdin`, standard input is **/dev/null rather than inherited**. A server's own
    stdin is not the caller's, so inheriting it lets a prompting script block until the
    wall-clock timeout — a 120s stall reported as a timeout, with nothing naming the
    prompt. Against /dev/null the read hits EOF at once and the script fails immediately
    and legibly.
    """
    script, err = resolve_script(skill_path, relative)
    if script is None:
        return RunResult(False, err)

    chosen = _interpreter(script)
    if chosen is None:
        return RunResult(
            False,
            f"Cannot run {script.name}: no interpreter available for {script.suffix!r} "
            "on this host.",
        )
    argv, windows_powershell = chosen

    env = dict(os.environ) if inherit_env else {}
    env.update(env_values or {})
    # Keep a script's own output stable and parseable regardless of host locale.
    env.setdefault("PYTHONIOENCODING", "utf-8")

    feed = None
    stdin_clipped = False
    if stdin:
        feed = stdin[:MAX_STDIN_CHARS]
        # A silently clipped final answer becomes a PLAUSIBLE WRONG answer to a prompt,
        # not an error — the one failure this module must not produce quietly. Reported
        # alongside the output, in keeping with how truncation is handled everywhere else.
        stdin_clipped = len(stdin) > MAX_STDIN_CHARS
        if not feed.endswith("\n"):
            feed += "\n"  # an unterminated final answer is never consumed by a line read
        # PowerShell is normally launched `-NonInteractive` so a prompting script fails
        # fast instead of hanging. That flag is precisely what would defeat the answers:
        # under it `Read-Host` throws regardless of what is on stdin. Dropped for this
        # run only — the very scripts this hatch exists for are the PowerShell ones.
        argv = [a for a in argv if a != "-NonInteractive"]

    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never shell=True
            [*argv, str(script), *(args or [])],
            input=feed,
            stdin=None if feed is not None else subprocess.DEVNULL,
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
    ok = proc.returncode == 0
    if stdin_clipped:
        out += (f"\n[note] the answers supplied were longer than {MAX_STDIN_CHARS} "
                "characters and were cut off — the script may have read a truncated answer.")
    if not ok and windows_powershell:
        # Appended AFTER truncation so a long failure cannot push the diagnosis out.
        out += encoding_hint(script)
    return RunResult(ok, out or "(no output)", proc.returncode)
