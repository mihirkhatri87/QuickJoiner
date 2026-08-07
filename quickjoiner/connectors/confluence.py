"""Confluence connector: wiki pages per space via the REST API, live CQL search,
and page-event webhooks (payloads without a body are re-fetched by id)."""

from __future__ import annotations

import concurrent.futures
from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register
from quickjoiner.connectors.util import get_json, resolve_secret
from quickjoiner.ingest.extract import render_html_table
from quickjoiner.llm.base import AgentTool, ToolSpec

PAGE_SIZE = 50
MAX_PAGES_PER_SPACE = 2000


def page_document(base_url: str, page: dict[str, Any]) -> Document:
    from bs4 import BeautifulSoup

    body = page.get("body", {}) or {}
    # Prefer the SERVER-RENDERED `view` body over raw `storage`. In storage format a
    # user-mention is an empty element (`<ac:link><ri:user account-id="…"/></ac:link>`)
    # carrying only an account id, and many macros are `<ac:structured-macro>` shells —
    # BeautifulSoup's get_text() drops them, so a page that lists its team via @mentions
    # ingested with the names BLANK (observed: a "Team Charter" whose Members table came
    # through as roles with no people). `body.view` renders mentions to their display
    # name, expands macros, and renders tables, so get_text captures them. Fall back to
    # storage when view is absent (e.g. a webhook payload that only carries storage).
    html = (body.get("view", {}) or {}).get("value") or (body.get("storage", {}) or {}).get("value", "")
    soup = BeautifulSoup(html, "html.parser")
    # Render tables as markdown pipe rows before flattening, for the reason in
    # ingest/extract.py: get_text() puts every cell on its own line, so a Members table
    # arrives as a vertical stream of roles and names with nothing tying one to the other
    # — which is why a charter's team roster produced no person→team edges and retrieved
    # as an unreadable column of orphaned cells. Leaf tables only; a wrapper is layout.
    for table in soup.find_all("table"):
        if table.find("table") is None:
            table.replace_with("\n" + render_html_table(table) + "\n")
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    link = page.get("_links", {}).get("webui", "")
    updated = page.get("version", {}).get("when")
    return Document(
        uri=f"{base_url}{link}" if link else f"{base_url}/pages/{page.get('id')}",
        title=page.get("title", "Untitled page"),
        text=f"Confluence page: {page.get('title', '')}\n\n{text}",
        kind="doc",
        updated_at=updated,
    )


