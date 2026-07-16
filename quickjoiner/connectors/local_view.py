"""Resolve an ingested document's evidence URI back to a local file on disk, for
connector types that provably keep (or point directly at) one — so the graph/
citation UI can show the file's *current* content in-app instead of always
linking out to a remote host, even when the content backing the citation is
already sitting in the workspace.

Scoped to exactly the connectors that have a real local file:
- `git`: clones into workspace/repos/<name> (git_repo.py); Document.uri is
  "<remote_url>::<relative_path>" (its own convention — see git_repo.py:sync).
- `files`: Document.uri is a file:// URI when the source is a local path (a URL
  target has no local file and correctly returns None).

Anything else (github/gitlab/azure_devops's API-based connectors, confluence,
jira, ...) has no local checkout and returns None — the caller falls back to
the remote link.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

_REPO_CLONE_TYPES = {"git"}


def local_file_path(workspace: Path, source_id: str, uri: str) -> Path | None:
    type_, _, name = source_id.partition(":")
    if type_ in _REPO_CLONE_TYPES:
        _, sep, rel = uri.partition("::")
        if not sep or not rel:
            return None
        path = workspace / "repos" / name / rel
    elif type_ == "files" and uri.startswith("file://"):
        path = Path(url2pathname(urlparse(uri).path))
    else:
        return None
    try:
        return path if path.is_file() else None
    except OSError:
        return None
