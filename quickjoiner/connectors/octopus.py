"""Octopus Deploy connector: projects, environments, releases, and the deployment
dashboard via the REST API, plus a live deployment-status tool.

Push mode: create an Octopus Subscription (webhook) pointing at POST /hooks/<source>
with the generic X-QJ-Signature scheme (Octopus can't sign, so front it with a proxy
that adds the header, or use a secret URL path via the source name).
"""

from __future__ import annotations

from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.llm.base import AgentTool, ToolSpec

TAKE = 100


def project_document(server: str, project: dict[str, Any]) -> Document:
    slug = project.get("Slug", project.get("Id", ""))
    text = (
        f"Octopus project: {project.get('Name', '?')}\n"
        f"Slug: {slug} | Lifecycle: {project.get('LifecycleId', '?')}\n\n"
        f"{project.get('Description') or '(no description)'}"
    )
    name = project.get("Name", "?")
    return Document(
        uri=f"{server}/app#/projects/{slug}",
        title=f"Octopus project: {name}",
        text=text,
        kind="deployment",
        metadata={"graph": {
            "entities": [(f"service:{name.lower()}", name, "service")],
            "aliases": [], "edges": [],
        }},
    )


def releases_document(server: str, project: dict[str, Any], releases: list[dict[str, Any]]) -> Document:
    lines = []
    for r in releases:
        notes = (r.get("ReleaseNotes") or "").strip().splitlines()
        lines.append(
            f"- {r.get('Version', '?')} (assembled {r.get('Assembled', '?')[:10]})"
            + (f": {notes[0][:120]}" if notes else "")
        )
    slug = project.get("Slug", project.get("Id", ""))
    return Document(
        uri=f"{server}/app#/projects/{slug}/releases",
        title=f"Octopus releases: {project.get('Name', '?')}",
        text=f"Recent releases of {project.get('Name', '?')}:\n" + "\n".join(lines),
        kind="deployment",
    )


def dashboard_document(
    server: str,
    items: list[dict[str, Any]],
    projects: dict[str, str],
    environments: dict[str, str],
) -> Document:
    lines = [
        f"- {projects.get(i.get('ProjectId'), i.get('ProjectId', '?'))} in "
        f"{environments.get(i.get('EnvironmentId'), i.get('EnvironmentId', '?'))}: "
        f"{i.get('ReleaseVersion', '?')} — {i.get('State', '?')}"
        f" ({i.get('CompletedTime') or i.get('QueueTime') or ''})"
        for i in items
    ]
    # Graph: each service deploys to each environment it currently sits in.
    entities: dict[str, tuple[str, str, str]] = {}
    edges: list[tuple[str, str, str, str]] = []
    for i in items:
        pname = projects.get(i.get("ProjectId"), "")
        ename = environments.get(i.get("EnvironmentId"), "")
        if not pname or not ename:
            continue
        sid, eid = f"service:{pname.lower()}", f"environment:{ename.lower()}"
        entities.setdefault(sid, (sid, pname, "service"))
        entities.setdefault(eid, (eid, ename, "environment"))
        edges.append((sid, "deploys", eid,
                      f"{i.get('ReleaseVersion', '?')} — {i.get('State', '?')}"))
    return Document(
        uri=f"{server}/app#/dashboard",
        title="Octopus deployment dashboard: current state per project/environment",
        text="Latest deployment per project and environment:\n" + "\n".join(lines),
        kind="deployment",
        metadata={"graph": {"entities": sorted(entities.values()), "aliases": [],
                            "edges": edges}},
    )


def event_document(server: str, event: dict[str, Any]) -> Document:
    occurred = event.get("Occurred", "")
    return Document(
        uri=f"{server}/app#/tasks",
        title=f"Octopus event: {event.get('Category', 'event')} ({occurred[:16]})",
        text=(
            f"{event.get('Category', 'Event')} at {occurred}\n"
            f"{event.get('Message', '')}\n"
            f"Related: {', '.join(event.get('RelatedDocumentIds') or []) or 'n/a'}"
        ),
        kind="deployment",
        updated_at=occurred or None,
    )


@register
class OctopusConnector(Connector):
    type_name = "octopus"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _server(self) -> str:
        return str(self.options.get("server_url", "")).rstrip("/")

    def _space(self) -> str:
        return str(self.options.get("space_id", "Spaces-1"))

    def _headers(self) -> dict[str, str]:
        key = resolve_secret(self.options, "api_key", "OCTOPUS_API_KEY")
        return {"X-Octopus-ApiKey": key} if key else {}

    def _api(self) -> str:
        return f"{self._server()}/api/{self._space()}"

    def test(self) -> ConnectionStatus:
        if not self._server():
            return ConnectionStatus(False, "No 'server_url' configured")
        try:
            data = get_json(f"{self._server()}/api", headers=self._headers())
            return ConnectionStatus(True, f"Octopus {data.get('Version', '?')} reachable")
        except Exception as exc:
            return ConnectionStatus(False, f"Octopus API error: {exc}")

    def _name_map(self, endpoint: str) -> dict[str, str]:
        items = get_json(
            f"{self._api()}/{endpoint}", headers=self._headers(), params={"take": TAKE}
        ).get("Items", [])
        return {i["Id"]: i.get("Name", i["Id"]) for i in items}

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        server, headers = self._server(), self._headers()
        projects = get_json(
            f"{self._api()}/projects", headers=headers, params={"take": TAKE}
        ).get("Items", [])
        for project in projects:
            yield project_document(server, project)
            releases = get_json(
                f"{self._api()}/projects/{project['Id']}/releases",
                headers=headers,
                params={"take": 20},
            ).get("Items", [])
            if releases:
                yield releases_document(server, project, releases)

        dashboard = get_json(f"{self._api()}/dashboard", headers=headers)
        items = dashboard.get("Items", [])
        if items:
            project_names = {p["Id"]: p.get("Name", p["Id"]) for p in projects}
            env_names = self._name_map("environments")
            yield dashboard_document(server, items, project_names, env_names)

    def tools(self) -> list[AgentTool]:
        def octopus_deployment_status() -> str:
            dashboard = get_json(f"{self._api()}/dashboard", headers=self._headers())
            items = dashboard.get("Items", [])
            if not items:
                return "No deployments on the dashboard."
            project_names = self._name_map("projects")
            env_names = self._name_map("environments")
            return dashboard_document(self._server(), items, project_names, env_names).text

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"octopus_deployment_status_{self.name}",
                    description="Live view of the Octopus dashboard: latest deployment state per project/environment.",
                    input_schema={"type": "object", "properties": {}},
                ),
                fn=octopus_deployment_status,
            )
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        # Octopus subscription webhooks wrap the event: {"Payload": {"Event": {...}}}
        event = (payload.get("Payload") or {}).get("Event") or payload.get("Event")
        if event:
            yield event_document(self._server() or "octopus://server", event)
