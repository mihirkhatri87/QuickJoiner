"""Installing a skill into the workspace, and removing one.

A skill arrives as a zip — the shape GitHub, Claude Code and Copilot all hand you — and
lands in `<workspace>/skills/<name>/`, the one root that travels with a deployment.

Extraction is the sharp edge here, so its rules are explicit:

  * **Every member is resolved and checked to stay inside the target folder.** A zip may
    name `../../etc/authorized_keys` or an absolute path, and a naive `extractall` writes
    it. Symlink entries are dropped for the same reason — they are a path escape that
    survives the check on the entry's own name.
  * **Bounded** by member count, per-member size and total uncompressed size: the header
    a zip declares costs nothing to inflate, which is the decompression-bomb shape the
    archive extractor next door already guards against.
  * **It must actually be a skill** — a zip with no `SKILL.md` is rejected before anything
    is written, rather than leaving a folder that discovery then ignores.

Installing is an admin act because a skill may carry scripts, and a script runs with the
server's privileges (see `runner`). Nothing here executes anything.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from quickjoiner.skills.loader import SKILL_FILE, _NAME, load_skill, parse_frontmatter

MAX_MEMBERS = 400
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024


class InstallError(ValueError):
    """The zip is not an installable skill. The message is shown to the user, so it says
    what is wrong rather than that something is."""


@dataclass(frozen=True)
class Installed:
    name: str
    path: Path
    files: int
    replaced: bool


def _members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise InstallError("the archive is empty")
    if len(infos) > MAX_MEMBERS:
        raise InstallError(f"too many files ({len(infos)}; the limit is {MAX_MEMBERS})")
    total = 0
    for info in infos:
        if info.file_size > MAX_MEMBER_BYTES:
            raise InstallError(
                f"{info.filename!r} is larger than {MAX_MEMBER_BYTES // (1024 * 1024)}MB")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise InstallError(
                f"the archive expands to more than {MAX_TOTAL_BYTES // (1024 * 1024)}MB")
        # An external_attr high byte of 0xA1FF marks a symlink. It points wherever it likes,
        # so the containment check on its own path would not save us.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise InstallError(f"{info.filename!r} is a symbolic link, which is not allowed")
    return infos


def _writable(name: str) -> bool:
    """Whether a member's path is one we would ever write.

    Absolute paths and `..` segments are dropped rather than sanitized — the point is to
    write nothing outside the skill folder, and there is no reading of such an entry that
    the author could have meant. Filtering them out HERE, before the common root is
    computed, matters: one stray `/etc/passwd` entry in an otherwise valid archive would
    otherwise make the top-level folders disagree, defeat root detection, and get the whole
    skill rejected with a misleading "no SKILL.md" — which it plainly has.
    """
    return bool(name) and not name.startswith("/") and ".." not in name.split("/")


def _strip_root(names: list[str]) -> str:
    """The single top-level folder every member shares, or "".

    A zip made from a folder carries `my-skill/SKILL.md`; one made from *inside* it carries
    `SKILL.md`. Both are normal, so the common prefix is removed rather than demanded.
    """
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(tops) == 1 and not any("/" not in n for n in names):
        return tops.pop()
    return ""


def _skill_name(zf: zipfile.ZipFile, member: str, fallback: str) -> str:
    """The name the skill declares, falling back to its folder. Validated the same way
    discovery validates it, so an installed skill is always addressable."""
    try:
        front, _ = parse_frontmatter(zf.read(member).decode("utf-8", errors="replace"))
        declared = str(front.get("name") or "").strip()
    except (KeyError, OSError, zipfile.BadZipFile):
        declared = ""
    for candidate in (declared, fallback):
        if candidate and _NAME.match(candidate):
            return candidate
    raise InstallError(
        "could not determine a usable skill name — give the folder a name of letters, "
        "digits, dots, dashes or underscores, or set `name:` in SKILL.md's frontmatter")


def install_zip(workspace: Path, data: bytes, fallback_name: str = "") -> Installed:
    """Extract a skill zip into `<workspace>/skills/<name>/`, replacing any same-named one.

    Replacement is deliberate and total: the folder is removed first, so a re-upload cannot
    leave a stale script from the previous version behind to be run by a body that no
    longer mentions it. QuickJoiner's stored configuration for the name is untouched — an
    admin's scope decision outlives the files, which is the registry's whole contract.
    """
    import io

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise InstallError(f"not a readable zip archive: {exc}") from exc

    with zf:
        infos = _members(zf)
        entries = [(i, i.filename.replace("\\", "/")) for i in infos]
        entries = [(i, n) for i, n in entries if _writable(n)]
        if not entries:
            raise InstallError("every path in the archive is unsafe to extract")
        names = [n for _i, n in entries]
        root = _strip_root(names)
        rel = {n: (n[len(root) + 1:] if root else n) for n in names}

        manifest = next((n for n, r in rel.items() if r == SKILL_FILE), None)
        if manifest is None:
            raise InstallError(
                f"no {SKILL_FILE} in the archive — a skill is a folder containing one")

        name = _skill_name(zf, manifest, root or fallback_name.strip())
        target = (workspace / "skills" / name).resolve()
        root_dir = (workspace / "skills").resolve()
        if not target.is_relative_to(root_dir):
            raise InstallError(f"unusable skill name {name!r}")

        replaced = target.exists()
        staging = target.parent / f".{name}.incoming"
        # Stage, then swap: a failure part-way through leaves the installed skill intact
        # rather than a half-written folder that discovery would happily load.
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            written = 0
            for info, source in entries:
                relative = rel[source]
                # Re-checked after the root strip: `_writable` cleared the archive path, and
                # this clears what we would actually write.
                if not _writable(relative):
                    continue
                dest = (staging / relative).resolve()
                if not dest.is_relative_to(staging.resolve()):
                    continue  # a path that escapes the folder is dropped, never written
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, length=64 * 1024)
                written += 1

            if load_skill(staging) is None:
                raise InstallError(
                    f"the archive's {SKILL_FILE} could not be parsed as a skill")

            if replaced:
                shutil.rmtree(target, ignore_errors=True)
            staging.replace(target)
        except InstallError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"could not write the skill: {exc}") from exc

    return Installed(name=name, path=target, files=written, replaced=replaced)


def uninstall(workspace: Path, name: str) -> bool:
    """Remove a workspace-installed skill's folder. False when there was nothing to remove.

    Only the workspace root is writable here: a skill discovered under a personal
    `~/.claude/skills` belongs to that person's own tooling, and deleting their file
    because QuickJoiner happened to read it would be well beyond what this owns.
    """
    if not name or not _NAME.match(name):
        return False
    root = (workspace / "skills").resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root) or target == root or not target.is_dir():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()
