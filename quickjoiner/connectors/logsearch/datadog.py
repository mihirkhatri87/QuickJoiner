"""Datadog connector: monitors/dashboards inventory + live log search.

Push mode: a Datadog webhook integration posting to POST /hooks/<source> with a
custom payload; add the X-QJ-Signature header in the webhook's custom headers."""

from __future__ import annotations

from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, post_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec


def monitor_document(site_url: str, monitor: dict[str, Any]) -> Document:
    tags = ", ".join(monitor.get("tags") or []) or "none"
    return Document(
        uri=f"{site_url}/monitors/{monitor.get('id', '')}",
        title=f"Datadog monitor: {monitor.get('name', '?')}",
        text=(
            f"Datadog monitor '{monitor.get('name', '?')}' (type: {monitor.get('type', '?')}, "
            f"state: {monitor.get('overall_state', '?')})\n"
            f"Tags: {tags}\nQuery: {monitor.get('query', '')}\n\n"
            f"{monitor.get('message') or ''}"
        ),
        kind="dashboard",
        updated_at=monitor.get("modified"),
    )


def dashboards_document(site_url: str, dashboards: list[dict[str, Any]]) -> Document:
    lines = [
        f"- {d.get('title', '?')} ({site_url}{d.get('url', '')})" for d in dashboards
    ]
    return Document(
        uri=f"{site_url}/dashboard/lists",
        title="Datadog dashboards inventory",
        text="Dashboards in Datadog:\n" + "\n".join(lines),
        kind="dashboard",
    )


def webhook_document(site_url: str, payload: dict[str, Any]) -> Document:
    title = payload.get("title") or payload.get("alert_title") or "Datadog event"
    return Document(
        uri=payload.get("link") or f"{site_url}/event/stream",
        title=f"Datadog alert: {title}",
        text=(
            f"{title}\n"
            f"Type: {payload.get('alert_type', payload.get('event_type', '?'))} | "
            f"Priority: {payload.get('priority', '?')}\n\n"
            f"{payload.get('body') or payload.get('event_msg') or ''}"
        ),
        kind="dashboard",
    )


@register
class DatadogConnector(Connector):
    type_name = "datadog"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _site(self) -> str:
        return str(self.options.get("site", "datadoghq.com"))

    def _api(self) -> str:
        return f"https://api.{self._site()}"

    def _site_url(self) -> str:
        return f"https://app.{self._site()}"

    def _headers(self) -> dict[str, str]:
        headers = {}
        api_key = resolve_secret(self.options, "api_key", "DD_API_KEY")
        app_key = resolve_secret(self.options, "app_key", "DD_APP_KEY")
        if api_key:
            headers["DD-API-KEY"] = api_key
        if app_key:
            headers["DD-APPLICATION-KEY"] = app_key
        return headers

    def test(self) -> ConnectionStatus:
        try:
            data = get_json(f"{self._api()}/api/v1/validate", headers=self._headers())
            ok = bool(data.get("valid"))
            return ConnectionStatus(ok, "API key valid" if ok else "API key rejected")
        except Exception as exc:
            return ConnectionStatus(False, f"Datadog API error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        headers, site_url = self._headers(), self._site_url()
        monitors = get_json(
            f"{self._api()}/api/v1/monitor", headers=headers, params={"page_size": 100}
        )
        for monitor in monitors:
            yield monitor_document(site_url, monitor)
        dashboards = get_json(f"{self._api()}/api/v1/dashboard", headers=headers).get(
            "dashboards", []
        )
        if dashboards:
            yield dashboards_document(site_url, dashboards)

    def tools(self) -> list[AgentTool]:
        def datadog_search_logs(query: str, minutes: int = 60, limit: int = 20) -> str:
            data = post_json(
                f"{self._api()}/api/v2/logs/events/search",
                {
                    "filter": {"query": query, "from": f"now-{minutes}m", "to": "now"},
                    "page": {"limit": limit},
                },
                headers=self._headers(),
            )
            events = data.get("data", [])
            if not events:
                return "No logs match."
            lines = []
            for e in events:
                attrs = e.get("attributes", {})
                lines.append(f"[{attrs.get('timestamp', '')}] {str(attrs.get('message', ''))[:300]}")
            return "\n".join(lines)

        def datadog_monitor_states(query: str = "") -> str:
            monitors = get_json(
                f"{self._api()}/api/v1/monitor",
                headers=self._headers(),
                params={"page_size": 50, **({"name": query} if query else {})},
            )
            if not monitors:
                return "No monitors found."
            return "\n".join(
                f"- {m.get('name')}: {m.get('overall_state', '?')}" for m in monitors
            )

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"datadog_search_logs_{self.name}",
                    description="Live Datadog log search (Datadog query syntax, e.g. 'service:payments status:error').",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "minutes": {"type": "integer"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                ),
                fn=datadog_search_logs,
            ),
            AgentTool(
                spec=ToolSpec(
                    name=f"datadog_monitor_states_{self.name}",
                    description="Live view of Datadog monitor states (optionally filtered by name).",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                ),
                fn=datadog_monitor_states,
            ),
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        if payload:
            yield webhook_document(self._site_url(), payload)
