"""Elasticsearch connector: index inventory + live search queries.

Push mode: a Watcher/Kibana alerting webhook action posting JSON to
POST /hooks/<source> with the X-QJ-Signature header configured on the action."""

from __future__ import annotations

import json
from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec


def indices_document(base_url: str, indices: list[dict[str, Any]]) -> Document:
    lines = [
        f"- {i.get('index', '?')}: {i.get('docs.count', '?')} docs, "
        f"{i.get('store.size', '?')}, health {i.get('health', '?')}"
        for i in indices
        if not str(i.get("index", "")).startswith(".")
    ]
    return Document(
        uri=f"{base_url}/_cat/indices",
        title="Elasticsearch index inventory",
        text="Indices in the Elasticsearch cluster (system indices omitted):\n" + "\n".join(lines),
        kind="dashboard",
    )


def watcher_document(base_url: str, payload: dict[str, Any]) -> Document:
    watch_id = payload.get("watch_id") or payload.get("rule", {}).get("name") or "alert"
    return Document(
        uri=f"{base_url}/_watcher",
        title=f"Elastic alert: {watch_id}",
        text=f"Elastic alert '{watch_id}' fired:\n{json.dumps(payload, indent=2)[:2000]}",
        kind="dashboard",
    )


@register
class ElasticConnector(Connector):
    type_name = "elastic"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _base(self) -> str:
        return str(self.options.get("base_url", "")).rstrip("/")

    def _headers(self) -> dict[str, str]:
        api_key = resolve_secret(self.options, "api_key", "ELASTIC_API_KEY")
        return {"Authorization": f"ApiKey {api_key}"} if api_key else {}

    def _auth(self) -> tuple[str, str] | None:
        user = self.options.get("username", "")
        password = resolve_secret(self.options, "password", "ELASTIC_PASSWORD")
        return (str(user), password) if user and password else None

    def test(self) -> ConnectionStatus:
        if not self._base():
            return ConnectionStatus(False, "No 'base_url' configured")
        try:
            info = get_json(self._base(), headers=self._headers(), auth=self._auth())
            return ConnectionStatus(
                True,
                f"Cluster '{info.get('cluster_name', '?')}' "
                f"(v{info.get('version', {}).get('number', '?')})",
            )
        except Exception as exc:
            return ConnectionStatus(False, f"Elasticsearch error: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        indices = get_json(
            f"{self._base()}/_cat/indices",
            headers=self._headers(),
            auth=self._auth(),
            params={"format": "json"},
        )
        if indices:
            yield indices_document(self._base(), indices)

    def tools(self) -> list[AgentTool]:
        def elastic_search(index: str, query: str, size: int = 10) -> str:
            data = get_json(
                f"{self._base()}/{index}/_search",
                headers=self._headers(),
                auth=self._auth(),
                params={"q": query, "size": size, "sort": "@timestamp:desc"},
            )
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                return "No documents match."
            lines = []
            for h in hits:
                src = h.get("_source", {})
                summary = src.get("message") or json.dumps(src)[:300]
                lines.append(f"[{src.get('@timestamp', h.get('_index', ''))}] {str(summary)[:300]}")
            return "\n".join(lines)

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"elastic_search_{self.name}",
                    description="Live Elasticsearch search with Lucene query syntax against an index pattern "
                    "(e.g. index='logs-*', query='level:ERROR AND service:payments').",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "index": {"type": "string"},
                            "query": {"type": "string"},
                            "size": {"type": "integer"},
                        },
                        "required": ["index", "query"],
                    },
                ),
                fn=elastic_search,
            )
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        if payload:
            yield watcher_document(self._base() or "elastic://", payload)
