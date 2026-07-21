"""Jira connector: issues, epics, and roadmap items via the REST API + live JQL tool."""

from __future__ import annotations

from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

PAGE_SIZE = 50
MAX_ISSUES = 1000
FIELDS = "summary,description,status,assignee,labels,issuetype,updated,priority,parent"


def issue_document(base_url: str, issue: dict[str, Any]) -> Document:
    fields = issue.get("fields", {})
    assignee = (fields.get("assignee") or {}).get("displayName", "unassigned")
    status = (fields.get("status") or {}).get("name", "?")
    itype = (fields.get("issuetype") or {}).get("name", "issue")
    parent = (fields.get("parent") or {}).get("key")
    priority = (fields.get("priority") or {}).get("name", "")
    description = fields.get("description") or "(no description)"
    if isinstance(description, dict):  # Atlassian Document Format (API v3)
        description = _adf_to_text(description)
    text = (
        f"{itype} {issue['key']}: {fields.get('summary', '')}\n"
        f"Status: {status} | Assignee: {assignee} | Priority: {priority}"
        + (f" | Parent/Epic: {parent}" if parent else "")
        + f"\nLabels: {', '.join(fields.get('labels', [])) or 'none'} | Updated: {fields.get('updated', '')}\n\n"
        f"{description}"
    )
    # Knowledge-graph assertions (persisted by the ingest pipeline with this
    # document as evidence): the ticket, its project, and its epic/parent.
    key = issue["key"]
    project_key = key.split("-", 1)[0]
    graph: dict[str, list] = {
        "entities": [
            (f"ticket:{key.lower()}", key, "ticket"),
            (f"project:{project_key.lower()}", project_key, "project"),
        ],
        "aliases": [],
        "edges": [
            (f"ticket:{key.lower()}", "part_of", f"project:{project_key.lower()}",
             str(fields.get("summary", ""))[:80]),
        ],
    }
    if parent:
        graph["entities"].append((f"ticket:{parent.lower()}", parent, "ticket"))
        graph["edges"].append(
            (f"ticket:{key.lower()}", "part_of", f"ticket:{parent.lower()}", "epic/parent")
        )
    return Document(
        uri=f"{base_url}/browse/{issue['key']}",
        title=f"{issue['key']}: {fields.get('summary', '')}",
        text=text,
        kind="ticket",
        updated_at=fields.get("updated"),
        metadata={"graph": graph},
    )


def _adf_to_text(node: dict[str, Any]) -> str:
    """Flatten Atlassian Document Format to plain text."""
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text":
                parts.append(n.get("text", ""))
            for child in n.get("content", []) or []:
                walk(child)
            if n.get("type") in {"paragraph", "heading", "listItem"}:
                parts.append("\n")
        elif isinstance(n, list):
            for child in n:
                walk(child)

    walk(node)
    return "".join(parts).strip()


@register
class JiraConnector(Connector):
    type_name = "jira"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _base(self) -> str:
        return str(self.options.get("base_url", "")).rstrip("/")

    def _auth(self) -> tuple[str, str] | None:
        email = self.options.get("email", "")
        token = resolve_secret(self.options, "api_token", "JIRA_API_TOKEN")
        return (str(email), token) if email and token else None

    def _jql(self, state: dict[str, str]) -> str:
        clauses = []
        projects = self.options.get("projects") or []
        if projects:
            keys = ", ".join(f'"{p}"' for p in projects)
            clauses.append(f"project in ({keys})")
        if self.options.get("jql"):
            clauses.append(f"({self.options['jql']})")
        since = state.get("since")
        if since:
            clauses.append(f'updated >= "{since[:16].replace("T", " ")}"')
        return (" AND ".join(clauses) or "updated >= -90d") + " ORDER BY updated DESC"

    def test(self) -> ConnectionStatus:
        if not self._base():
            return ConnectionStatus(False, "No 'base_url' configured")
        try:
            me = get_json(f"{self._base()}/rest/api/2/myself", auth=self._auth())
            return ConnectionStatus(True, f"Authenticated as {me.get('displayName', '?')}")
        except Exception as exc:
            return ConnectionStatus(False, f"Jira API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        base, auth = self._base(), self._auth()
        jql = self._jql(state)
        start = 0
        self._stage("issues")
        while start < MAX_ISSUES:
            self._checkpoint()  # stop/pause between page fetches
            data = get_json(
                f"{base}/rest/api/2/search",
                auth=auth,
                params={"jql": jql, "startAt": start, "maxResults": PAGE_SIZE, "fields": FIELDS},
            )
            issues = data.get("issues", [])
            # Jira's search returns the matching total, so this % is accurate (capped at
            # MAX_ISSUES, the ceiling we actually ingest).
            total = min(data.get("total", 0) or 0, MAX_ISSUES)
            for issue in issues:
                yield issue_document(base, issue)
            start += len(issues)
            self._stage("issues", min(start, total), total)
            if start >= data.get("total", 0) or not issues:
                break

    def tools(self) -> list[AgentTool]:
        base, auth = self._base(), self._auth()

        def jira_search(jql: str, max_results: int = 10) -> str:
            data = get_json(
                f"{base}/rest/api/2/search",
                auth=auth,
                params={"jql": jql, "maxResults": max_results, "fields": "summary,status,assignee,updated"},
            )
            issues = data.get("issues", [])
            if not issues:
                return "No issues match this JQL."
            lines = []
            for i in issues:
                f = i.get("fields", {})
                lines.append(
                    f"- {i['key']}: {f.get('summary', '')} "
                    f"[{(f.get('status') or {}).get('name', '?')}, "
                    f"{((f.get('assignee') or {}).get('displayName')) or 'unassigned'}]"
                )
            return "\n".join(lines)

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"jira_search_{self.name}",
                    description="Live-search Jira with a JQL query (current state, not memory). "
                    "Example: 'project = PAY AND status != Done ORDER BY updated DESC'",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "jql": {"type": "string"},
                            "max_results": {"type": "integer"},
                        },
                        "required": ["jql"],
                    },
                ),
                fn=jira_search,
            )
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        issue = payload.get("issue")
        if issue:
            yield issue_document(self._base() or "jira://", issue)
