"""Azure DevOps connector: Boards work items, Repos pull requests, and Pipelines runs."""

from __future__ import annotations

import base64
from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, post_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

API = "api-version=7.0"
WORK_ITEM_BATCH = 200
MAX_WORK_ITEMS = 2000


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
        org = str(self.options.get("organization", ""))
        return f"https://dev.azure.com/{org}"

    def _project(self) -> str:
        return str(self.options.get("project", ""))

    def _headers(self) -> dict[str, str]:
        pat = resolve_secret(self.options, "token", "AZURE_DEVOPS_PAT") or ""
        encoded = base64.b64encode(f":{pat}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    def test(self) -> ConnectionStatus:
        if not self.options.get("organization") or not self._project():
            return ConnectionStatus(False, "Need 'organization' and 'project' options")
        try:
            data = get_json(
                f"{self._org_url()}/_apis/projects/{self._project()}?{API}",
                headers=self._headers(),
            )
            return ConnectionStatus(True, f"Project reachable: {data.get('name')}")
        except Exception as exc:
            return ConnectionStatus(False, f"Azure DevOps API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        org_url, project, headers = self._org_url(), self._project(), self._headers()
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
            f"{org_url}/{project}/_apis/wit/wiql?{API}", wiql, headers=headers
        )
        ids = [w["id"] for w in result.get("workItems", [])][:MAX_WORK_ITEMS]
        for i in range(0, len(ids), WORK_ITEM_BATCH):
            batch = ids[i : i + WORK_ITEM_BATCH]
            items = get_json(
                f"{org_url}/{project}/_apis/wit/workitems?{API}",
                headers=headers,
                params={"ids": ",".join(map(str, batch))},
            ).get("value", [])
            for item in items:
                yield work_item_document(org_url, item)

        # -- Repos pull requests ---------------------------------------------
        prs = get_json(
            f"{org_url}/{project}/_apis/git/pullrequests?{API}",
            headers=headers,
            params={"searchCriteria.status": "all", "$top": 100},
        ).get("value", [])
        for pr in prs:
            yield pull_request_document(org_url, project, pr)

        # -- Pipelines + recent runs ------------------------------------------
        pipelines = get_json(
            f"{org_url}/{project}/_apis/pipelines?{API}", headers=headers
        ).get("value", [])
        all_runs: list[dict[str, Any]] = []
        for p in pipelines[:25]:
            runs = get_json(
                f"{org_url}/{project}/_apis/pipelines/{p['id']}/runs?{API}", headers=headers
            ).get("value", [])
            for r in runs[:5]:
                r["pipeline_name"] = p.get("name", "?")
                all_runs.append(r)
        if all_runs:
            yield pipelines_document(org_url, project, all_runs)

    def tools(self) -> list[AgentTool]:
        org_url, project, headers = self._org_url(), self._project(), self._headers()

        def ado_query_work_items(wiql_query: str) -> str:
            result = post_json(
                f"{org_url}/{project}/_apis/wit/wiql?{API}",
                {"query": wiql_query},
                headers=headers,
            )
            ids = [str(w["id"]) for w in result.get("workItems", [])][:20]
            if not ids:
                return "No work items match."
            items = get_json(
                f"{org_url}/{project}/_apis/wit/workitems?{API}",
                headers=headers,
                params={"ids": ",".join(ids)},
            ).get("value", [])
            return "\n".join(
                f"- #{i['id']}: {i.get('fields', {}).get('System.Title', '')} "
                f"[{i.get('fields', {}).get('System.State', '?')}]"
                for i in items
            )

        def ado_search_code(query: str) -> str:
            org = str(self.options.get("organization", ""))
            try:
                data = post_json(
                    f"https://almsearch.dev.azure.com/{org}/{project}/_apis/search/codesearchresults?{API}",
                    {"searchText": query, "$top": 10},
                    headers=headers,
                )
            except Exception as exc:
                return f"Code search failed (is the Code Search extension installed in the org?): {exc}"
            results = data.get("results", [])
            if not results:
                return "No code matches."
            return "\n".join(
                f"- {r.get('repository', {}).get('name', '?')}{r.get('path', '?')}"
                for r in results
            )

        def ado_get_file(repository: str, path: str) -> str:
            data = get_json(
                f"{org_url}/{project}/_apis/git/repositories/{repository}/items?{API}",
                headers=headers,
                params={"path": path, "includeContent": "true"},
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
