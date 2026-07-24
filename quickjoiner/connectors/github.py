"""GitHub connector: PRs, issues, Actions runs, CODEOWNERS via the REST API,
plus live code-search tools. (Clone/index the code itself with the 'git' connector.)"""

from __future__ import annotations

import base64
from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.live_tools import bounded, clamp, make_resolver, read_tool
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, prefetch_pages, resolve_secret
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
        # Each list prefetches the next page while the current one is parsed+embedded.
        def _fetch(endpoint):
            def fetch(page):
                page = page or 1
                items = get_json(
                    f"{base}/repos/{repo}/{endpoint}", headers=headers,
                    params={"state": "all", "sort": "updated", "direction": "desc",
                            "per_page": PER_PAGE, "page": page},
                )
                more = len(items) == PER_PAGE and page < MAX_PAGES
                return items, (page + 1 if more else None)
            return fetch

        self._stage("pull requests")
        stop = False
        for prs in prefetch_pages(_fetch("pulls"), 1, self._checkpoint):
            self._stage("pull requests")
            for pr in prs:
                if since and pr.get("updated_at", "") < since:
                    stop = True
                    break
                yield pr_document(repo, pr)
            if stop:
                break

        self._stage("issues")
        stop = False
        for issues in prefetch_pages(_fetch("issues"), 1, self._checkpoint):
            self._stage("issues")
            for issue in (i for i in issues if "pull_request" not in i):
                if since and issue.get("updated_at", "") < since:
                    stop = True
                    break
                yield issue_document(repo, issue)
            if stop:
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
        return type(self).type_tools([self])

    @classmethod
    def type_tools(cls, connectors: list["GitHubConnector"]) -> list[AgentTool]:
        """Consolidated READ-ONLY live tools for all configured GitHub repos (plan 09) — the
        parallel family to GitLab's. Each tool takes an optional `repo` selector (a substring of
        the configured owner/name); omit it with a single GitHub source. GET-only; no mutations."""
        resolve = make_resolver(connectors, lambda c: str(c.options.get("repo", "")))
        R = ("string", False, "GitHub repo (substring of owner/name; omit if only one is connected)")

        def _pick(repo):
            c, err = resolve(repo)
            if err:
                return None, None, None, None, err
            return c, c._base(), c._repo(), c._headers(), None

        def list_pull_requests(repo=None, state="open", max_results=30):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            st = (state or "open").lower().strip()
            st = st if st in {"open", "closed", "all"} else "open"
            items = get_json(f"{base}/repos/{r}/pulls", headers=h,
                             params={"state": st, "sort": "updated", "direction": "desc",
                                     "per_page": clamp(max_results, 30, 100)})
            if not items:
                return f"No {st} pull requests in {r}."
            return bounded(f"{len(items)} {st} pull requests in {r} (newest first):\n" + "\n".join(
                f"- #{p.get('number')} [{p.get('state', '?')}{'/merged' if p.get('merged_at') else ''}] "
                f"{p.get('title', '')} (branch {(p.get('head') or {}).get('ref', '?')}) — "
                f"{(p.get('user') or {}).get('login', '?')}  {p.get('html_url', '')}" for p in items))

        def get_pull_request(number, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            p = get_json(f"{base}/repos/{r}/pulls/{number}", headers=h)
            reviewers = ", ".join(u.get("login", "?") for u in (p.get("requested_reviewers") or [])) or "none"
            return bounded(
                f"PR #{p.get('number')}: {p.get('title', '')}\n"
                f"State: {p.get('state', '?')}{' (merged)' if p.get('merged_at') else ''} | "
                f"Author: {(p.get('user') or {}).get('login', '?')} | Requested reviewers: {reviewers}\n"
                f"Branch: {(p.get('head') or {}).get('ref', '?')} -> {(p.get('base') or {}).get('ref', '?')} | "
                f"Mergeable: {p.get('mergeable_state', p.get('mergeable', '?'))}\n"
                f"Commits: {p.get('commits', '?')} | Changed files: {p.get('changed_files', '?')} "
                f"(+{p.get('additions', '?')}/-{p.get('deletions', '?')})\n"
                f"URL: {p.get('html_url', '')}\n\n{p.get('body') or '(no description)'}")

        def pull_request_reviews(number, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            reviews = get_json(f"{base}/repos/{r}/pulls/{number}/reviews", headers=h, params={"per_page": 100})
            if not reviews:
                return f"No reviews on PR #{number}."
            return bounded(f"Reviews on PR #{number}:\n" + "\n".join(
                f"- {(rv.get('user') or {}).get('login', '?')}: {rv.get('state', '?')} "
                f"{(rv.get('body') or '').strip()[:200]}" for rv in reviews))

        def list_issues(repo=None, state="open", max_results=30):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            st = (state or "open").lower().strip()
            st = st if st in {"open", "closed", "all"} else "open"
            items = get_json(f"{base}/repos/{r}/issues", headers=h,
                             params={"state": st, "sort": "updated", "direction": "desc",
                                     "per_page": clamp(max_results, 30, 100)})
            issues = [i for i in items if not i.get("pull_request")]  # the API mixes PRs in
            if not issues:
                return f"No {st} issues in {r}."
            return bounded(f"{len(issues)} {st} issues in {r} (newest first):\n" + "\n".join(
                f"- #{i.get('number')} [{i.get('state', '?')}] {i.get('title', '')} — "
                f"{(i.get('user') or {}).get('login', '?')}  {i.get('html_url', '')}" for i in issues))

        def workflow_runs(branch=None, repo=None, max_results=5):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            params = {"per_page": clamp(max_results, 5, 20)}
            if branch:
                params["branch"] = branch
            data = get_json(f"{base}/repos/{r}/actions/runs", headers=h, params=params)
            runs = data.get("workflow_runs") or []
            if not runs:
                return f"No workflow runs{f' for {branch}' if branch else ''} in {r}."
            return bounded(f"Workflow runs in {r} (newest first):\n" + "\n".join(
                f"- run {run.get('id')} [{run.get('status', '?')}/{run.get('conclusion') or 'pending'}] "
                f"{run.get('name', '?')} on {run.get('head_branch', '?')}  {run.get('html_url', '')}"
                for run in runs))

        def run_jobs(run_id, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            data = get_json(f"{base}/repos/{r}/actions/runs/{run_id}/jobs", headers=h, params={"per_page": 100})
            jobs = data.get("jobs") or []
            if not jobs:
                return f"No jobs for run {run_id}."
            return bounded(f"Run {run_id} jobs:\n" + "\n".join(
                f"- [{j.get('status', '?')}/{j.get('conclusion') or 'pending'}] {j.get('name', '?')} "
                f"(job {j.get('id')})  {j.get('html_url', '')}" for j in jobs))

        def job_log(job_id, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            import httpx
            resp = httpx.get(f"{base}/repos/{r}/actions/jobs/{job_id}/logs", headers=h,
                             timeout=60.0, follow_redirects=True)
            if resp.status_code == 404:
                return f"No log for job {job_id}."
            resp.raise_for_status()
            trace = resp.text
            return f"Log tail for job {job_id}:\n{trace[-6000:] if len(trace) > 6000 else trace}"

        def list_commits(repo=None, ref=None, since=None, path=None, max_results=20):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            params = {"per_page": clamp(max_results, 20, 100)}
            if ref:
                params["sha"] = ref
            if since:
                params["since"] = since
            if path:
                params["path"] = path
            items = get_json(f"{base}/repos/{r}/commits", headers=h, params=params)
            if not items:
                return "No commits found for that filter."
            return bounded(f"{len(items)} commits (newest first):\n" + "\n".join(
                f"- {ci.get('sha', '')[:8]} {((ci.get('commit') or {}).get('message') or '').splitlines()[0][:70]} "
                f"— {((ci.get('commit') or {}).get('author') or {}).get('name', '?')}" for ci in items))

        def compare(base_ref, head_ref, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            data = get_json(f"{base}/repos/{r}/compare/{base_ref}...{head_ref}", headers=h)
            files = data.get("files") or []
            flist = "\n".join(f"  {f.get('filename', '?')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                              for f in files[:40])
            return bounded(f"{base_ref}...{head_ref}: {data.get('ahead_by', '?')} ahead, "
                           f"{data.get('behind_by', '?')} behind, {len(files)} files changed\n{flist}")

        def list_branches(repo=None, max_results=40):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            items = get_json(f"{base}/repos/{r}/branches", headers=h,
                             params={"per_page": clamp(max_results, 40, 100)})
            if not items:
                return "No branches."
            return bounded(f"{len(items)} branches:\n" + "\n".join(
                f"- {b.get('name')}{' [protected]' if b.get('protected') else ''} "
                f"({(b.get('commit') or {}).get('sha', '')[:8]})" for b in items))

        def repo_info(repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            p = get_json(f"{base}/repos/{r}", headers=h)
            langs = {}
            try:
                langs = get_json(f"{base}/repos/{r}/languages", headers=h)
            except Exception:
                pass
            lang_str = ", ".join(k for k, _ in sorted(langs.items(), key=lambda kv: -kv[1])[:6]) or "n/a"
            return bounded(
                f"{p.get('full_name', r)}\n"
                f"Default branch: {p.get('default_branch', '?')} | Visibility: "
                f"{'private' if p.get('private') else 'public'}\n"
                f"Open issues: {p.get('open_issues_count', '?')} | Stars: {p.get('stargazers_count', '?')} | "
                f"Updated: {p.get('updated_at', '?')}\n"
                f"Topics: {', '.join(p.get('topics') or []) or 'none'}\nLanguages: {lang_str}\n"
                f"URL: {p.get('html_url', '')}\n\n{p.get('description') or '(no description)'}")

        def search_code(query, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            data = get_json(f"{base}/search/code", headers=h,
                            params={"q": f"{query} repo:{r}", "per_page": 10})
            items = data.get("items", [])
            if not items:
                return "No code matches."
            return bounded("\n".join(f"- {i['path']} ({i['html_url']})" for i in items))

        def get_file(path, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            data = get_json(f"{base}/repos/{r}/contents/{path}", headers=h)
            if isinstance(data, list):
                return "Directory listing:\n" + "\n".join(f"- {d['path']}" for d in data)
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")[:20000]

        # ---- P2 ----
        def list_releases(repo=None, max_results=20):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            items = get_json(f"{base}/repos/{r}/releases", headers=h,
                             params={"per_page": clamp(max_results, 20, 100)})
            if not items:
                return f"No releases in {r}."
            return bounded(f"{len(items)} releases (newest first):\n" + "\n".join(
                f"- {rel.get('name') or rel.get('tag_name')} (tag {rel.get('tag_name')}, "
                f"{str(rel.get('published_at', ''))[:10]}){' [prerelease]' if rel.get('prerelease') else ''}"
                for rel in items))

        def list_milestones(repo=None, state="open", max_results=30):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            st = state if state in {"open", "closed", "all"} else "open"
            items = get_json(f"{base}/repos/{r}/milestones", headers=h,
                             params={"state": st, "per_page": clamp(max_results, 30, 100)})
            if not items:
                return f"No {st} milestones in {r}."
            return bounded(f"{len(items)} {st} milestones:\n" + "\n".join(
                f"- {m.get('title')} [{m.get('state', '?')}] {m.get('open_issues', 0)} open / "
                f"{m.get('closed_issues', 0)} closed" + (f", due {str(m.get('due_on', ''))[:10]}" if m.get("due_on") else "")
                for m in items))

        def list_contributors(repo=None, max_results=30):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            items = get_json(f"{base}/repos/{r}/contributors", headers=h,
                             params={"per_page": clamp(max_results, 30, 100)})
            if not items:
                return "No contributors."
            return bounded("Top contributors:\n" + "\n".join(
                f"- {ct.get('login', '?')} — {ct.get('contributions', '?')} contributions" for ct in items))

        def list_environments(repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            data = get_json(f"{base}/repos/{r}/environments", headers=h)
            envs = data.get("environments") or []
            if not envs:
                return f"No environments in {r}."
            return bounded(f"{len(envs)} environments:\n" + "\n".join(f"- {e.get('name')}" for e in envs))

        def list_tree(path="", ref=None, repo=None):
            c, base, r, h, err = _pick(repo)
            if err:
                return err
            params = {"ref": ref} if ref else None
            data = get_json(f"{base}/repos/{r}/contents/{path}", headers=h, params=params)
            if not isinstance(data, list):
                return f"{path} is a file, not a directory."
            return bounded(f"{len(data)} entries at {path or '/'}:\n" + "\n".join(
                f"- {'[dir] ' if d.get('type') == 'dir' else ''}{d.get('name')}" for d in data))

        return [
            read_tool("github_list_pull_requests",
                      "List CURRENT pull requests by state (open/closed/all). Use for 'list all open "
                      "PRs' — memory can't enumerate them.",
                      list_pull_requests, {"repo": R,
                      "state": ("string", False, "open (default)|closed|all"),
                      "max_results": ("integer", False, "")}),
            read_tool("github_get_pull_request",
                      "Full PR details by number: state, author, reviewers, branches, mergeable, "
                      "commits/changed-files, description.",
                      get_pull_request, {"number": ("integer", True, "the PR number"), "repo": R}),
            read_tool("github_pull_request_reviews",
                      "Reviews on a PR (who approved / requested changes + comments), by number.",
                      pull_request_reviews, {"number": ("integer", True, "the PR number"), "repo": R}),
            read_tool("github_list_issues",
                      "List CURRENT issues by state (open/closed/all) — excludes PRs.",
                      list_issues, {"repo": R, "state": ("string", False, "open (default)|closed|all"),
                      "max_results": ("integer", False, "")}),
            read_tool("github_workflow_runs",
                      "Latest GitHub Actions workflow run(s) — status/conclusion + link (LIVE). 'did "
                      "CI pass on branch X?'. Optional branch filter.",
                      workflow_runs, {"branch": ("string", False, "branch name"), "repo": R,
                      "max_results": ("integer", False, "")}),
            read_tool("github_run_jobs",
                      "Jobs of a workflow run (by run id) with status/conclusion — which job failed.",
                      run_jobs, {"run_id": ("integer", True, "the run id"), "repo": R}),
            read_tool("github_job_log",
                      "Tail of an Actions job's log (by job id, from github_run_jobs) — 'why did it fail'.",
                      job_log, {"job_id": ("integer", True, "the job id"), "repo": R}),
            read_tool("github_list_commits",
                      "Recent commits, optionally by ref/branch, since date, or path.",
                      list_commits, {"repo": R, "ref": ("string", False, "branch/ref"),
                      "since": ("string", False, "ISO date"), "path": ("string", False, "file/dir"),
                      "max_results": ("integer", False, "")}),
            read_tool("github_compare",
                      "Compare two refs: ahead/behind counts and changed files.",
                      compare, {"base_ref": ("string", True, "base ref"),
                      "head_ref": ("string", True, "head ref"), "repo": R}),
            read_tool("github_list_branches",
                      "List branches with their head SHA.",
                      list_branches, {"repo": R, "max_results": ("integer", False, "")}),
            read_tool("github_repo_info",
                      "Repo overview: default branch, visibility, stars, topics, languages, open "
                      "issues, description.",
                      repo_info, {"repo": R}),
            read_tool("github_search_code",
                      "Live search for code in the repo. Returns matching file paths.",
                      search_code, {"query": ("string", True, "search text"), "repo": R}),
            read_tool("github_get_file",
                      "Live-read a file (or list a directory) at HEAD.",
                      get_file, {"path": ("string", True, "file path"), "repo": R}),
            read_tool("github_list_releases", "List the repo's releases (tag, name, date).",
                      list_releases, {"repo": R, "max_results": ("integer", False, "")}),
            read_tool("github_list_milestones", "List milestones by state (open/closed/all) with progress.",
                      list_milestones, {"repo": R, "state": ("string", False, "open (default)|closed|all"),
                      "max_results": ("integer", False, "")}),
            read_tool("github_list_contributors", "Top contributors to the repo.",
                      list_contributors, {"repo": R, "max_results": ("integer", False, "")}),
            read_tool("github_list_environments", "Deployment environments configured for the repo.",
                      list_environments, {"repo": R}),
            read_tool("github_list_tree", "List files/dirs at a path (default repo root).",
                      list_tree, {"path": ("string", False, "dir path"), "ref": ("string", False, "branch/SHA"),
                      "repo": R}),
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        repo = self._repo() or payload.get("repository", {}).get("full_name", "unknown")
        if "pull_request" in payload:
            yield pr_document(repo, payload["pull_request"])
        elif "issue" in payload and "pull_request" not in payload.get("issue", {}):
            yield issue_document(repo, payload["issue"])
        elif "workflow_run" in payload:
            yield runs_document(repo, [payload["workflow_run"]])