@register
class ConfluenceConnector(Connector):
    type_name = "confluence"
    modes = Mode.PULL | Mode.PUSH | Mode.LIVE

    def _base(self) -> str:
        base = str(self.options.get("base_url", "")).rstrip("/")
        # Atlassian cloud wikis live under /wiki
        if base and "atlassian.net" in base and not base.endswith("/wiki"):
            base += "/wiki"
        return base

    def _auth(self) -> tuple[str, str] | None:
        email = self.options.get("email", "")
        token = resolve_secret(self.options, "api_token", "CONFLUENCE_API_TOKEN") or resolve_secret(
            self.options, "api_token", "JIRA_API_TOKEN"
        )
        return (str(email), token) if email and token else None

    def test(self) -> ConnectionStatus:
        if not self._base():
            return ConnectionStatus(False, "No 'base_url' configured")
        try:
            get_json(f"{self._base()}/rest/api/space", auth=self._auth(), params={"limit": 1})
            return ConnectionStatus(True, "Confluence reachable")
        except Exception as exc:
            return ConnectionStatus(False, f"Confluence API error: {exc}")

    def _space_page_count(self, base: str, auth, space: str | None) -> int | None:
        """Best-effort up-front page count for a space, so the sync can report an accurate
        %. The content-listing endpoints give no total; the CQL **search** endpoint
        (`/rest/api/search`, distinct from `/rest/api/content/search`) is the one that
        returns `totalSize`, for one cheap `limit=1` call. Returns None — meaning "no %,
        show the shimmer" — when the pull isn't scoped to a single space or the count
        fails; ingestion never depends on it."""
        if not space:
            return None
        try:
            data = get_json(
                f"{base}/rest/api/search", auth=auth,
                params={"cql": f'type=page and space="{space}"', "limit": 1},
            )
            total = int(data.get("totalSize") or 0)
            return min(total, MAX_PAGES_PER_SPACE) if total > 0 else None
        except Exception:
            return None

    def _fetch_page_batch(self, base: str, auth, space: str | None, start: int) -> dict:
        params: dict[str, Any] = {
            # body.view (server-rendered) not body.storage — renders user-mentions to
            # display names, expands macros, and renders tables (see page_document).
            "type": "page",
            "expand": "body.view,version",
            "limit": PAGE_SIZE,
            "start": start,
        }
        if space:
            params["spaceKey"] = space
        return get_json(f"{base}/rest/api/content", auth=auth, params=params)

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        base, auth = self._base(), self._auth()
        spaces = self.options.get("spaces") or [None]
        for space in spaces:
            label = f"pages · {space}" if space else "pages"
            total = self._space_page_count(base, auth, space)  # None ⇒ shimmer, not a bar
            self._stage(label, 0 if total is not None else None, total)
            # Prefetch the NEXT batch on a worker thread while the pipeline parses+embeds the
            # current one — the network round-trip (heavy, `body.storage` bodies) overlaps the
            # downstream work instead of being a serial gap between batches. next_start is always
            # derived from the actual result count (never guessed), so pagination stays correct.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                start = 0
                future: concurrent.futures.Future | None = pool.submit(
                    self._fetch_page_batch, base, auth, space, start
                )
                while future is not None and start < MAX_PAGES_PER_SPACE:
                    self._checkpoint()  # stop/pause between batches
                    results = future.result().get("results", [])
                    next_start = start + len(results)
                    more = len(results) == PAGE_SIZE and next_start < MAX_PAGES_PER_SPACE
                    future = (
                        pool.submit(self._fetch_page_batch, base, auth, space, next_start)
                        if more else None
                    )
                    for page in results:
                        yield page_document(base, page)
                    start = next_start
                    if total is not None:
                        # Clamp: a space can grow between the preflight count and now.
                        self._stage(label, min(start, total), total)

    def tools(self) -> list[AgentTool]:
        base, auth = self._base(), self._auth()
        spaces = self.options.get("spaces") or []

        def confluence_search(query: str, max_results: int = 10) -> str:
            cql = f'text ~ "{query}"'
            if spaces:
                keys = ", ".join(f'"{s}"' for s in spaces)
                cql += f" and space in ({keys})"
            data = get_json(
                f"{base}/rest/api/content/search",
                auth=auth,
                params={"cql": cql, "limit": max_results},
            )
            results = data.get("results", [])
            if not results:
                return "No Confluence pages match."
            return "\n".join(
                f"- {r.get('title', '?')} ({base}{r.get('_links', {}).get('webui', '')})"
                for r in results
            )

        return [
            AgentTool(
                spec=ToolSpec(
                    name=f"confluence_search_{self.name}",
                    description="Live-search Confluence pages by keyword (current content, not memory)."
                    + (f" Scoped to spaces: {', '.join(map(str, spaces))}." if spaces else ""),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "max_results": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                ),
                fn=confluence_search,
            )
        ]

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        page = payload.get("page") or payload.get("content") or {}
        if not page and payload.get("id") and payload.get("title"):
            page = payload  # some webhook shapes put the page at the top level
        if not page:
            return
        body = page.get("body", {}) or {}
        if (body.get("view", {}) or {}).get("value") or (body.get("storage", {}) or {}).get("value"):
            yield page_document(self._base(), page)  # page_document prefers view, falls back to storage
            return
        # Confluence webhooks usually omit the body; re-fetch the page by id.
        page_id = page.get("id")
        if not page_id:
            return
        try:
            full = get_json(
                f"{self._base()}/rest/api/content/{page_id}",
                auth=self._auth(),
                params={"expand": "body.view,version"},
            )
        except Exception:
            return  # page deleted or no access; nothing to ingest
        yield page_document(self._base(), full)
