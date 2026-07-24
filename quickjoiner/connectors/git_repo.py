"""Git repository connector: clone/pull any git remote (GitHub/GitLab/Azure DevOps/etc.)
and index its files plus recent commit history. Works with a plain git URL + token."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse, urlunparse

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.deps import SKIP_DIRS, dependency_document
from quickjoiner.connectors.files import read_documents_parallel, read_file_document
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import resolve_secret

HISTORY_COMMITS = 200


def suggest_api_connector(git_url: str) -> dict | None:
    """Given a git clone URL, suggest the matching **API** connector (github/gitlab) that
    layers merge requests / issues / pipelines / wiki + the ticket↔MR graph on top of the
    cloned code — the "connect as many modes as possible" nudge. Returns
    `{"type", "options", "label"}` (ready to hand to the connect flow) or None for a plain /
    unrecognized host. Pure + host-heuristic:
      github.com / github.* (Enterprise)  → github, options.repo = "org/repo"
      gitlab.com / *gitlab*  (self-managed) → gitlab, options.project = full group/…/repo path
    A self-managed host carries its `base_url`; the token is left unset so it falls back to the
    `$GITHUB_TOKEN`/`$GITLAB_TOKEN` env var. Handles `https://…`, `http://…`, and scp-style
    `git@host:group/repo.git`."""
    url = (git_url or "").strip()
    if not url:
        return None
    if url.startswith("git@") and ":" in url:  # scp form → https for a uniform parse
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    scheme = parsed.scheme or "https"
    segments = path.split("/")
    if host == "github.com" or host.startswith("github."):
        if len(segments) < 2:
            return None
        repo = "/".join(segments[:2])  # github repos are exactly org/name
        options = {"repo": repo}
        if host != "github.com":
            options["base_url"] = f"{scheme}://{host}"  # GitHub Enterprise
        return {"type": "github", "options": options, "label": f"GitHub repo {repo}"}
    if host == "gitlab.com" or "gitlab" in host:
        options = {"project": path}  # gitlab wants the full group/subgroup/name path
        if host != "gitlab.com":
            options["base_url"] = f"{scheme}://{host}"  # self-managed instance
        return {"type": "gitlab", "options": options, "label": f"GitLab project {path}"}
    return None


@register
class GitRepoConnector(Connector):
    type_name = "git"
    modes = Mode.PULL

    def _remote(self) -> str:
        url = str(self.options.get("url", ""))
        token = resolve_secret(self.options, "token", "GIT_TOKEN")
        if token and url.startswith("https://"):
            parsed = urlparse(url)
            netloc = f"{token}@{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
            url = urlunparse(parsed._replace(netloc=netloc))
        return url

    def _clone_dir(self) -> Path:
        return self.workspace / "repos" / self.name

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )

    def test(self) -> ConnectionStatus:
        if not self.options.get("url"):
            return ConnectionStatus(False, "No 'url' configured")
        result = self._git("ls-remote", "--heads", self._remote())
        if result.returncode != 0:
            return ConnectionStatus(False, f"git ls-remote failed: {result.stderr.strip()[:300]}")
        return ConnectionStatus(True, "Remote reachable")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        clone_dir = self._clone_dir()
        branch = self.options.get("branch")
        self._stage("cloning" if not (clone_dir / ".git").exists() else "pulling")
        if (clone_dir / ".git").exists():
            result = self._git("pull", "--ff-only", cwd=clone_dir)
        else:
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--depth", "200"]
            if branch:
                args += ["--branch", str(branch)]
            args += [self._remote(), str(clone_dir)]
            result = self._git(*args)
        if result.returncode != 0:
            raise RuntimeError(f"git sync failed: {result.stderr.strip()[:500]}")

        repo_url = str(self.options.get("url", ""))
        self._stage("scanning files")
        files = [
            p for p in sorted(clone_dir.rglob("*"))
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
        ]
        total = len(files)

        def _read(path: Path) -> Document | None:
            doc = read_file_document(path, clone_dir)
            if doc is None:
                return None
            rel = path.relative_to(clone_dir).as_posix()
            doc.uri = f"{repo_url}::{rel}"
            doc.title = f"{self.name}/{rel}"
            return doc

        # Read files concurrently (bounded, in-order) — disk I/O releases the GIL, so a large
        # repo's file phase parallelizes and overlaps with the downstream embed. Stop/pause +
        # progress still land per file via the stage callback.
        yield from read_documents_parallel(
            files, _read, stage=lambda done, tot: self._stage("reading files", done, tot)
        )

        self._stage("dependency map", total, total)
        dep_doc = dependency_document(clone_dir, self.name, repo_url)
        if dep_doc:
            yield dep_doc

        history = self._git(
            "log", f"-n{HISTORY_COMMITS}", "--date=iso",
            "--pretty=format:%h %ad %an: %s", cwd=clone_dir,
        )
        if history.returncode == 0 and history.stdout.strip():
            yield Document(
                uri=f"{repo_url}::git-log",
                title=f"{self.name}: recent commit history",
                text=f"Recent commits in {self.name}:\n{history.stdout}",
                kind="doc",
            )
