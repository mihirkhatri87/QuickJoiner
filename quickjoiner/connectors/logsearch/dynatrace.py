"""Dynatrace connector: problems/dashboards inventory + live log and problem queries.

Push mode: a Dynatrace problem-notification webhook posting its JSON payload to
POST /hooks/<source> (add the X-QJ-Signature header in the notification config)."""

from __future__ import annotations

from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec


def problem_document(base_url: str, problem: dict[str, Any]) -> Document:
    entities = ", ".join(
        e.get("name", "?") for e in problem.get("affectedEntities", [])
    ) or "n/a"
    return Document(
        uri=f"{base_url}/#problems/problemdetails;pid={problem.get('problemId', '')}",
        title=f"Dynatrace problem: {problem.get('title', '?')}",
        text=(
            f"Problem {problem.get('displayId', '?')}: {problem.get('title', '?')}\n"
            f"Status: {problem.get('status', '?')} | Severity: {problem.get('severityLevel', '?')} | "
            f"Impact: {problem.get('impactLevel', '?')}\n"
            f"Affected: {entities}"
        ),
        kind="dashboard",
    )


def dashboards_document(base_url: str, dashboards: list[dict[str, Any]]) -> Document:
    lines = [
        f"- {d.get('name', '?')} (owner: {d.get('owner', '?')})" for d in dashboards
    ]
    return Document(
        uri=f"{base_url}/#dashboards",
        title="Dynatrace dashboards inventory",
        text="Dashboards in Dynatrace:\n" + "\n".join(lines),
        kind="dashboard",
    )


def notification_document(base_url: str, payload: dict[str, Any]) -> Document:
    title = payload.get("ProblemTitle") or payload.get("title") or "Dynatrace notification"
    return Document(
        uri=payload.get("ProblemURL") or f"{base_url}/#problems",
        title=f"Dynatrace problem: {title}",
        text=(
            f"{title}\n"
            f"State: {payload.get('State', '?')} | Impact: {payload.get('ProblemImpact', '?')} | "
            f"Severity: {payload.get('ProblemSeverity', '?')}\n\n"
            f"{payload.get('ProblemDetailsText') or ''}"
        ),
        kind="dashboard",
    )


@register
class DynatraceConnector(Connector):
    type_name = "dynatrace"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _base(self) -> str:
        return str(self.options.get("base_url", "")).rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = resolve_secret(self.options, "token", "DYNATRACE_TOKEN")
        return {"Authorization": f"Api-Token {token}"} if token else {}

    def test(self) -> ConnectionStatus:
        if not self._base():
            return ConnectionStatus(False, "No 'base_url' configured (https://<env>.live.dynatrace.com)")
        try:
            get_json(
                f"{self._base()}/api/v2/problems",
                headers=self._headers(),
                params={"pageSize": 1},
            )
            return ConnectionStatus(True, "Dynatrace API reachable")
        except Exception as exc:
            return ConnectionStatus(False, f"Dynatrace API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        base, headers = self._base(), self._headers()
        problems = get_json(
            f"{base}/api/v2/problems", headers=headers, params={"pageSize": 50}
        ).get("problems", [])
        for problem in problems:
            yield problem_document(base, problem)
        try:
            dashboards = get_json(f"{base}/api/config/v1/dashboards", headers=headers).get(
                "dashboards", []
            )
        except Exception:
            dashboards = []  # config API needs a separate scope; best-effort
        if dashboards:
            yield dashboards_document(base, dashboards)

    def tools(self) -> list[AgentTool]:
        base, headers = self._base(), self._headers()

        def dynatrace_search_logs(query: str, minutes: int = 60, limit: int = 20) -> str:
            data = get_json(
                f"{base}/api/v2/logs/search",
                headers=headers,
                params={"query": query, "from": f"now-{minutes}m", "limit": limit},
            )
            results = data.get("results", [])
            if not results:
                return "No logs match."
            return "\n".join(
                f"[{r.get('timestamp', '')}] {str(r.get('content', ''))[:300]}" for r in results
            )

        def dynatrace_open_problems() -> str:
            problems = get_json(
                f"{base}/api/v2/problems",
                headers=headers,
                params={"pageSize": 20, "problemSelector": 'status("OPEN")'},
            ).get("problems", [])
            if not problems:
                return "No open problems."
            return "\n".join(
                f"- {p.get('displayId', '?')}: {p.get('title', '?')} ({p.get('severityLevel', '?')})"
                for p in problems
            )

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"dynatrace_search_logs_{self.name}",
                    description="Live Dynatrace log search (Dynatrace log query syntax).",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "minutes": {"type": "integer", "description": "How far back from NOW, in minutes. For a phrase like 'last Friday' or 'this weekend', call resolve_dates first and use the minutes it reports — do not estimate."},
                            "limit": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                ),
                fn=dynatrace_search_logs,
            ),
            AgentTool(
                spec=ToolSpec(
                    name=f"dynatrace_open_problems_{self.name}",
                    description="Live list of currently open Dynatrace problems.",
                    input_schema={"type": "object", "properties": {}},
                ),
                fn=dynatrace_open_problems,
            ),
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        if payload:
            yield notification_document(self._base() or "dynatrace://", payload)
