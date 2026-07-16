"""Azure DevOps connector: Boards work items, Repos pull requests, and Pipelines runs."""

from __future__ import annotations

import base64
from typing import Any, Iterator
from urllib.parse import quote

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import as_bool as _as_bool, get_json, post_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

DEFAULT_API_VERSION = "7.0"
WORK_ITEM_BATCH = 200
DEFAULT_SPRINTS = 10       # per team, most recent N iterations that have started
TEAM_PAGE = 100            # teams list page size
MAX_WORK_ITEMS = 8000      # safety cap across all teams' recent sprints


def work_item_document(org_url: str, item: dict[str, Any]) -> Document:
    f = item.get("fields", {})
    assignee = (f.get("System.AssignedTo") or {})
    assignee_name = assignee.get("displayName", "unassigned") if isinstance(assignee, dict) else str(assignee)
    text = (
        f"{f.get('System.WorkItemType', 'Work item')} #{item['id']}: {f.get('System.Title', '')}\n"
        f"State: {f.get('System.State', '?')} | Assigned to: {assignee_name} | "
        f"Area: {f.get('System.AreaPath', '')} | Iteration: {f.get('System.IterationPath', '')}\n"
        f"Tags: {f.get('System.Tags', 'none')} | Updated: {f.get('System.ChangedDate', '')}\n\n"
        f"{_strip_html(f.get('System.Description') or '(no description)')}"
    )
    return Document(
        uri=f"{org_url}/_workitems/edit/{item['id']}",
        title=f"#{item['id']}: {f.get('System.Title', '')}",
        text=text,
        kind="ticket",
        updated_at=f.get("System.ChangedDate"),
    )


