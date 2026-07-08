"""Grafana connector: dashboards/datasources inventory + live Loki log queries.

Push mode: point a Grafana alert webhook contact point at POST /hooks/<source>
(generic X-QJ-Signature scheme is not supported by Grafana natively; use a
custom webhook header or shared-token proxy)."""

from __future__ import annotations

from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec


def dashboard_document(base_url: str, dash: dict[str, Any]) -> Document:
    tags = ", ".join(dash.get("tags") or []) or "none"
    folder = dash.get("folderTitle", "General")
    return Document(
        uri=f"{base_url}{dash.get('url', '')}",
        title=f"Grafana dashboard: {dash.get('title', '?')}",
        text=(
            f"Grafana dashboard '{dash.get('title', '?')}' (folder: {folder})\n"
            f"Tags: {tags} | UID: {dash.get('uid', '?')}"
        ),
        kind="dashboard",
    )


def datasources_document(base_url: str, sources: list[dict[str, Any]]) -> Document:
    lines = [
        f"- {s.get('name', '?')} (type: {s.get('type', '?')}"
        + (", default" if s.get("isDefault") else "")
        + ")"
        for s in sources
    ]
    return Document(
        uri=f"{base_url}/connections/datasources",
        title="Grafana datasources inventory",
        text="Datasources configured in Grafana:\n" + "\n".join(lines),
        kind="dashboard",
    )


def alert_document(base_url: str, payload: dict[str, Any]) -> Document:
    # Unified alerting webhook: {"alerts": [...], "status": ..., "title": ...}
    alerts = payload.get("alerts") or []
    lines = [
        f"- {a.get('labels', {}).get('alertname', '?')} [{a.get('status', '?')}]: "
        f"{a.get('annotations', {}).get('summary') or a.get('annotations', {}).get('description') or ''}"
        for a in alerts
    ]
    title = payload.get("title") or f"Grafana alert ({payload.get('status', '?')})"
    return Document(
        uri=f"{base_url}/alerting/list",
        title=f"Grafana alert: {title}",
        text=f"{title}\n" + ("\n".join(lines) or payload.get("message", "")),
        kind="dashboard",
    )


@register
class GrafanaConnector(Connector):
    type_name = "grafana"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _base(self) -> str:
        return str(self.options.get("base_url", "")).rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = resolve_secret(self.options, "token", "GRAFANA_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def test(self) -> ConnectionStatus:
        if not self._base():
            return ConnectionStatus(False, "No 'base_url' configured")
        try:
            health = get_json(f"{self._base()}/api/health", headers=self._headers())
            return ConnectionStatus(True, f"Grafana {health.get('version', '?')} reachable")
        except Exception as exc:
            return ConnectionStatus(False, f"Grafana API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        base, headers = self._base(), self._headers()
        dashboards = get_json(
            f"{base}/api/search", headers=headers, params={"type": "dash-db", "limit": 200}
        )
        for dash in dashboards:
            yield dashboard_document(base, dash)
        try:
            sources = get_json(f"{base}/api/datasources", headers=headers)
        except Exception:
            sources = []  # needs admin scope; inventory is best-effort
        if sources:
            yield datasources_document(base, sources)

    def tools(self) -> list[AgentTool]:
        base, headers = self._base(), self._headers()

        def grafana_search_dashboards(query: str) -> str:
            hits = get_json(
                f"{base}/api/search", headers=headers, params={"query": query, "limit": 20}
            )
            if not hits:
                return "No dashboards match."
            return "\n".join(f"- {h.get('title')} ({base}{h.get('url', '')})" for h in hits)

        tools = [
            AgentTool(
                spec=ToolSpec(
                    name=f"grafana_search_dashboards_{self.name}",
                    description="Live-search Grafana dashboards by title/keyword.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                ),
                fn=grafana_search_dashboards,
            )
        ]

        loki_uid = self.options.get("loki_datasource_uid")
        if loki_uid:

            def grafana_query_loki(logql: str, minutes: int = 60, limit: int = 50) -> str:
                data = get_json(
                    f"{base}/api/datasources/proxy/uid/{loki_uid}/loki/api/v1/query_range",
                    headers=headers,
                    params={"query": logql, "since": f"{minutes}m", "limit": limit},
                )
                streams = data.get("data", {}).get("result", [])
                if not streams:
                    return "No log lines match."
                lines = []
                for stream in streams[:10]:
                    for _, line in stream.get("values", [])[:10]:
                        lines.append(line[:300])
                return "\n".join(lines) or "No log lines match."

            tools.append(
                AgentTool(
                    spec=ToolSpec(
                        name=f"grafana_query_loki_{self.name}",
                        description="Live LogQL query against Loki via Grafana. "
                        'Example: \'{app="payments"} |= "error"\'',
                        input_schema={
                            "type": "object",
                            "properties": {
                                "logql": {"type": "string"},
                                "minutes": {"type": "integer"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["logql"],
                        },
                    ),
                    fn=grafana_query_loki,
                )
            )
        return tools

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        if payload.get("alerts") or payload.get("title") or payload.get("message"):
            yield alert_document(self._base() or "grafana://", payload)
