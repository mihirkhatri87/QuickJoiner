"""GitHub connector: PRs, issues, Actions runs, CODEOWNERS via the REST API,
plus live code-search tools. (Clone/index the code itself with the 'git' connector.)"""

from __future__ import annotations

import base64
from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

MAX_PAGES = 4
PER_PAGE = 50


def pr_document(repo: str, pr: dict[str, Any]) -> Document:
    labels = ", ".join(lbl["name"] for lbl in pr.get("labels", []))
    text = (
        f"Pull request #{pr['number']}: {pr['title']}\n"
        f"State: {pr['state']}{' (merged)' if pr.get('merged_at') else ''} | "
        f"Author: {pr.get('user', {}).get('login', '?')} | "
        f"Branch: {pr.get('head', {}).get('ref', '?')} -> {pr.get('base', {}).get('ref', '?')}\n"
        f"Labels: {labels or 'none'} | Updated: {pr.get('updated_at', '')}\n\n"
        f"{pr.get('body') or '(no description)'}"
    )
    return Document(
        uri=pr["html_url"],
        title=f"{repo} PR #{pr['number']}: {pr['title']}",
        text=text,
        kind="ticket",
        updated_at=pr.get("updated_at"),
    )


def issue_document(repo: str, issue: dict[str, Any]) -> Document:
    labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))
    text = (
        f"Issue #{issue['number']}: {issue['title']}\n"
        f"State: {issue['state']} | Author: {issue.get('user', {}).get('login', '?')} | "
        f"Labels: {labels or 'none'} | Updated: {issue.get('updated_at', '')}\n\n"
        f"{issue.get('body') or '(no description)'}"
    )
    return Document(
        uri=issue["html_url"],
        title=f"{repo} issue #{issue['number']}: {issue['title']}",
        text=text,
        kind="ticket",
        updated_at=issue.get("updated_at"),
    )


def runs_document(repo: str, runs: list[dict[str, Any]]) -> Document:
    lines = [
        f"- {r.get('name', '?')} on {r.get('head_branch', '?')}: "
        f"{r.get('status', '?')}/{r.get('conclusion') or 'pending'} "
        f"(run #{r.get('run_number', '?')}, {r.get('updated_at', '')})"
        for r in runs
    ]
    return Document(
        uri=f"https://github.com/{repo}/actions",
        title=f"{repo}: recent GitHub Actions runs",
        text=f"Recent CI runs for {repo}:\n" + "\n".join(lines),
        kind="pipeline",
    )


@register
class GitHubConnector(Connector):
    type_name = "github"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        token = resolve_secret(self.options, "token", "GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _base(self) -> str:
        return str(self.options.get("base_url", "https://api.github.com")).rstrip("/")

    def _repo(self) -> str:
        return str(self.options.get("repo", ""))

    def test(self) -> ConnectionStatus:
        repo = self._repo()
        if not repo:
            return ConnectionStatus(False, "No 'repo' configured (expected 'owner/name')")
        try:
            data = get_json(f"{self._base()}/repos/{repo}", headers=self._headers())
            return ConnectionStatus(True, f"Reachable: {data.get('full_name')}")
        except Exception as exc:
            return ConnectionStatus(False, f"GitHub API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        repo, base, headers = self._repo(), self._base(), self._headers()
        since = state.get("since", "")

        # No cheap up-front count for the GitHub list APIs, so these report a phase label
        # (the UI shows a live shimmer) rather than a misleading fraction of the page cap.
        for page in range(1, MAX_PAGES + 1):
            self._stage("pull requests")  # also a stop/pause checkpoint, per page
            prs = get_json(
                f"{base}/repos/{repo}/pulls",
                headers=headers,
                params={"state": "all", "sort": "updated", "direction": "desc",
                        "per_page": PER_PAGE, "page": page},
            )
            for pr in prs:
                if since and pr.get("updated_at", "") < since:
                    prs = []
                    break
                yield pr_document(repo, pr)
            if len(prs) < PER_PAGE:
                break

        for page in range(1, MAX_PAGES + 1):
            self._stage("issues")
            issues = get_json(
                f"{base}/repos/{repo}/issues",
                headers=headers,
                params={"state": "all", "sort": "updated", "direction": "desc",
                        "per_page": PER_PAGE, "page": page},
            )
            real_issues = [i for i in issues if "pull_request" not in i]
            for issue in real_issues:
                if since and issue.get("updated_at", "") < since:
                    issues = []
                    break
                yield issue_document(repo, issue)
            if len(issues) < PER_PAGE:
                break

        self._stage("CI runs & ownership")
        runs = get_json(
            f"{base}/repos/{repo}/actions/runs",
            headers=headers,
            params={"per_page": PER_PAGE},
        ).get("workflow_runs", [])
        if runs:
            yield runs_document(repo, runs)

        for codeowners_path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            try:
                data = get_json(f"{base}/repos/{repo}/contents/{codeowners_path}", headers=headers)
            except Exception:
                continue
            content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
            yield Document(
                uri=data.get("html_url", f"https://github.com/{repo}/{codeowners_path}"),
                title=f"{repo} CODEOWNERS",
                text=f"Code ownership map for {repo}:\n{content}",
                kind="doc",
            )
            break

    def tools(self) -> list[AgentTool]:
        repo, base, headers = self._repo(), self._base(), self._headers()

        def github_search_code(query: str) -> str:
            data = get_json(
                f"{base}/search/code",
                headers=headers,
                params={"q": f"{query} repo:{repo}", "per_page": 10},
            )
            items = data.get("items", [])
            if not items:
                return "No code matches."
            return "\n".join(f"- {i['path']} ({i['html_url']})" for i in items)

        def github_get_file(path: str) -> str:
            data = get_json(f"{base}/repos/{repo}/contents/{path}", headers=headers)
            if isinstance(data, list):
                return "Directory listing:\n" + "\n".join(f"- {d['path']}" for d in data)
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")[:20000]

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"github_search_code_{self.name}",
                    description=f"Live search for code in the GitHub repo {repo}. Returns matching file paths.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                ),
                fn=github_search_code,
            ),
            AgentTool(
                spec=ToolSpec(
                    name=f"github_get_file_{self.name}",
                    description=f"Live-read a file (or list a directory) from the GitHub repo {repo} at its current HEAD.",
                    input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                ),
                fn=github_get_file,
            ),
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        repo = self._repo() or payload.get("repository", {}).get("full_name", "unknown")
        if "pull_request" in payload:
            yield pr_document(repo, payload["pull_request"])
        elif "issue" in payload and "pull_request" not in payload.get("issue", {}):
            yield issue_document(repo, payload["issue"])
        elif "workflow_run" in payload:
            yield runs_document(repo, [payload["workflow_run"]])