def select_recent_iterations(iterations: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """The most recent `count` iterations (sprints) — current plus the prior ones.

    Iterations without a start date (loose backlog buckets) are ignored, and *future*
    sprints (the server's `timeFrame`) are excluded so "recent" means current + past
    rather than upcoming, still-empty ones. The rest are ordered by start date and the
    trailing `count` returned. `count <= 0` means all such iterations.
    """
    dated = [it for it in iterations if (it.get("attributes") or {}).get("startDate")]
    started = [it for it in dated if (it["attributes"].get("timeFrame") or "").lower() != "future"]
    pool = started or dated  # fall back to all dated if the server omits timeFrame
    pool.sort(key=lambda it: it["attributes"]["startDate"])
    return pool[-count:] if count and count > 0 else pool


def build_map_document(org_url: str, project: str, definitions: list[dict[str, Any]]) -> Document:
    """One doc mapping each build pipeline to the repository it builds, with graph
    edges `pipeline --builds--> repo`.

    This is the TFS↔GitLab bridge: at AppRiver, merge requests land in GitLab, each
    branch mirrors into a same-named TFS Git repo, and the build pipelines live in TFS.
    The TFS repo names therefore match the GitLab repo names, so emitting `repo:` entities
    here lets the knowledge graph connect a GitLab repo to the TFS pipeline that builds it
    (repo-name aliasing in connectors/deps.py reconciles the spoken forms).
    """
    lines: list[str] = []
    entities: dict[str, tuple[str, str, str]] = {}
    edges: list[tuple[str, str, str, str]] = []
    for d in definitions:
        name = d.get("name", "?")
        repo = d.get("repository") or {}
        rname = repo.get("name")
        if not rname:
            continue
        branch = str(repo.get("defaultBranch") or "").replace("refs/heads/", "")
        lines.append(f"- Pipeline '{name}' builds repo {rname}"
                     + (f" (default branch {branch})" if branch else ""))
        pid, rid = f"pipeline:{name.lower()}", f"repo:{rname.lower()}"
        entities[pid] = (pid, name, "pipeline")
        entities[rid] = (rid, rname, "repo")
        edges.append((pid, "builds", rid, f"default branch {branch}" if branch else "builds"))
    return Document(
        uri=f"{org_url}/{project}/_build/definitions",
        title=f"{project}: build pipelines and the repositories they build",
        text=("Build pipelines and their source repositories. At AppRiver, merge requests are made "
              "in GitLab, each branch syncs into a same-named TFS Git repo, and the build pipelines "
              "run in TFS — so these repo names match the GitLab repositories:\n" + "\n".join(lines)),
        kind="pipeline",
        metadata={"graph": {"entities": sorted(entities.values()), "aliases": [], "edges": edges}},
    )


def pipelines_document(org_url: str, project: str, runs: list[dict[str, Any]]) -> Document:
    lines = [
        f"- {r.get('pipeline_name', '?')} run #{r.get('id', '?')}: "
        f"{r.get('state', '?')}/{r.get('result') or 'pending'} ({r.get('finishedDate') or r.get('createdDate', '')})"
        for r in runs
    ]
    return Document(
        uri=f"{org_url}/{project}/_build",
        title=f"{project}: recent Azure Pipelines runs",
        text=f"Recent pipeline runs in {project}:\n" + "\n".join(lines),
        kind="pipeline",
    )


def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").get_text("\n").strip()


@register
class AzureDevOpsConnector(Connector):
    type_name = "azure_devops"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _org_url(self) -> str:
        """Collection/organization base URL. SaaS: https://dev.azure.com/{organization}.
        On-prem Azure DevOps Server / TFS: {server_url}/{collection}, e.g.
        https://tfs.company.com/tfs/DefaultCollection — set `server_url` (the host up to
        and including /tfs) and `collection` for that path."""
        server_url = str(self.options.get("server_url", "")).rstrip("/")
        if server_url:
            collection = str(self.options.get("collection", "")).strip("/")
            return f"{server_url}/{collection}" if collection else server_url
        org = str(self.options.get("organization", ""))
        return f"https://dev.azure.com/{org}"

    def _project(self) -> str:
        return str(self.options.get("project", ""))

    def _api(self) -> str:
        """api-version query fragment. Azure DevOps Server pins to the version its release
        supports (2022 → 7.x, 2020 → 6.0, 2019 → 5.0); override via the `api_version` option."""
        return f"api-version={self.options.get('api_version') or DEFAULT_API_VERSION}"

    def _verify(self) -> bool:
        """TLS verification. On-prem servers often use an internal-CA/self-signed cert;
        set `verify_tls=false` (mirrors a TFS_INSECURE=true setup) to skip verification."""
        return _as_bool(self.options.get("verify_tls"), default=True)

    def _search_url(self) -> str:
        """Code-search base. SaaS hosts search on a separate almsearch.* host; on-prem
        serves it from the same collection URL."""
        if self.options.get("server_url"):
            return self._org_url()
        org = str(self.options.get("organization", ""))
        return f"https://almsearch.dev.azure.com/{org}"

    def _headers(self) -> dict[str, str]:
        # PAT via Basic auth — works for both SaaS and Azure DevOps Server 2017+.
        pat = resolve_secret(self.options, "token", "AZURE_DEVOPS_PAT") or ""
        encoded = base64.b64encode(f":{pat}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    def test(self) -> ConnectionStatus:
        if not (self.options.get("organization") or self.options.get("server_url")):
            return ConnectionStatus(False, "Need 'organization' (SaaS) or 'server_url' + 'collection' (on-prem)")
        if not self._project():
            return ConnectionStatus(False, "Need a 'project' option")
        try:
            data = get_json(
                f"{self._org_url()}/_apis/projects/{self._project()}?{self._api()}",
                headers=self._headers(),
                verify=self._verify(),
            )
            return ConnectionStatus(True, f"Project reachable: {data.get('name')}")
        except Exception as exc:
            return ConnectionStatus(False, f"Azure DevOps API error: {exc}")

    def _teams(self, org_url: str, headers: dict, api: str, verify: bool) -> list[str]:
        """Team names to ingest. The `teams` option restricts to a named subset;
        otherwise every team in the project is enumerated (paginated). Teams without
        sprints are skipped later, so a project with 100 teams where only a handful use
        iterations still costs little beyond the one iterations probe per team."""
        configured = self.options.get("teams")
        if configured:
            names = configured if isinstance(configured, list) else str(configured).split(",")
            return [n.strip() for n in names if n and n.strip()]
        teams, skip, project = [], 0, self._project()
        while True:
            page = get_json(
                f"{org_url}/_apis/projects/{project}/teams?{api}",
                headers=headers, params={"$top": TEAM_PAGE, "$skip": skip}, verify=verify,
            ).get("value", [])
            teams += [t["name"] for t in page if t.get("name")]
            if len(page) < TEAM_PAGE:
                return teams
            skip += TEAM_PAGE

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        org_url, project, headers = self._org_url(), self._project(), self._headers()
        api, verify = self._api(), self._verify()
        sprints = int(self.options.get("sprints") or DEFAULT_SPRINTS)

        # -- Boards work items, by team over the last N sprints ---------------
        # A 300k-item project is far too large to pull flat; instead we walk each
        # team's most recent sprints and ingest the work items planned into them.
        # Anything older / outside these sprints is answered live via the WIQL tool.
        seen: set[int] = set()
        wanted: list[int] = []
        for team in self._teams(org_url, headers, api, verify):
            tp = quote(team, safe="")
            try:
                iters = get_json(
                    f"{org_url}/{project}/{tp}/_apis/work/teamsettings/iterations?{api}",
                    headers=headers, verify=verify,
                ).get("value", [])
            except Exception:
                continue  # team has no iteration settings, or no access — skip
            for it in select_recent_iterations(iters, sprints):
                try:
                    rels = get_json(
                        f"{org_url}/{project}/{tp}/_apis/work/teamsettings/iterations/{it['id']}/workitems?{api}",
                        headers=headers, verify=verify,
                    ).get("workItemRelations", [])
                except Exception:
                    continue
                for r in rels:
                    tid = (r.get("target") or {}).get("id")
                    if tid and tid not in seen:
                        seen.add(tid)
                        wanted.append(tid)
            if len(wanted) >= MAX_WORK_ITEMS:
                wanted = wanted[:MAX_WORK_ITEMS]
                break
        for i in range(0, len(wanted), WORK_ITEM_BATCH):
            batch = wanted[i : i + WORK_ITEM_BATCH]
            items = get_json(
                f"{org_url}/{project}/_apis/wit/workitems?{api}",
                headers=headers,
                params={"ids": ",".join(map(str, batch))},
                verify=verify,
            ).get("value", [])
            for item in items:
                yield work_item_document(org_url, item)

        # -- Build pipelines -> repositories (TFS↔GitLab bridge) --------------
        definitions = get_json(
            f"{org_url}/{project}/_apis/build/definitions?{api}",
            headers=headers, params={"includeAllProperties": "true", "$top": 2000}, verify=verify,
        ).get("value", [])
        if definitions:
            yield build_map_document(org_url, project, definitions)

        # -- Pipelines + recent runs ------------------------------------------
        pipelines = get_json(
            f"{org_url}/{project}/_apis/pipelines?{api}", headers=headers, verify=verify
        ).get("value", [])
        all_runs: list[dict[str, Any]] = []
        for p in pipelines[:25]:
            runs = get_json(
                f"{org_url}/{project}/_apis/pipelines/{p['id']}/runs?{api}", headers=headers, verify=verify
            ).get("value", [])
            for r in runs[:5]:
                r["pipeline_name"] = p.get("name", "?")
                all_runs.append(r)
        if all_runs:
            yield pipelines_document(org_url, project, all_runs)

    def tools(self) -> list[AgentTool]:
        org_url, project, headers = self._org_url(), self._project(), self._headers()
        api, verify, search_url = self._api(), self._verify(), self._search_url()

        def ado_query_work_items(wiql_query: str) -> str:
            result = post_json(
                f"{org_url}/{project}/_apis/wit/wiql?{api}",
                {"query": wiql_query},
                headers=headers,
                verify=verify,
            )
            ids = [str(w["id"]) for w in result.get("workItems", [])][:20]
            if not ids:
                return "No work items match."
            items = get_json(
                f"{org_url}/{project}/_apis/wit/workitems?{api}",
                headers=headers,
                params={"ids": ",".join(ids)},
                verify=verify,
            ).get("value", [])
            return "\n".join(
                f"- #{i['id']}: {i.get('fields', {}).get('System.Title', '')} "
                f"[{i.get('fields', {}).get('System.State', '?')}]"
                for i in items
            )

        def ado_search_code(query: str) -> str:
            try:
                data = post_json(
                    f"{search_url}/{project}/_apis/search/codesearchresults?{api}",
                    {"searchText": query, "$top": 10},
                    headers=headers,
                    verify=verify,
                )
            except Exception as exc:
                return f"Code search failed (is the Code Search extension installed?): {exc}"
            results = data.get("results", [])
            if not results:
                return "No code matches."
            return "\n".join(
                f"- {r.get('repository', {}).get('name', '?')}{r.get('path', '?')}"
                for r in results
            )

        def ado_get_file(repository: str, path: str) -> str:
            data = get_json(
                f"{org_url}/{project}/_apis/git/repositories/{repository}/items?{api}",
                headers=headers,
                params={"path": path, "includeContent": "true"},
                verify=verify,
            )
            return str(data.get("content", "(no content returned)"))[:20000]

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"ado_query_work_items_{self.name}",
                    description=(
                        "Live-query Azure DevOps/TFS Boards with a WIQL query (current state, not memory). "
                        "Only each team's most recent sprints are ingested into memory, so use THIS tool for "
                        "any work item outside that slice — older items, other teams, a specific #id, or a "
                        "custom filter. Example: SELECT [System.Id] FROM WorkItems WHERE [System.State] = 'Active'"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"wiql_query": {"type": "string"}},
                        "required": ["wiql_query"],
                    },
                ),
                fn=ado_query_work_items,
            ),
            AgentTool(
                spec=ToolSpec(
                    name=f"ado_search_code_{self.name}",
                    description=f"Live code search across Azure DevOps repos in project {project}. Returns repo/path matches.",
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                ),
                fn=ado_search_code,
            ),
            AgentTool(
                spec=ToolSpec(
                    name=f"ado_get_file_{self.name}",
                    description=f"Live-read a file from an Azure DevOps repo in project {project} at its current HEAD.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "repository": {"type": "string"},
                            "path": {"type": "string"},
                        },
                        "required": ["repository", "path"],
                    },
                ),
                fn=ado_get_file,
            ),
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        resource = payload.get("resource", {})
        event_type = payload.get("eventType", "")
        if event_type.startswith("workitem.") and resource.get("id"):
            yield work_item_document(self._org_url(), resource)
