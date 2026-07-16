"""Azure DevOps connector: Boards work items, Repos pull requests, and Pipelines runs."""

from __future__ import annotations

import base64
from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, post_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

DEFAULT_API_VERSION = "7.0"
WORK_ITEM_BATCH = 200
MAX_WORK_ITEMS = 2000


def _as_bool(value: Any, default: bool = True) -> bool:
    """Coerce a form/option value (bool or string) to bool. Form values arrive as
    strings, so "false"/"0"/"no"/"off" (any case) read as False."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


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


def pull_request_document(org_url: str, project: str, pr: dict[str, Any]) -> Document:
    repo = pr.get("repository", {}).get("name", "?")
    text = (
        f"Pull request !{pr['pullRequestId']} in {repo}: {pr.get('title', '')}\n"
        f"Status: {pr.get('status', '?')} | Author: {pr.get('createdBy', {}).get('displayName', '?')} | "
        f"{pr.get('sourceRefName', '')} -> {pr.get('targetRefName', '')}\n\n"
        f"{pr.get('description') or '(no description)'}"
    )
    return Document(
        uri=f"{org_url}/{project}/_git/{repo}/pullrequest/{pr['pullRequestId']}",
        title=f"{repo} PR !{pr['pullRequestId']}: {pr.get('title', '')}",
        text=text,
        kind="ticket",
        updated_at=pr.get("creationDate"),
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

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        org_url, project, headers = self._org_url(), self._project(), self._headers()
        api, verify = self._api(), self._verify()
        since = state.get("since", "")

        # -- Boards work items (WIQL query, then detail batches) -------------
        changed_clause = f" AND [System.ChangedDate] >= '{since[:10]}'" if since else ""
        wiql = {
            "query": (
                "SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.TeamProject] = '{project}'{changed_clause} "
                "ORDER BY [System.ChangedDate] DESC"
            )
        }
        result = post_json(
            f"{org_url}/{project}/_apis/wit/wiql?{api}", wiql, headers=headers, verify=verify
        )
        ids = [w["id"] for w in result.get("workItems", [])][:MAX_WORK_ITEMS]
        for i in range(0, len(ids), WORK_ITEM_BATCH):
            batch = ids[i : i + WORK_ITEM_BATCH]
            items = get_json(
                f"{org_url}/{project}/_apis/wit/workitems?{api}",
                headers=headers,
                params={"ids": ",".join(map(str, batch))},
                verify=verify,
            ).get("value", [])
            for item in items:
                yield work_item_document(org_url, item)

        # -- Repos pull requests ---------------------------------------------
        prs = get_json(
            f"{org_url}/{project}/_apis/git/pullrequests?{api}",
            headers=headers,
            params={"searchCriteria.status": "all", "$top": 100},
            verify=verify,
        ).get("value", [])
        for pr in prs:
            yield pull_request_document(org_url, project, pr)

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
                        "Live-query Azure DevOps Boards with a WIQL query (current state, not memory). "
                        "Example: SELECT [System.Id] FROM WorkItems WHERE [System.State] = 'Active'"
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
        elif event_type.startswith("git.pullrequest") and resource.get("pullRequestId"):
            yield pull_request_document(self._org_url(), self._project(), resource)
