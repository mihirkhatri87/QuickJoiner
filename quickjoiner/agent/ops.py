"""Operational agent tools — the agent-tool bridge.

The web UI's slash commands (/scrape, /connect, sync buttons) are the
deterministic fast path; these tools expose the SAME service functions to the
agent so plain language works too: "scrape example.com and tell me what it is",
"connect our wiki", "sync the handbook source". Dispatch differs, operations
don't.

Safety model (enforced by tool descriptions + prompts.py guidance):
- scrape_website: only URLs the user explicitly gave; the crawl is polite and
  bounded, pages are NOT ingested into memory — the tool returns excerpts for
  the agent to synthesize from, and saves the deterministic report to
  <workspace>/scrapes/ like /scrape does.
- add_connector: a persistent configuration change — the agent must state the
  plan and get the user's explicit yes in conversation BEFORE calling it. A
  failed connection test does not save.
- sync_source: re-pulls an already-configured source; safe to run when asked.
"""

from __future__ import annotations

from datetime import datetime, timezone

from quickjoiner.config import SourceConfig
from quickjoiner.llm.base import AgentTool, ToolSpec

_EXCERPT_BUDGET = 12_000
_PER_PAGE = 1_200


def build_ops_tools(ctx) -> list[AgentTool]:
    def scrape_website(url: str, depth: int = 2, max_pages: int = 20) -> str:
        from quickjoiner.agent.scrape_report import build_report, save_report
        from quickjoiner.connectors.registry import create_connector

        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            return "ERROR: provide an http(s):// URL. Only scrape URLs the user explicitly gave."
        depth = max(0, min(int(depth), 6))
        max_pages = max(1, min(int(max_pages), 60))
        source = SourceConfig(name="agent-scrape", type="web_scrape",
                              options={"start_urls": url, "max_depth": depth,
                                       "max_pages": max_pages})
        connector = create_connector(source, ctx.workspace)
        pages = list(connector.sync({}))
        if not pages:
            return ("NO_RESULTS: no readable pages found — the site may block automated "
                    "access or have no textual content. Report this to the user honestly.")
        # Deterministic report (digest + site map) for the artifact trail; the
        # agent itself is the synthesizer, so hand it raw page excerpts.
        path = save_report(ctx.workspace, url, build_report(url, pages, provider=None))
        blocks, used = [], 0
        for doc in pages:
            take = doc.text[:_PER_PAGE]
            blocks.append(f"[{doc.title}] ({doc.uri})\n{take}")
            used += len(take)
            if used >= _EXCERPT_BUDGET:
                break
        return (
            f"Crawled {len(pages)} readable pages from {url} (depth {depth}). "
            f"Report saved to {path}.\n"
            f"Page excerpts ({len(blocks)} of {len(pages)} shown) — cite pages as [title]:\n\n"
            + "\n\n---\n\n".join(blocks)
            + "\n\nNothing was ingested into memory. If the user wants this kept, use the "
              "remember tool for specific facts, or suggest /scrape + Learn for the full report."
        )

    def list_connector_types() -> str:
        from quickjoiner.connectors.specs import connector_catalog

        lines = ["Available connector types (fields marked * are required; secrets accept "
                 "env indirection like env:MY_TOKEN):"]
        for spec in connector_catalog():
            fields = ", ".join(
                f"{f['key']}{'*' if f['required'] else ''}{' (secret)' if f['secret'] else ''}"
                for f in spec["fields"]
            )
            lines.append(f"- {spec['type']} ({spec['label']}; modes: {'/'.join(spec['modes'])})"
                         + (f" — options: {fields}" if fields else ""))
        return "\n".join(lines)

    def add_connector(name: str, type: str, options: dict | None = None,
                      shared: bool = True, sync_now: bool = False) -> str:
        from quickjoiner.connectors.registry import create_connector

        if any(s.name == name for s in ctx.config.sources):
            return f"ERROR: a connector named {name!r} already exists. Pick another name."
        source = SourceConfig(name=name, type=type, options=options or {}, shared=shared)
        try:
            connector = create_connector(source, ctx.workspace)
        except ValueError as exc:  # unknown type
            return f"ERROR: {exc}. Call list_connector_types for valid types and fields."
        result = connector.test()
        if not result.ok:
            return (f"NOT SAVED — the connection test failed: {result.message}\n"
                    "Fix the options and try again, or tell the user they can save it "
                    "anyway from Settings or the /connect wizard.")
        ctx.config.sources = [s for s in ctx.config.sources if s.name != name] + [source]
        ctx.catalog.save_config(ctx.config)
        out = f"Connector {name!r} ({type}) saved — test: {result.message}"
        if sync_now:
            out += "\n" + sync_source(name)
        return out

    def sync_source(name: str, clean: bool = False) -> str:
        from quickjoiner.connectors.registry import create_connector

        source = next((s for s in ctx.config.sources if s.name == name), None)
        if source is None:
            known = ", ".join(s.name for s in ctx.config.sources) or "none configured"
            return f"ERROR: no configured source {name!r}. Known sources: {known}."
        connector = create_connector(source, ctx.workspace)
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        if clean:  # purge docs/vectors/graph first for a from-scratch, non-corrupting resync
            ctx.catalog.delete_documents_for_source(connector.source_id)
            ctx.store.delete_source(connector.source_id)
            ctx.catalog.gc_orphan_entities()
            ctx.catalog.clear_sync_state(connector.source_id)
        state = {} if clean else ctx.catalog.get_sync_state(connector.source_id)
        started = datetime.now(timezone.utc).isoformat()
        stats = ctx.pipeline.ingest(connector.sync(state), connector.source_id)
        ctx.catalog.set_sync_state_many(connector.source_id, state)
        ctx.catalog.set_sync_state(connector.source_id, "since", started)
        errors = f" Errors: {'; '.join(stats.errors[:3])}" if stats.errors else ""
        from quickjoiner.agent.repo_docs import maybe_autogenerate

        brief_path = maybe_autogenerate(ctx, source)
        brief = f" Generated an architecture brief: {brief_path}." if brief_path else ""
        return f"Sync of {name!r} finished: {stats.summary()}.{errors}{brief}"

    return [
        AgentTool(
            spec=ToolSpec(
                name="scrape_website",
                description=(
                    "Politely crawl a website the USER EXPLICITLY ASKED YOU to look at "
                    "(bounded depth/pages, robots.txt respected) and return page excerpts "
                    "to synthesize from, citing [page title]. Does NOT ingest into memory. "
                    "Never scrape URLs the user did not provide."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "http(s) URL the user gave"},
                        "depth": {"type": "integer", "description": "Link hops from the start URL (default 2, max 6)"},
                        "max_pages": {"type": "integer", "description": "Page budget (default 20, max 60)"},
                    },
                    "required": ["url"],
                },
            ),
            fn=scrape_website,
        ),
        AgentTool(
            spec=ToolSpec(
                name="list_connector_types",
                description=(
                    "List the connector types QuickJoiner can set up and each type's option "
                    "fields. Call this before proposing an add_connector plan."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            fn=list_connector_types,
        ),
        AgentTool(
            spec=ToolSpec(
                name="add_connector",
                description=(
                    "Create a persistent connector (a configuration change). ONLY call this "
                    "after you have stated the exact name, type, and options in conversation "
                    "and the user explicitly confirmed. Secrets must use env indirection "
                    "(env:VAR_NAME) — never literal tokens. Runs the connection test first; "
                    "a failing test is not saved."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short connector name, e.g. team-wiki"},
                        "type": {"type": "string", "description": "Connector type from list_connector_types"},
                        "options": {"type": "object", "description": "Type-specific options"},
                        "shared": {"type": "boolean", "description": "Visible to all users (default true)"},
                        "sync_now": {"type": "boolean", "description": "Run the first sync immediately"},
                    },
                    "required": ["name", "type"],
                },
            ),
            fn=add_connector,
        ),
        AgentTool(
            spec=ToolSpec(
                name="sync_source",
                description=(
                    "Run an incremental sync of an already-configured source by name, "
                    "ingesting new/changed documents into memory. Safe when the user asks "
                    "for fresh data."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Configured source name"},
                    },
                    "required": ["name"],
                },
            ),
            fn=sync_source,
        ),
    ]
