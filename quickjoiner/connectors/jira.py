"""Jira connector: issues, epics, and roadmap items via the REST API + live JQL tool."""

from __future__ import annotations

from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, prefetch_pages, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

PAGE_SIZE = 50
MAX_ISSUES = 1000
MAX_PARENT_DEPTH = 8  # walk-up bound fetching missing Epics — generous over Jira's normal
                      # 2-level Epic > Story/Task hierarchy (no grandparents in practice)
FIELDS = "summary,description,status,assignee,labels,issuetype,updated,priority,parent,issuelinks,resolutiondate"


def _linked_issue_keys(fields: dict[str, Any]) -> list[str]:
    """Every issue key this one links to via `issuelinks`, regardless of link type
    (Relates/Blocks/Duplicates/…) — flattened to one undirected relation, matching how
    the Azure DevOps connector treats `System.LinkTypes.Related` (a single `related_to`
    edge, not a typed taxonomy of link kinds). Each entry carries EITHER an
    `outwardIssue` or an `inwardIssue`, never both."""
    keys = []
    for link in fields.get("issuelinks", []) or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if other and other.get("key"):
            keys.append(other["key"])
    return keys


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
    # document as evidence): the ticket, its project, its epic/parent, and — new —
    # any issues it's linked to (Relates/Blocks/Duplicates/…), flattened to `related_to`
    # to match the Azure DevOps connector's Related-link handling.
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
    for other_key in _linked_issue_keys(fields):
        graph["entities"].append((f"ticket:{other_key.lower()}", other_key, "ticket"))
        graph["edges"].append(
            (f"ticket:{key.lower()}", "related_to", f"ticket:{other_key.lower()}", "linked issue")
        )
    return Document(
        uri=f"{base_url}/browse/{issue['key']}",
        title=f"{issue['key']}: {fields.get('summary', '')}",
        text=text,
        kind="ticket",
        updated_at=fields.get("updated"),
        metadata={
            "graph": graph,
            # UI-display-only, distinct from "graph" above — see the document-browser's
            # Epic/Story/Task tree (frontend/src/components/DocumentsModal.tsx), which
            # reads this rather than querying graph edges. `id`/`parent_id` are Jira KEYS
            # (strings like "PROJ-123"), not numbers — the tree component keys by
            # String(id) uniformly so it works for both this and Azure DevOps's numeric ids.
            # `team`/`sprint` are intentionally absent: Jira's board/sprint data lives in a
            # separate Agile REST API this connector doesn't speak yet.
            "display": {
                "id": key,
                "work_item_type": itype,
                "state": status,
                "sprint": "",
                "team": "",
                "changed_date": fields.get("updated", ""),
                "closed_date": fields.get("resolutiondate") or "",
                "parent_id": parent,
                "assigned_to": assignee,
                "tags": list(fields.get("labels", []) or []),
            },
        },
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
        self._stage("issues")
        # Jira's search returns the matching total, so the % is accurate (capped at MAX_ISSUES,
        # the ceiling we actually ingest). Prefetch the next offset page while parsing+embedding
        # the current one, so the network round-trip overlaps the downstream work.
        seen = 0
        state_total = {"total": 0}
        seen_keys: set[str] = set()
        parent_keys_needed: set[str] = set()

        def fetch(start):
            start = start or 0
            data = get_json(
                f"{base}/rest/api/2/search",
                auth=auth,
                params={"jql": jql, "startAt": start, "maxResults": PAGE_SIZE, "fields": FIELDS},
            )
            issues = data.get("issues", [])
            state_total["total"] = min(data.get("total", 0) or 0, MAX_ISSUES)
            next_start = start + len(issues)
            more = bool(issues) and next_start < state_total["total"]
            return issues, (next_start if more else None)

        for issues in prefetch_pages(fetch, 0, self._checkpoint):
            for issue in issues:
                seen_keys.add(issue["key"])
                yield issue_document(base, issue)
                parent_key = ((issue.get("fields") or {}).get("parent") or {}).get("key")
                if parent_key and parent_key not in seen_keys:
                    parent_keys_needed.add(parent_key)
            seen += len(issues)
            self._stage("issues", min(seen, state_total["total"]), state_total["total"])

        # Walk UP to fetch missing parent Epics: the filter above only matches issues
        # that were themselves recently updated, so an Epic that hasn't been touched
        # would otherwise never reach memory at all — the same gap the Azure DevOps
        # connector had before its own hierarchy walk-up, and a `part_of` edge with no
        # target document behind it. Batched via the same search endpoint (`key in
        # (...)`), not one call per id; bounded since Jira's real hierarchy is normally
        # just Epic > Story/Task (no grandparents), but walks further if it ever is.
        depth = 0
        while parent_keys_needed and depth < MAX_PARENT_DEPTH:
            seen_keys.update(parent_keys_needed)
            keys = list(parent_keys_needed)
            parent_keys_needed = set()
            for i in range(0, len(keys), PAGE_SIZE):
                self._checkpoint()
                chunk = keys[i : i + PAGE_SIZE]
                keys_jql = ", ".join(f'"{k}"' for k in chunk)
                data = get_json(
                    f"{base}/rest/api/2/search", auth=auth,
                    params={"jql": f"key in ({keys_jql})", "maxResults": PAGE_SIZE, "fields": FIELDS},
                )
                for issue in data.get("issues", []):
                    yield issue_document(base, issue)
                    parent_key = ((issue.get("fields") or {}).get("parent") or {}).get("key")
                    if parent_key and parent_key not in seen_keys:
                        parent_keys_needed.add(parent_key)
            depth += 1

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

        def jira_get_issue(issue_key: str) -> str:
            """Full details of one issue — description, comments, linked issues — the fields
            `jira_search` doesn't return and memory may not hold if this issue hasn't been
            (re)synced recently. Mirrors `ado_get_work_item`'s role for the Azure DevOps
            connector."""
            data = get_json(
                f"{base}/rest/api/2/issue/{issue_key}",
                auth=auth,
                params={"fields": FIELDS + ",comment"},
            )
            f = data.get("fields", {}) or {}
            description = f.get("description") or "(none)"
            if isinstance(description, dict):
                description = _adf_to_text(description)
            assignee = (f.get("assignee") or {}).get("displayName", "unassigned")
            status = (f.get("status") or {}).get("name", "?")
            itype = (f.get("issuetype") or {}).get("name", "issue")
            priority = (f.get("priority") or {}).get("name", "")
            parent = (f.get("parent") or {}).get("key")
            out = (
                f"{itype} {issue_key}: {f.get('summary', '')}\n"
                f"Status: {status} | Assignee: {assignee} | Priority: {priority}"
                + (f" | Parent/Epic: {parent}" if parent else "") + "\n"
                f"Labels: {', '.join(f.get('labels', [])) or 'none'}\n\n"
                f"Description:\n{description}\n"
            )
            link_lines = []
            for link in f.get("issuelinks", []) or []:
                other = link.get("outwardIssue") or link.get("inwardIssue")
                if not other:
                    continue
                direction = "outward" if "outwardIssue" in link else "inward"
                rel = (link.get("type") or {}).get(direction, "related to")
                link_lines.append(f"  {rel}: {other.get('key')} - {(other.get('fields') or {}).get('summary', '')}")
            if link_lines:
                out += "\nLinked issues:\n" + "\n".join(link_lines)
            comments = ((f.get("comment") or {}).get("comments")) or []
            if comments:
                out += "\n\nRecent comments:\n" + "\n".join(
                    f"- {(c.get('author') or {}).get('displayName', '?')}: "
                    f"{_adf_to_text(c['body']) if isinstance(c.get('body'), dict) else c.get('body', '')}"
                    for c in comments[-5:]
                )
            return out[:12000]

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
            ),
            AgentTool(
                spec=ToolSpec(
                    name=f"jira_get_issue_{self.name}",
                    description="Full details of ONE Jira issue by key — description, comments, "
                    "linked issues, priority. Use when memory lacks a specific key or these fields "
                    "(jira_search only returns summary/status/assignee).",
                    input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}},
                                  "required": ["issue_key"]},
                ),
                fn=jira_get_issue,
            ),
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        issue = payload.get("issue")
        if issue:
            yield issue_document(self._base() or "jira://", issue)
