"""Where a cited document can actually be opened.

A document's `uri` is its IDENTITY, and identity is not always a browsable address:

  - a git-cloned file is `<clone-url>::<path-in-repo>` — which begins with "https://" and
    so *looks* linkable, but resolves to nothing. Left alone it produces a confidently
    broken link, which is worse than none. The real address has to be built from the
    forge's own web layout (`/-/blob/` on GitLab, `/blob/` on GitHub, `?path=` on Azure
    DevOps).
  - a local file is `file://…`, which a browser will not navigate to from an http page —
    it fails silently, so it is deliberately not offered as a link.
  - a distilled conversation is `conversation://…`, which is internal and opens nothing.

So this maps a uri to the URL a person can click, or None when there isn't one. Pure and
bounded: it only ever rewrites shapes it recognises, and returns None rather than guessing.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# `git_repo.py` joins the clone url and the in-repo path with "::" (see its sync()).
_REPO_FILE = re.compile(r"^(?P<repo>[^\s]+?)::(?P<path>[^\s].*)$")
# scp-style remote: git@host:group/repo.git
_SCP = re.compile(r"^(?:ssh://)?(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$")

# Without a branch (the connector's `branch` option is often blank, meaning "whatever the
# remote's default is") HEAD is the honest reference: GitLab, GitHub and Bitbucket all
# resolve it to the default branch, so the link lands on the file as it is now.
DEFAULT_REF = "HEAD"

_LINKABLE_SCHEME = re.compile(r"^https?://", re.I)


def _normalize_remote(repo: str) -> tuple[str, str] | None:
    """(host, project path) from a clone url in either https or scp form."""
    repo = repo.strip().rstrip("/")
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    m = _LINKABLE_SCHEME.match(repo)
    if m:
        rest = repo[m.end():]
        host, _, path = rest.partition("/")
        # Strip any embedded credentials — never put a token in a link we render.
        host = host.rpartition("@")[2]
        return (host, path) if path else None
    scp = _SCP.match(repo)
    if scp:
        return scp.group("host"), scp.group("path").lstrip("/")
    return None


def repo_file_url(repo: str, path: str, ref: str = DEFAULT_REF) -> str | None:
    """A browsable URL for one file in a cloned repository, or None for a host whose web
    layout we don't know — a wrong path would 404 with full confidence."""
    parts = _normalize_remote(repo)
    if not parts:
        return None
    host, project = parts
    if not project:
        return None
    # Percent-encode the path but keep its separators readable.
    safe = quote(path.strip().lstrip("/"), safe="/._-~")
    h = host.lower()
    if "gitlab" in h:
        return f"https://{host}/{project}/-/blob/{ref}/{safe}"
    if "github" in h:
        return f"https://{host}/{project}/blob/{ref}/{safe}"
    if "bitbucket" in h:
        return f"https://{host}/{project}/src/{ref}/{safe}"
    if "dev.azure.com" in h or "visualstudio.com" in h or "/_git/" in project:
        # Azure DevOps / TFS address a file by query string, not by path.
        return f"https://{host}/{project}?path=/{safe}"
    return None


def citable_link(uri: str) -> str | None:
    """The URL a citation of this document should open, or None when it has none.

    Order matters: the repo-file shape is checked FIRST, because it begins with the clone
    url and would otherwise pass the plain http test and be offered as a broken link.
    """
    uri = (uri or "").strip()
    if not uri:
        return None
    m = _REPO_FILE.match(uri)
    if m:
        return repo_file_url(m.group("repo"), m.group("path"))
    if _LINKABLE_SCHEME.match(uri):
        return uri
    # file:// (blocked from an http page), conversation:// (internal), anything else.
    return None
