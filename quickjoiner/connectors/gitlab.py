"""GitLab connector: MRs, issues, CI pipelines, and wiki pages via the REST API v4,
plus live code-search tools. (Clone/index the code itself with the 'git' connector.)

Webhooks: GitLab sends a plain shared token in X-Gitlab-Token; set the same value as
the source's webhook_secret and point the project webhook at POST /hooks/<source>.
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import quote

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

MAX_PAGES = 4
PER_PAGE = 50


def _labels(raw: Any) -> str:
    """GitLab labels are strings in the API but {'title': ...} objects in webhooks."""
    names = [l if isinstance(l, str) else (l.get("title") or l.get("name", "")) for l in raw or []]
    return ", ".join(n for n in names if n) or "none"


def _url(obj: dict[str, Any]) -> str:
    return obj.get("web_url") or obj.get("url") or ""


def mr_document(project: str, mr: dict[str, Any]) -> Document:
    author = (mr.get("author") or {}).get("username", "?")
    state = mr.get("state", "?")
    text = (
        f"Merge request !{mr.get('iid', '?')}: {mr.get('title', '')}\n"
        f"State: {state} | Author: {author} | "
        f"Branch: {mr.get('source_branch', '?')} -> {mr.get('target_branch', '?')}\n"
        f"Labels: {_labels(mr.get('labels'))} | Updated: {mr.get('updated_at', '')}\n\n"
        f"{mr.get('description') or '(no description)'}"
    )
    return Document(
        uri=_url(mr) or f"gitlab://{project}/merge_requests/{mr.get('iid')}",
        title=f"{project} MR !{mr.get('iid')}: {mr.get('title', '')}",
        text=text,
        kind="ticket",
        updated_at=mr.get("updated_at"),
    )


def issue_document(project: str, issue: dict[str, Any]) -> Document:
    author = (issue.get("author") or {}).get("username", "?")
    assignees = ", ".join(a.get("username", "?") for a in issue.get("assignees") or []) or "unassigned"
    text = (
        f"Issue #{issue.get('iid', '?')}: {issue.get('title', '')}\n"
        f"State: {issue.get('state', '?')} | Author: {author} | Assignees: {assignees}\n"
        f"Labels: {_labels(issue.get('labels'))} | Updated: {issue.get('updated_at', '')}\n\n"
        f"{issue.get('description') or '(no description)'}"
    )
    return Document(
        uri=_url(issue) or f"gitlab://{project}/issues/{issue.get('iid')}",
        title=f"{project} issue #{issue.get('iid')}: {issue.get('title', '')}",
        text=text,
        kind="ticket",
        updated_at=issue.get("updated_at"),
    )


def pipelines_document(project: str, pipelines: list[dict[str, Any]], web_base: str = "https://gitlab.com") -> Document:
    lines = [
        f"- #{p.get('id', '?')} on {p.get('ref', '?')}: {p.get('status', '?')} ({p.get('updated_at', '')})"
        for p in pipelines
    ]
    return Document(
        uri=f"{web_base}/{project}/-/pipelines",
        title=f"{project}: recent GitLab CI pipelines",
        text=f"Recent CI pipelines for {project}:\n" + "\n".join(lines),
        kind="pipeline",
    )


def wiki_document(project: str, page: dict[str, Any], web_base: str = "https://gitlab.com") -> Document:
    slug = page.get("slug", "")
    return Document(
        uri=f"{web_base}/{project}/-/wikis/{slug}",
        title=f"{project} wiki: {page.get('title', slug)}",
        text=page.get("content") or "(empty page)",
        kind="doc",
    )


def push_document(project: str, payload: dict[str, Any]) -> Document:
    ref = payload.get("ref", "").removeprefix("refs/heads/")
    commits = payload.get("commits") or []
    lines = [
        f"- {c.get('id', '')[:8]} {(c.get('message') or '').splitlines()[0] if c.get('message') else ''} "
        f"({(c.get('author') or {}).get('name', '?')})"
        for c in commits
    ]
    web_url = (payload.get("project") or {}).get("web_url", f"https://gitlab.com/{project}")
    return Document(
        uri=f"{web_url}/-/commits/{ref}",
        title=f"{project}: push to {ref}",
        text=(
            f"Push to {ref} in {project} by {payload.get('user_name', '?')} "
            f"({len(commits)} commits):\n" + "\n".join(lines)
        ),
        kind="doc",
    )


@register
class GitLabConnector(Connector):
    type_name = "gitlab"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _web_base(self) -> str:
        return str(self.options.get("base_url", "https://gitlab.com")).rstrip("/")

    def _base(self) -> str:
        return f"{self._web_base()}/api/v4"

    def _project(self) -> str:
        return str(self.options.get("project", ""))

    def _project_api(self) -> str:
        return f"{self._base()}/projects/{quote(self._project(), safe='')}"

    def _headers(self) -> dict[str, str]:
        token = resolve_secret(self.options, "token", "GITLAB_TOKEN")
        return {"PRIVATE-TOKEN": token} if token else {}

    def test(self) -> ConnectionStatus:
        project = self._project()
        if not project:
            return ConnectionStatus(False, "No 'project' configured (expected 'group/name')")
        try:
            data = get_json(self._project_api(), headers=self._headers())
            return ConnectionStatus(True, f"Reachable: {data.get('path_with_namespace')}")
        except Exception as exc:
            return ConnectionStatus(False, f"GitLab API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        project, headers = self._project(), self._headers()
        since = state.get("since", "")

        for kind, endpoint, to_doc in (
            ("mrs", "merge_requests", mr_document),
            ("issues", "issues", issue_document),
        ):
            params: dict[str, Any] = {
                "state": "all", "order_by": "updated_at", "sort": "desc", "per_page": PER_PAGE,
            }
            if since:
                params["updated_after"] = since
            for page in range(1, MAX_PAGES + 1):
                items = get_json(
                    f"{self._project_api()}/{endpoint}", headers=headers,
                    params={**params, "page": page},
                )
                for item in items:
                    yield to_doc(project, item)
                if len(items) < PER_PAGE:
                    break

        pipelines = get_json(
            f"{self._project_api()}/pipelines", headers=headers, params={"per_page": PER_PAGE}
        )
        if pipelines:
            yield pipelines_document(project, pipelines, self._web_base())

        try:
            pages = get_json(
                f"{self._project_api()}/wikis", headers=headers, params={"with_content": 1}
            )
        except Exception:
            pages = []  # wiki disabled or no access
        for page in pages:
            yield wiki_document(project, page, self._web_base())

    def tools(self) -> list[AgentTool]:
        project, headers = self._project(), self._headers()

        def gitlab_search_code(query: str) -> str:
            items = get_json(
                f"{self._project_api()}/search",
                headers=headers,
                params={"scope": "blobs", "search": query, "per_page": 10},
            )
            if not items:
                return "No code matches."
            return "\n".join(
                f"- {i.get('path', i.get('filename', '?'))} (ref {i.get('ref', '?')}): "
                f"{(i.get('data') or '').strip().splitlines()[0][:120] if i.get('data') else ''}"
                for i in items
            )

        def gitlab_get_file(path: str, ref: str = "HEAD") -> str:
            import httpx

            resp = httpx.get(
                f"{self._project_api()}/repository/files/{quote(path, safe='')}/raw",
                headers=headers,
                params={"ref": ref},
                timeout=60.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.text[:20000]

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"gitlab_search_code_{self.name}",
                    description=f"Live search for code in the GitLab project {project}. Returns matching file paths.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                ),
                fn=gitlab_search_code,
            ),
            AgentTool(
                spec=ToolSpec(
                    name=f"gitlab_get_file_{self.name}",
                    description=f"Live-read a file from the GitLab project {project} (default ref HEAD).",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "ref": {"type": "string"}},
                        "required": ["path"],
                    },
                ),
                fn=gitlab_get_file,
            ),
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        project = (
            (payload.get("project") or {}).get("path_with_namespace")
            or self._project()
            or "unknown"
        )
        kind = payload.get("object_kind", "")
        attrs = payload.get("object_attributes") or {}
        if kind == "merge_request":
            if "author" not in attrs and payload.get("user"):
                attrs = {**attrs, "author": {"username": payload["user"].get("username", "?")}}
            yield mr_document(project, {**attrs, "labels": payload.get("labels", attrs.get("labels"))})
        elif kind == "issue":
            if "author" not in attrs and payload.get("user"):
                attrs = {**attrs, "author": {"username": payload["user"].get("username", "?")}}
            yield issue_document(project, {**attrs, "labels": payload.get("labels", attrs.get("labels"))})
        elif kind == "pipeline":
            yield pipelines_document(project, [attrs], self._web_base())
        elif kind == "push":
            yield push_document(project, payload)
        elif kind == "wiki_page":
            yield wiki_document(project, attrs, self._web_base())
