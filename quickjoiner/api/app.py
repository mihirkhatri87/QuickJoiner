"""FastAPI app: chat (SSE), sources dashboard, sync, search, webhooks."""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from quickjoiner.api.hooks import build_hooks_router
from quickjoiner.app import AppContext, build_context

STATIC_DIR = Path(__file__).parent / "static"

_SENTINEL = object()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    project: str | None = None  # project id or name; groups sessions + scopes memory
    provider: str | None = None
    model: str | None = None


class ProjectRequest(BaseModel):
    name: str
    description: str = ""


def create_app(workspace: Path) -> FastAPI:
    from quickjoiner.sessions import SessionManager

    ctx: AppContext = build_context(workspace)
    api = FastAPI(title="QuickJoiner", version="0.1.0")
    manager = SessionManager(ctx)
    api.include_router(build_hooks_router(ctx))

    @api.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @api.get("/health")
    def health():
        return {"status": "ok", "workspace": str(ctx.workspace), "org": ctx.config.org}

    @api.get("/api/status")
    def status():
        return {
            "org": ctx.config.org,
            "llm": {"provider": ctx.config.llm.provider, "model": ctx.config.llm.resolved_model()},
            "stats": ctx.catalog.stats(),
        }

    @api.get("/api/sources")
    def sources():
        configured = {s.name: s for s in ctx.config.sources}
        rows = []
        for s in ctx.catalog.list_sources():
            cfg = configured.get(s["name"])
            rows.append(
                {
                    "id": s["id"],
                    "name": s["name"],
                    "type": s["type"],
                    "documents": s["doc_count"],
                    "configured": cfg is not None,
                }
            )
        return rows

    @api.post("/api/sync/{source_name}")
    def sync_source(source_name: str):
        from quickjoiner.connectors.registry import create_connector

        source = next((s for s in ctx.config.sources if s.name == source_name), None)
        if source is None:
            raise HTTPException(status_code=404, detail=f"No configured source {source_name!r}")
        connector = create_connector(source, ctx.workspace)
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        state = ctx.catalog.get_sync_state(connector.source_id)
        started = datetime.now(timezone.utc).isoformat()
        stats = ctx.pipeline.ingest(connector.sync(state), connector.source_id)
        ctx.catalog.set_sync_state(connector.source_id, "since", started)
        return {"source": source_name, "result": stats.summary(), "errors": stats.errors[:10]}

    @api.get("/api/search")
    def search(q: str, top_k: int = 8):
        hits = ctx.store.search(q, top_k=top_k, min_score=ctx.config.retrieval.min_score)
        return [
            {"score": round(h.score, 3), "title": h.title, "uri": h.uri, "kind": h.kind,
             "text": h.text[:500]}
            for h in hits
        ]

    @api.get("/api/briefs")
    def list_briefs():
        briefs_dir = ctx.workspace / "briefs"
        files = sorted(briefs_dir.glob("*.md")) if briefs_dir.exists() else []
        return [{"name": f.stem, "path": str(f)} for f in files]

    @api.post("/api/briefs/{brief_type}")
    def make_brief(brief_type: str, provider: str | None = None, model: str | None = None):
        from quickjoiner.agent.briefs import generate_brief

        try:
            markdown, path = generate_brief(
                ctx, brief_type, provider_override=provider, model_override=model
            )
        except ValueError as exc:  # unknown brief type
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # provider/setup failures
            raise HTTPException(status_code=502, detail=str(exc))
        return {"brief": markdown, "path": str(path) if path else None, "generated": path is not None}

    @api.get("/api/projects")
    def list_projects():
        return ctx.catalog.list_projects()

    @api.post("/api/projects")
    def create_project(req: ProjectRequest):
        return manager.create_project(req.name, req.description)

    @api.get("/api/sessions")
    def list_sessions(project: str | None = None):
        project_row = manager.resolve_project(project)
        if project and not project_row:
            raise HTTPException(status_code=404, detail=f"No project {project!r}")
        return ctx.catalog.list_sessions(project_row["id"] if project_row else None)

    @api.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        session = ctx.catalog.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
        session["messages"] = [
            {k: v for k, v in m.items() if k in ("role", "content")}
            for m in json.loads(session.pop("messages_json"))
        ]
        return session

    @api.post("/api/sessions/{session_id}/distill")
    def distill_session(session_id: str):
        try:
            facts = manager.distill(session_id, provider=ctx.build_provider())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"session": session_id, "facts_learned": facts}

    @api.post("/api/chat")
    def chat(req: ChatRequest):
        """SSE stream: {type: thinking|delta|tool_call|answer|error|done, data: ...} events."""
        events: queue.Queue = queue.Queue()

        def worker():
            try:
                session = manager.open_session(req.session_id, req.project)
                history = manager.history(session)
                # Built inside the worker so provider setup errors (e.g. missing
                # ANTHROPIC_API_KEY) surface as SSE error events, not a 500.
                agent = ctx.build_agent(
                    req.provider, req.model, extra_system=manager.system_context(session)
                )
                answer, new_history = agent.ask(
                    req.message,
                    history,
                    on_event=lambda etype, detail: events.put({"type": etype, "data": detail}),
                )
                manager.record_turn(session["id"], new_history)
                manager.maybe_compress(session["id"])
                events.put({"type": "answer", "data": answer, "session_id": session["id"]})
            except Exception as exc:
                events.put({"type": "error", "data": str(exc)})
            finally:
                events.put(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        def stream():
            while True:
                item = events.get()
                if item is _SENTINEL:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                yield f"data: {json.dumps(item)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return api
