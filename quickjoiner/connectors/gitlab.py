"""GitLab connector: MRs, issues, CI pipelines, and wiki pages via the REST API v4,
plus live code-search tools. (Clone/index the code itself with the 'git' connector.)

Webhooks: GitLab sends a plain shared token in X-Gitlab-Token; set the same value as
the source's webhook_secret and point the project webhook at POST /hooks/<source>.
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import quote

import re

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.live_tools import bounded, clamp, make_resolver, read_tool
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import as_bool, get_json, prefetch_pages, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

MAX_PAGES = 4
PER_PAGE = 50

# Per-branch "synced to TFS" capture (opt-in via the `tfs_sync_stage` option). Bounded so it
# can't blow up the sync on a project with many active branches.
TFS_SYNC_LOOKBACK_DAYS = 15          # default window to search for a successful sync
TFS_SYNC_MAX_BRANCHES = 60           # only the most-recent MR source branches
TFS_SYNC_MAX_PIPELINES_PER_BRANCH = 10   # newest-first; stop at the first with a successful sync job


def branch_sync_document(
    project: str, repo_name: str, syncs: list[dict[str, Any]], web_base: str = "https://gitlab.com"
) -> Document:
    """One doc recording, per branch, the most recent successful pipeline whose TFS-mirror
    stage succeeded (within the look-back). This is the evidence that a GitLab branch is
    actually mirrored into TFS — the real basis for the TFS↔GitLab branch parity the graph
    relies on. Carries `branch:<repo>/<name> --synced_to_tfs--> repo:<name>` edges (detail =
    pipeline id + date), evidence = this doc, so "is branch X synced to TFS / when?" is both
    retrievable and graph-answerable. `syncs`: [{branch, pipeline_id, pipeline_url, job, finished_at}]."""
    rlow = repo_name.lower()
    repo_id = f"repo:{rlow}"
    entities: dict[str, tuple[str, str, str]] = {repo_id: (repo_id, repo_name, "repo")}
    edges: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}
    lines: list[str] = []
    for s in syncs:
        branch = s["branch"]
        detail = f"pipeline #{s['pipeline_id']} (job '{s['job']}', success) at {s.get('finished_at', '?')}"
        lines.append(f"- {branch}: last synced to TFS via {detail} — {s.get('pipeline_url', '')}")
        bid = f"branch:{rlow}/{branch.lower()}"
        entities[bid] = (bid, branch, "branch")
        edges[(bid, "synced_to_tfs", repo_id)] = (bid, "synced_to_tfs", repo_id, detail)
    return Document(
        uri=f"{web_base}/{project}/-/pipelines?tfs-sync",
        title=f"{repo_name}: branches synced to TFS (last {TFS_SYNC_LOOKBACK_DAYS} days)",
        text=(
            f"Most recent successful TFS-mirror pipeline per branch for {repo_name} "
            f"(GitLab→TFS sync, {TFS_SYNC_LOOKBACK_DAYS}-day look-back):\n" + "\n".join(lines)
        ),
        kind="pipeline",
        metadata={"graph": {"entities": list(entities.values()), "aliases": [],
                            "edges": list(edges.values())}},
    )


def _labels(raw: Any) -> str:
    """GitLab labels are strings in the API but {'title': ...} objects in webhooks."""
    names = [l if isinstance(l, str) else (l.get("title") or l.get("name", "")) for l in raw or []]
    return ", ".join(n for n in names if n) or "none"


def _url(obj: dict[str, Any]) -> str:
    return obj.get("web_url") or obj.get("url") or ""


def mr_graph(repo_name: str, mr: dict[str, Any], ticket_pattern: Any = None) -> dict:
    """Knowledge-graph assertions from a merge request: the MR, its source branch, and
    the repo, keyed by NAME so they merge with the TFS side.

      `merge_request:<repo>/!<iid> --from_branch--> branch:<repo>/<source_branch>`
      `merge_request:<repo>/!<iid> --in_repo--> repo:<repo>`
      `branch:<repo>/<source_branch> --belongs_to--> repo:<repo>`

    The `branch:<repo>/<name>` entity is the JOIN with the TFS connector: a TFS work
    item's Development "Branch" link emits the same `branch:<repo>/<name>` id (name
    parity — same repo + branch name, group nesting ignored), so `ticket --on_branch-->
    branch <--from_branch-- merge_request` connects a TFS ticket to the GitLab MR that
    implements it. `repo_name` should be the repo's own name (matches the TFS Git repo
    name); returns {} without it or without an MR iid.

    **Org-specific rule (opt-in):** if `ticket_pattern` (a compiled regex, capture group 1
    = the ticket id) is given, the TFS ticket number is extracted from the branch name /
    MR title and a DIRECT `merge_request --implements--> ticket:#<id>` (+ `branch
    --for_ticket--> ticket:#<id>`) edge is emitted — the strongest join, independent of the
    branch-parity mechanism, for orgs that name branches/MRs `<ticket>-<slug>`."""
    iid = mr.get("iid")
    if iid is None or not repo_name:
        return {}
    rlow = repo_name.lower()
    repo_id = f"repo:{rlow}"
    mr_id = f"merge_request:{rlow}/!{iid}"
    title = (mr.get("title") or "").strip()
    entities: dict[str, tuple[str, str, str]] = {
        repo_id: (repo_id, repo_name, "repo"),
        mr_id: (mr_id, f"{repo_name} MR !{iid}" + (f": {title[:60]}" if title else ""), "merge_request"),
    }
    edges: dict[tuple[str, str, str], tuple[str, str, str, str]] = {
        (mr_id, "in_repo", repo_id): (mr_id, "in_repo", repo_id, f"MR !{iid}"),
    }
    src_branch = (mr.get("source_branch") or "").strip()
    bid = ""
    if src_branch:
        bid = f"branch:{rlow}/{src_branch.lower()}"
        entities[bid] = (bid, src_branch, "branch")
        edges[(mr_id, "from_branch", bid)] = (mr_id, "from_branch", bid, f"MR !{iid} source branch")
        edges[(bid, "belongs_to", repo_id)] = (bid, "belongs_to", repo_id, f"branch of {repo_name}")
    if ticket_pattern is not None:
        for source in (src_branch, title):
            m = ticket_pattern.search(source) if source else None
            if not m:
                continue
            tid = m.group(1)
            ticket_id = f"ticket:#{tid}"
            entities[ticket_id] = (ticket_id, f"#{tid}", "ticket")
            edges[(mr_id, "implements", ticket_id)] = (
                mr_id, "implements", ticket_id, f"MR !{iid} name references ticket #{tid}")
            if bid:
                edges[(bid, "for_ticket", ticket_id)] = (
                    bid, "for_ticket", ticket_id, f"branch name references ticket #{tid}")
            break
    return {"entities": list(entities.values()), "aliases": [], "edges": list(edges.values())}


def mr_document(project: str, mr: dict[str, Any], repo_name: str | None = None,
                ticket_pattern: Any = None) -> Document:
    author = (mr.get("author") or {}).get("username", "?")
    state = mr.get("state", "?")
    text = (
        f"Merge request !{mr.get('iid', '?')}: {mr.get('title', '')}\n"
        f"State: {state} | Author: {author} | "
        f"Branch: {mr.get('source_branch', '?')} -> {mr.get('target_branch', '?')}\n"
        f"Labels: {_labels(mr.get('labels'))} | Updated: {mr.get('updated_at', '')}\n\n"
        f"{mr.get('description') or '(no description)'}"
    )
    graph = mr_graph(repo_name, mr, ticket_pattern) if repo_name else {}
    return Document(
        uri=_url(mr) or f"gitlab://{project}/merge_requests/{mr.get('iid')}",
        title=f"{project} MR !{mr.get('iid')}: {mr.get('title', '')}",
        text=text,
        kind="ticket",
        updated_at=mr.get("updated_at"),
        metadata={"graph": graph} if graph else {},
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

    def _group(self) -> str:
        """Group/namespace (e.g. 'zix') for a GROUP-scoped connector (plan 09 Phase 3): its live
        tools resolve any repo in the group by name and can enumerate group-wide, so one connector
        replaces one-per-repo. Empty ⇒ a classic single-project connector."""
        return str(self.options.get("group", "")).strip("/")

    def _ingest_projects(self) -> list[str]:
        """Which project paths this connector INGESTS (its docs + MR/branch graph). A group
        connector still only ingests the projects it's told to (`projects` list) — never the whole
        group automatically — while its TOOLS reach the entire group. Falls back to the single
        `project`. A group connector with no `projects` is tools-only (ingests nothing)."""
        raw = self.options.get("projects")
        if raw:
            items = raw if isinstance(raw, list) else str(raw).split(",")
            return [p.strip() for p in items if p and p.strip()]
        return [self._project()] if self._project() else []

    def _project_api_for(self, project: str) -> str:
        return f"{self._base()}/projects/{quote(str(project), safe='')}"

    def _project_api(self) -> str:
        return self._project_api_for(self._project())

    def _repo_name_for(self, project: str) -> str:
        """The project's own repo name (group-independent) — keys the repo/branch/MR graph so it
        matches the TFS side. Prefers the GitLab `name` field; falls back to the path's last segment."""
        try:
            data = get_json(self._project_api_for(project), headers=self._headers())
            name = data.get("name") or data.get("path")
            if name:
                return str(name)
        except Exception:
            pass
        return str(project).rsplit("/", 1)[-1]

    def _repo_name(self) -> str:
        return self._repo_name_for(self._project())

    def _resolve_project_in_group(self, name: str):
        """Group-scoped tool resolution: a repo NAME → `(project_api, display, error)` via the
        group's project search (cached per name). This is what lets one `group=zix` connector's
        tools reach any repo in the group without a connector per repo."""
        name = (name or "").strip()
        if not name:
            return None, None, f"Name the repo (a project in group {self._group()})."
        cache = self.__dict__.setdefault("_proj_cache", {})
        if name.lower() in cache:
            pid, disp = cache[name.lower()]
            return self._project_api_for(pid), disp, None
        try:
            projects = get_json(
                f"{self._base()}/groups/{quote(self._group(), safe='')}/projects",
                headers=self._headers(),
                params={"search": name, "include_subgroups": "true", "per_page": 20},
            )
        except Exception as exc:
            return None, None, f"Group search failed: {exc}"
        if not projects:
            return None, None, f"No repo matching '{name}' in group {self._group()}."
        # 'connector' matches many repos (…azure.connector, appriver.connector, …); rank so the
        # most specific wins: exact name/path, then a dotted/slashed suffix match, then shortest.
        sel = name.lower()

        def _rank(x):
            nm, pth = str(x.get("name", "")).lower(), str(x.get("path", "")).lower()
            exact = sel in (nm, pth)
            suffix = any(s == sel or s.endswith("." + sel) or s.endswith("/" + sel)
                         or s.endswith("-" + sel) for s in (nm, pth))
            return (0 if exact else 1 if suffix else 2, len(pth or nm))

        p = sorted(projects, key=_rank)[0]
        cache[sel] = (p["id"], p.get("path_with_namespace"))
        return self._project_api_for(p["id"]), p.get("path_with_namespace"), None

    # --- Org-specific integration rules (all OPT-IN, off by default) -----------------
    # Conventions that tie GitLab to an org's other systems but aren't universal, so each
    # is a connector option a given org turns on: `ticket_pattern`/`ticket_in_branch` (the
    # TFS work-item id embedded in a branch/MR name) and `tfs_sync_stage` (the pipeline job
    # that mirrors a branch into TFS). An org without these conventions leaves them unset
    # and gets none of the behavior.
    def _ticket_pattern(self) -> "re.Pattern | None":
        """Compiled regex whose group 1 is the ticket id embedded in a branch/MR name, or
        None (rule off). `ticket_pattern` (custom regex) wins; else `ticket_in_branch=true`
        enables the default 'first run of 4+ digits' (a TFS work-item id like 320753-slug)."""
        custom = str(self.options.get("ticket_pattern") or "").strip()
        if custom:
            try:
                return re.compile(custom)
            except re.error:
                return None
        if as_bool(self.options.get("ticket_in_branch"), default=False):
            return re.compile(r"(\d{4,})")
        return None

    def _headers(self) -> dict[str, str]:
        token = resolve_secret(self.options, "token", "GITLAB_TOKEN")
        return {"PRIVATE-TOKEN": token} if token else {}

    def test(self) -> ConnectionStatus:
        if self._group():
            try:
                data = get_json(f"{self._base()}/groups/{quote(self._group(), safe='')}",
                                headers=self._headers())
                n = len(self._ingest_projects())
                return ConnectionStatus(True, f"Group reachable: {data.get('full_path')} "
                                        f"({n} repo(s) ingested; tools reach the whole group)")
            except Exception as exc:
                return ConnectionStatus(False, f"GitLab group error: {exc}")
        if not self._project():
            return ConnectionStatus(False, "No 'project' or 'group' configured (expected 'group/name')")
        try:
            data = get_json(self._project_api(), headers=self._headers())
            return ConnectionStatus(True, f"Reachable: {data.get('path_with_namespace')}")
        except Exception as exc:
            return ConnectionStatus(False, f"GitLab API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        since = state.get("since", "")
        # Ingest each configured project (one for a classic connector; the `projects` list for a
        # group-scoped one). One shared watermark keeps it simple — idempotent dedupe absorbs any
        # overlap. A group connector with no projects ingests nothing (tools-only).
        for project in self._ingest_projects():
            yield from self._sync_project(project, since)

    def _sync_project(self, project: str, since: str) -> Iterator[Document]:
        headers = self._headers()
        api = self._project_api_for(project)
        repo_name = self._repo_name_for(project)  # keys the MR/branch graph to match the TFS side
        ticket_pattern = self._ticket_pattern()   # org-specific: ticket id in branch/MR name (opt-in)
        mr_branches: list[str] = []

        for label, endpoint in (("merge requests", "merge_requests"), ("issues", "issues")):
            params: dict[str, Any] = {
                "state": "all", "order_by": "updated_at", "sort": "desc", "per_page": PER_PAGE,
            }
            if since:
                params["updated_after"] = since

            def fetch(page, endpoint=endpoint, params=params, api=api):
                page = page or 1
                items = get_json(f"{api}/{endpoint}", headers=headers, params={**params, "page": page})
                more = len(items) == PER_PAGE and page < MAX_PAGES
                return items, (page + 1 if more else None)

            self._stage(f"{label} · {repo_name}")
            for items in prefetch_pages(fetch, 1, self._checkpoint):
                self._stage(f"{label} · {repo_name}")
                for item in items:
                    if endpoint == "merge_requests":
                        branch = (item.get("source_branch") or "").strip()
                        if branch and branch not in mr_branches:
                            mr_branches.append(branch)
                        yield mr_document(project, item, repo_name, ticket_pattern)
                    else:
                        yield issue_document(project, item)

        self._stage(f"pipelines · {repo_name}")
        pipelines = get_json(f"{api}/pipelines", headers=headers, params={"per_page": PER_PAGE})
        if pipelines:
            yield pipelines_document(project, pipelines, self._web_base())

        yield from self._tfs_sync_documents(headers, project, api, repo_name, mr_branches)

        self._stage(f"wiki · {repo_name}")
        try:
            pages = get_json(f"{api}/wikis", headers=headers, params={"with_content": 1})
        except Exception:
            pages = []  # wiki disabled or no access
        for page in pages:
            yield wiki_document(project, page, self._web_base())

    def _find_tfs_sync(self, headers: dict, project_api: str, branch: str,
                       stage_match: str, since_iso: str) -> dict[str, Any] | None:
        """Newest-first, the first pipeline for `branch` (within the look-back) that has a
        successful job whose name/stage contains `stage_match` (case-insensitive) — the
        GitLab→TFS mirror. Bounded: at most TFS_SYNC_MAX_PIPELINES_PER_BRANCH pipelines
        inspected. Best-effort: any API error returns None (never fails the sync)."""
        try:
            pipelines = get_json(
                f"{project_api}/pipelines", headers=headers,
                params={"ref": branch, "updated_after": since_iso,
                        "order_by": "updated_at", "sort": "desc",
                        "per_page": TFS_SYNC_MAX_PIPELINES_PER_BRANCH},
            )
        except Exception:
            return None
        low = stage_match.lower()
        for p in pipelines[:TFS_SYNC_MAX_PIPELINES_PER_BRANCH]:
            pid = p.get("id")
            if pid is None:
                continue
            try:
                jobs = get_json(f"{project_api}/pipelines/{pid}/jobs", headers=headers,
                                params={"per_page": 100})
            except Exception:
                continue
            for j in jobs:
                if j.get("status") != "success":
                    continue
                if low in str(j.get("name", "")).lower() or low in str(j.get("stage", "")).lower():
                    return {"branch": branch, "pipeline_id": pid,
                            "pipeline_url": p.get("web_url", ""),
                            "job": j.get("name", ""),
                            "finished_at": j.get("finished_at") or p.get("updated_at", "")}
        return None

    def _tfs_sync_documents(self, headers: dict, project: str, project_api: str, repo_name: str,
                            branches: list[str]) -> Iterator[Document]:
        """Opt-in (set the `tfs_sync_stage` option to your mirror job/stage name substring).
        For each recent MR source branch, capture the most recent successful TFS-sync pipeline
        within the look-back and emit one `branch_sync_document`. Bounded + checkpointed."""
        stage_match = str(self.options.get("tfs_sync_stage") or "").strip()
        if not stage_match or not branches:
            return
        from datetime import datetime, timedelta, timezone

        try:
            days = int(self.options.get("tfs_sync_lookback_days") or TFS_SYNC_LOOKBACK_DAYS)
        except (TypeError, ValueError):
            days = TFS_SYNC_LOOKBACK_DAYS
        since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self._stage("tfs sync status")
        syncs: list[dict[str, Any]] = []
        for i, branch in enumerate(branches[:TFS_SYNC_MAX_BRANCHES]):
            self._checkpoint()  # a branch = a pipeline-list + jobs calls; stay stoppable
            self._stage("tfs sync status", i, min(len(branches), TFS_SYNC_MAX_BRANCHES))
            found = self._find_tfs_sync(headers, project_api, branch, stage_match, since_iso)
            if found:
                syncs.append(found)
        if syncs:
            yield branch_sync_document(project, repo_name, syncs, self._web_base())

    def tools(self) -> list[AgentTool]:
        # Delegate to the consolidated type-level builder (plan 09) — one instance is just the
        # single-connector case (the `project` selector is then optional/ignored).
        return type(self).type_tools([self])

    @classmethod
    def type_tools(cls, connectors: list["GitLabConnector"]) -> list[AgentTool]:
        """One consolidated set of READ-ONLY live tools covering ALL configured GitLab projects
        (plan 09), so N projects don't multiply into N tool sets. Each tool takes an optional
        `project` selector — a substring of the configured group/name path (e.g. "connector") —
        omit it when only one GitLab source is connected. Everything here is GET-only; nothing
        mutates GitLab."""
        P = ("string", False, "the repo/project (a name substring; for a group connector any repo "
             "in the group by name; optional when only one project is connected)")
        group_conns = [c for c in connectors if c._group()]

        def _pick(selector):
            """Resolve a `project`/`repo` selector → (api, headers, display, error). A project
            connector matches its configured path; a GROUP connector resolves any repo in the
            group by name via the API (plan 09 Phase 3)."""
            project_conns = [c for c in connectors if not c._group()]
            if len(connectors) == 1 and not group_conns:
                c = connectors[0]
                return c._project_api(), c._headers(), c._project(), None
            if selector:
                s = selector.strip().lower()
                for c in project_conns:
                    if s in str(c.options.get("project", "")).lower():
                        return c._project_api(), c._headers(), c._project(), None
                for c in group_conns:
                    api, disp, err = c._resolve_project_in_group(selector)
                    if api:
                        return api, c._headers(), disp, None
                if group_conns:
                    return None, None, None, f"No repo matching '{selector}' in group {group_conns[0]._group()}."
            if group_conns:
                return None, None, None, f"Name the repo (a project in group {group_conns[0]._group()})."
            names = ", ".join(sorted(str(c.options.get("project", "")) for c in project_conns))
            return None, None, None, f"Specify which project (one of: {names})."

        def _group_enum(kind, state, states, n, default):
            """Group-wide enumeration (a group connector, no repo named): 'list all open MRs/issues
            in the whole group'. Returns None if not applicable so the caller falls back to a
            single project."""
            if not group_conns:
                return None
            c = group_conns[0]
            st = (state or default).lower().strip()
            st = st if st in states else default
            items = get_json(
                f"{c._base()}/groups/{quote(c._group(), safe='')}/{kind}", headers=c._headers(),
                params={"state": st, "scope": "all", "order_by": "updated_at", "sort": "desc",
                        "per_page": n})
            head = "!" if kind == "merge_requests" else "#"
            label = "merge requests" if kind == "merge_requests" else "issues"
            if not items:
                return f"No {st} {label} across group {c._group()}."
            return bounded(f"{len(items)} {st} {label} across group {c._group()} (newest first):\n"
                           + "\n".join(
                f"- {head}{it.get('iid')} [{it.get('state', '?')}] {it.get('title', '')}"
                + (f" (branch {it.get('source_branch')})" if it.get("source_branch") else "")
                + f" — {(it.get('author') or {}).get('username', '?')}  {it.get('web_url', '')}"
                for it in items))

        def list_merge_requests(project=None, state="opened", max_results=30):
            n = clamp(max_results, 30, 100)
            if not project:
                grouped = _group_enum("merge_requests", state,
                                      {"opened", "closed", "merged", "locked", "all"}, n, "opened")
                if grouped is not None:
                    return grouped
            api, h, disp, err = _pick(project)
            if err:
                return err
            st = (state or "opened").lower().strip()
            st = st if st in {"opened", "closed", "merged", "locked", "all"} else "opened"
            items = get_json(f"{api}/merge_requests", headers=h,
                             params={"state": st, "order_by": "updated_at", "sort": "desc", "per_page": n})
            if not items:
                return f"No {st} merge requests in {disp}."
            return bounded(f"{len(items)} {st} merge requests in {disp} (newest first):\n" + "\n".join(
                f"- !{m.get('iid')} [{m.get('state', '?')}] {m.get('title', '')}"
                + (f" (branch {m.get('source_branch')})" if m.get("source_branch") else "")
                + f" — {(m.get('author') or {}).get('username', '?')}  {m.get('web_url', '')}"
                for m in items))

        def get_merge_request(iid, project=None):
            api, h, disp, err = _pick(project)
            if err:
                return err
            m = get_json(f"{api}/merge_requests/{iid}", headers=h)
            appr = get_json(f"{api}/merge_requests/{iid}/approvals", headers=h) if True else {}
            approvers = ", ".join((u.get("user") or u).get("username", "?")
                                  for u in (appr.get("approved_by") or [])) or "none"
            pipe = m.get("head_pipeline") or {}
            reviewers = ", ".join(r.get("username", "?") for r in (m.get("reviewers") or [])) or "none"
            return bounded(
                f"MR !{m.get('iid')}: {m.get('title', '')}\n"
                f"State: {m.get('state', '?')} | Author: {(m.get('author') or {}).get('username', '?')} | "
                f"Reviewers: {reviewers}\n"
                f"Branch: {m.get('source_branch', '?')} -> {m.get('target_branch', '?')} | "
                f"Merge status: {m.get('detailed_merge_status') or m.get('merge_status', '?')}\n"
                f"Approved by: {approvers}\n"
                f"Head pipeline: {pipe.get('status', 'none')} {pipe.get('web_url', '')}\n"
                f"URL: {m.get('web_url', '')}\n\n{m.get('description') or '(no description)'}")

        def merge_request_reviews(iid, project=None, max_results=30):
            api, h, disp, err = _pick(project)
            if err:
                return err
            appr = get_json(f"{api}/merge_requests/{iid}/approvals", headers=h)
            approvers = ", ".join((u.get("user") or u).get("username", "?")
                                  for u in (appr.get("approved_by") or [])) or "none"
            notes = get_json(f"{api}/merge_requests/{iid}/notes", headers=h,
                             params={"per_page": clamp(max_results, 30, 100), "sort": "asc"})
            human = [n for n in notes if not n.get("system")]
            lines = [f"- {(n.get('author') or {}).get('username', '?')}: "
                     f"{(n.get('body') or '').strip()[:300]}" for n in human]
            return bounded(f"MR !{iid} — approved by: {approvers}\nComments ({len(lines)}):\n"
                           + ("\n".join(lines) or "(none)"))

        def list_issues(project=None, state="opened", max_results=30):
            n = clamp(max_results, 30, 100)
            if not project:
                grouped = _group_enum("issues", state, {"opened", "closed", "all"}, n, "opened")
                if grouped is not None:
                    return grouped
            api, h, disp, err = _pick(project)
            if err:
                return err
            st = (state or "opened").lower().strip()
            st = st if st in {"opened", "closed", "all"} else "opened"
            items = get_json(f"{api}/issues", headers=h,
                             params={"state": st, "order_by": "updated_at", "sort": "desc", "per_page": n})
            if not items:
                return f"No {st} issues in {disp}."
            return bounded(f"{len(items)} {st} issues in {disp} (newest first):\n" + "\n".join(
                f"- #{i.get('iid')} [{i.get('state', '?')}] {i.get('title', '')} — "
                f"{(i.get('author') or {}).get('username', '?')}  {i.get('web_url', '')}"
                for i in items))

        def pipeline_status(ref, project=None, max_results=5):
            api, h, disp, err = _pick(project)
            if err:
                return err
            branch = (ref or "").strip()
            if not branch:
                return "Provide a branch/ref name."
            items = get_json(f"{api}/pipelines", headers=h,
                             params={"ref": branch, "order_by": "updated_at", "sort": "desc",
                                     "per_page": clamp(max_results, 5, 20)})
            if not items:
                return f"No pipelines for '{branch}' in {disp}."
            return bounded(f"Pipelines for '{branch}' in {disp} (newest first):\n" + "\n".join(
                f"- #{p.get('id')} [{p.get('status', '?')}] {p.get('updated_at', '')}  {p.get('web_url', '')}"
                for p in items))

        def pipeline_jobs(pipeline_id, project=None):
            api, h, disp, err = _pick(project)
            if err:
                return err
            jobs = get_json(f"{api}/pipelines/{pipeline_id}/jobs", headers=h, params={"per_page": 100})
            if not jobs:
                return f"No jobs for pipeline #{pipeline_id}."
            return bounded(f"Pipeline #{pipeline_id} jobs (by stage):\n" + "\n".join(
                f"- [{j.get('status', '?')}] {j.get('stage', '?')}/{j.get('name', '?')} "
                f"(job {j.get('id')})  {j.get('web_url', '')}" for j in jobs))

        def job_log(job_id, project=None):
            api, h, disp, err = _pick(project)
            if err:
                return err
            import httpx
            resp = httpx.get(f"{api}/jobs/{job_id}/trace", headers=h, timeout=60.0, follow_redirects=True)
            if resp.status_code == 404:
                return f"No log for job {job_id} (not found or not yet run)."
            resp.raise_for_status()
            trace = resp.text
            tail = trace[-6000:] if len(trace) > 6000 else trace
            return f"Log tail for job {job_id}:\n{tail}"

        def list_commits(project=None, ref=None, since=None, path=None, max_results=20):
            api, h, disp, err = _pick(project)
            if err:
                return err
            params = {"per_page": clamp(max_results, 20, 100)}
            if ref:
                params["ref_name"] = ref
            if since:
                params["since"] = since
            if path:
                params["path"] = path
            items = get_json(f"{api}/repository/commits", headers=h, params=params)
            if not items:
                return "No commits found for that filter."
            return bounded(f"{len(items)} commits (newest first):\n" + "\n".join(
                f"- {ci.get('short_id')} {ci.get('title', '')} — {ci.get('author_name', '?')} "
                f"({ci.get('created_at', '')})" for ci in items))

        def compare(from_ref, to_ref, project=None):
            api, h, disp, err = _pick(project)
            if err:
                return err
            data = get_json(f"{api}/repository/compare", headers=h,
                            params={"from": from_ref, "to": to_ref})
            commits = data.get("commits") or []
            diffs = data.get("diffs") or []
            files = "\n".join(f"  {d.get('new_path', d.get('old_path', '?'))}" for d in diffs[:40])
            return bounded(f"{from_ref}...{to_ref}: {len(commits)} commits, {len(diffs)} files changed\n"
                           f"Files:\n{files}")

        def list_branches(project=None, search=None, max_results=40):
            api, h, disp, err = _pick(project)
            if err:
                return err
            params = {"per_page": clamp(max_results, 40, 100)}
            if search:
                params["search"] = search
            items = get_json(f"{api}/repository/branches", headers=h, params=params)
            if not items:
                return "No branches match."
            return bounded(f"{len(items)} branches:\n" + "\n".join(
                f"- {b.get('name')}{' [protected]' if b.get('protected') else ''} — "
                f"last: {(b.get('commit') or {}).get('short_id', '?')} "
                f"{(b.get('commit') or {}).get('title', '')[:60]}" for b in items))

        def project_info(project=None):
            api, h, disp, err = _pick(project)
            if err:
                return err
            p = get_json(api, headers=h)
            langs = {}
            try:
                langs = get_json(f"{api}/languages", headers=h)
            except Exception:
                pass
            lang_str = ", ".join(f"{k} {v}%" for k, v in sorted(
                langs.items(), key=lambda kv: -kv[1])[:6]) or "n/a"
            return bounded(
                f"{p.get('name_with_namespace', p.get('name', '?'))}\n"
                f"Default branch: {p.get('default_branch', '?')} | Visibility: {p.get('visibility', '?')}\n"
                f"Open issues: {p.get('open_issues_count', '?')} | Last activity: {p.get('last_activity_at', '?')}\n"
                f"Topics: {', '.join(p.get('topics') or []) or 'none'}\nLanguages: {lang_str}\n"
                f"URL: {p.get('web_url', '')}\n\n{p.get('description') or '(no description)'}")

        def search_code(query, project=None):
            api, h, disp, err = _pick(project)
            if err:
                return err
            items = get_json(f"{api}/search", headers=h,
                             params={"scope": "blobs", "search": query, "per_page": 10})
            if not items:
                return "No code matches."
            return bounded("\n".join(
                f"- {i.get('path', i.get('filename', '?'))} (ref {i.get('ref', '?')}): "
                f"{(i.get('data') or '').strip().splitlines()[0][:120] if i.get('data') else ''}"
                for i in items))

        def get_file(path, project=None, ref="HEAD"):
            api, h, disp, err = _pick(project)
            if err:
                return err
            import httpx
            resp = httpx.get(f"{api}/repository/files/{quote(path, safe='')}/raw", headers=h,
                             params={"ref": ref}, timeout=60.0, follow_redirects=True)
            resp.raise_for_status()
            return resp.text[:20000]

        # ---- P2 ----
        def list_releases(project=None, max_results=20):
            api, h, disp, err = _pick(project)
            if err:
                return err
            items = get_json(f"{api}/releases", headers=h, params={"per_page": clamp(max_results, 20, 100)})
            if not items:
                return f"No releases in {disp}."
            return bounded(f"{len(items)} releases (newest first):\n" + "\n".join(
                f"- {r.get('name') or r.get('tag_name')} (tag {r.get('tag_name')}, {str(r.get('released_at', ''))[:10]})"
                for r in items))

        def list_milestones(project=None, state="active", max_results=30):
            api, h, disp, err = _pick(project)
            if err:
                return err
            st = state if state in {"active", "closed"} else "active"
            items = get_json(f"{api}/milestones", headers=h,
                             params={"state": st, "per_page": clamp(max_results, 30, 100)})
            if not items:
                return f"No {st} milestones in {disp}."
            return bounded(f"{len(items)} {st} milestones:\n" + "\n".join(
                f"- {m.get('title')} [{m.get('state', '?')}]"
                + (f" due {m.get('due_date')}" if m.get("due_date") else "") for m in items))

        def list_members(project=None, max_results=50):
            api, h, disp, err = _pick(project)
            if err:
                return err
            items = get_json(f"{api}/members/all", headers=h, params={"per_page": clamp(max_results, 50, 100)})
            if not items:
                return f"No members visible for {disp}."
            levels = {50: "owner", 40: "maintainer", 30: "developer", 20: "reporter", 10: "guest"}
            return bounded(f"{len(items)} members:\n" + "\n".join(
                f"- {u.get('username', '?')} ({levels.get(u.get('access_level'), u.get('access_level'))})"
                for u in items))

        def list_contributors(project=None, max_results=30):
            api, h, disp, err = _pick(project)
            if err:
                return err
            items = get_json(f"{api}/repository/contributors", headers=h,
                             params={"order_by": "commits", "sort": "desc", "per_page": clamp(max_results, 30, 100)})
            if not items:
                return "No contributors."
            return bounded(f"Top contributors:\n" + "\n".join(
                f"- {ct.get('name', '?')} — {ct.get('commits', '?')} commits" for ct in items))

        def list_environments(project=None, max_results=30):
            api, h, disp, err = _pick(project)
            if err:
                return err
            items = get_json(f"{api}/environments", headers=h, params={"per_page": clamp(max_results, 30, 100)})
            if not items:
                return f"No environments in {disp}."
            return bounded(f"{len(items)} environments:\n" + "\n".join(
                f"- {e.get('name')} [{e.get('state', '?')}] {e.get('external_url') or ''}" for e in items))

        def list_tree(project=None, path="", ref=None, max_results=100):
            api, h, disp, err = _pick(project)
            if err:
                return err
            params = {"path": path, "per_page": clamp(max_results, 100, 100)}
            if ref:
                params["ref"] = ref
            items = get_json(f"{api}/repository/tree", headers=h, params=params)
            if not items:
                return f"Nothing at path {path!r}."
            return bounded(f"{len(items)} entries at {path or '/'}:\n" + "\n".join(
                f"- {'[dir] ' if t.get('type') == 'tree' else ''}{t.get('name')}" for t in items))

        return [
            read_tool("gitlab_list_merge_requests",
                      "List CURRENT merge requests by state (opened/closed/merged/all). Use for "
                      "'list all open MRs' — learned memory can't enumerate them.",
                      list_merge_requests, {"project": P,
                      "state": ("string", False, "opened (default)|closed|merged|all"),
                      "max_results": ("integer", False, "")}),
            read_tool("gitlab_get_merge_request",
                      "Full details of one MR by iid: state, author, reviewers, source→target, "
                      "merge status, who approved, head-pipeline status, description.",
                      get_merge_request, {"iid": ("integer", True, "the MR iid (the !N number)"), "project": P}),
            read_tool("gitlab_merge_request_reviews",
                      "Who approved an MR and its human review comments (by iid).",
                      merge_request_reviews, {"iid": ("integer", True, "the MR iid"), "project": P,
                      "max_results": ("integer", False, "")}),
            read_tool("gitlab_list_issues",
                      "List CURRENT issues by state (opened/closed/all).",
                      list_issues, {"project": P,
                      "state": ("string", False, "opened (default)|closed|all"),
                      "max_results": ("integer", False, "")}),
            read_tool("gitlab_pipeline_status",
                      "Latest CI pipeline(s) for a branch/ref — status + link (LIVE). 'did the "
                      "build for branch X succeed?' / 'link to the build'. Pass an MR source branch.",
                      pipeline_status, {"ref": ("string", True, "branch or ref name"), "project": P,
                      "max_results": ("integer", False, "")}),
            read_tool("gitlab_pipeline_jobs",
                      "The jobs of a pipeline (by pipeline id) with per-stage status — shows which "
                      "job failed. Get the id from gitlab_pipeline_status.",
                      pipeline_jobs, {"pipeline_id": ("integer", True, "the pipeline id"), "project": P}),
            read_tool("gitlab_job_log",
                      "Tail of a CI job's log (by job id, from gitlab_pipeline_jobs) — 'why did the "
                      "build fail'.",
                      job_log, {"job_id": ("integer", True, "the job id"), "project": P}),
            read_tool("gitlab_list_commits",
                      "Recent commits, optionally filtered by ref/branch, author-since date, or path.",
                      list_commits, {"project": P, "ref": ("string", False, "branch/ref"),
                      "since": ("string", False, "ISO date, e.g. 2026-07-01"),
                      "path": ("string", False, "limit to a file/dir"),
                      "max_results": ("integer", False, "")}),
            read_tool("gitlab_compare",
                      "Compare two refs (branches/tags/SHAs): how many commits ahead and which files "
                      "changed.",
                      compare, {"from_ref": ("string", True, "base ref"),
                      "to_ref": ("string", True, "head ref"), "project": P}),
            read_tool("gitlab_list_branches",
                      "List branches (optionally matching a search string) with their last commit.",
                      list_branches, {"project": P, "search": ("string", False, "name substring"),
                      "max_results": ("integer", False, "")}),
            read_tool("gitlab_project_info",
                      "Project overview: default branch, visibility, topics, languages, open-issue "
                      "count, last activity, description.",
                      project_info, {"project": P}),
            read_tool("gitlab_search_code",
                      "Live search for code in the project. Returns matching file paths.",
                      search_code, {"query": ("string", True, "search text"), "project": P}),
            read_tool("gitlab_get_file",
                      "Live-read a file's contents (default ref HEAD).",
                      get_file, {"path": ("string", True, "file path"), "project": P,
                      "ref": ("string", False, "branch/tag/SHA")}),
            read_tool("gitlab_list_releases", "List the project's releases (tag, name, date).",
                      list_releases, {"project": P, "max_results": ("integer", False, "")}),
            read_tool("gitlab_list_milestones", "List milestones by state (active/closed) with due dates.",
                      list_milestones, {"project": P, "state": ("string", False, "active (default)|closed"),
                      "max_results": ("integer", False, "")}),
            read_tool("gitlab_list_members", "Who has access to the project and at what level.",
                      list_members, {"project": P, "max_results": ("integer", False, "")}),
            read_tool("gitlab_list_contributors", "Top commit contributors to the project.",
                      list_contributors, {"project": P, "max_results": ("integer", False, "")}),
            read_tool("gitlab_list_environments",
                      "Deployment environments (name, state, URL) — where the project runs.",
                      list_environments, {"project": P, "max_results": ("integer", False, "")}),
            read_tool("gitlab_list_tree", "List files/dirs at a path (default repo root, HEAD).",
                      list_tree, {"project": P, "path": ("string", False, "dir path"),
                      "ref": ("string", False, "branch/tag/SHA"), "max_results": ("integer", False, "")}),
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
