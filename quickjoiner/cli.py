"""qj — the QuickJoiner CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from quickjoiner.config import Config, SourceConfig, workspace_dir

# Windows consoles default to a legacy code page (cp1252) that can't encode the
# block/box-drawing/emoji characters an LLM may emit in a brief or answer — Rich then
# crashes at render time, *after* the command's real work (save + ingest) is done.
# Force UTF-8 on the standard streams so no command dies on a stray glyph.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # already-wrapped or non-reconfigurable stream
        pass

app = typer.Typer(
    name="qj",
    help="QuickJoiner: onboarding intelligence that learns your new org and answers only from what it learned.",
    no_args_is_help=True,
)
console = Console()

WORKSPACE_OPT = typer.Option(None, "--workspace", "-w", help="Workspace directory (default: QJ_WORKSPACE or ~/.quickjoiner/default)")
PROVIDER_OPT = typer.Option(None, "--provider", "-p", help="Override LLM provider: anthropic | ollama | litellm")
MODEL_OPT = typer.Option(None, "--model", "-m", help="Override LLM model id")


def _workspace(workspace: Optional[Path]) -> Path:
    return workspace or workspace_dir()


def _context(workspace: Optional[Path]):
    from quickjoiner.app import build_context

    ws = _workspace(workspace)
    if not ws.exists():
        console.print(f"[red]Workspace {ws} does not exist. Run [bold]qj init[/bold] first.[/red]")
        raise typer.Exit(1)
    return build_context(ws)


def _session_user(ctx) -> Optional[str]:
    """The CLI's signed-in user, from <workspace>/.session (None = signed out)."""
    from quickjoiner.auth import SESSION_FILE, Auth

    token_file = ctx.workspace / SESSION_FILE
    if not token_file.exists():
        return None
    return Auth(ctx.catalog).resolve(token_file.read_text(encoding="utf-8").strip())


def _stream_renderer():
    """Console renderer for agent events: dim thinking, live answer tokens, tool notes."""
    state = {"mode": None, "streamed": False}

    def on_event(etype: str, detail: str) -> None:
        if etype == "tool_call":
            if state["mode"] is not None:
                console.print()
            console.print(f"[dim]· using {detail}[/dim]")
            state["mode"] = None
        elif etype == "thinking":
            if state["mode"] != "thinking":
                if state["mode"] is not None:
                    console.print()
                console.print("[dim italic]thinking…[/dim italic]")
                state["mode"] = "thinking"
            console.print(detail, end="", style="dim italic", markup=False, highlight=False)
        elif etype == "delta":
            if state["mode"] == "thinking":
                console.print("\n")
            state["mode"] = "text"
            state["streamed"] = True
            console.print(detail, end="", markup=False, highlight=False)

    return on_event, state


def _export_answer(ctx, markdown_text: str, fmt: str, out: Optional[Path], title: str) -> None:
    from datetime import datetime, timezone

    from quickjoiner.export import export_markdown

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    path = out or ctx.workspace / "exports" / f"answer-{stamp}.{fmt}"
    try:
        written = export_markdown(markdown_text, fmt, path, title=title)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Exported ({fmt}):[/green] {written}")


@app.command()
def init(
    org: str = typer.Argument("default", help="Organization/workspace name"),
    provider: str = typer.Option("anthropic", help="LLM provider: anthropic | ollama | litellm"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Create a workspace for an organization."""
    from quickjoiner.config import load_env
    from quickjoiner.memory.factory import create_catalog

    ws = workspace or workspace_dir(org)
    load_env(ws)
    catalog = create_catalog(ws)  # SQLite on-prem, Postgres when DATABASE_URL is set
    fresh = catalog.get_setting("config") is None  # first init vs re-run / cloud reboot
    config = catalog.load_config()  # migrates any legacy config.yaml
    config.org = org
    if fresh:
        config.llm.provider = provider  # don't clobber a provider already tuned via the UI
    catalog.save_config(config)
    catalog.close()
    where = "created" if fresh else "ready"
    console.print(f"[green]Workspace {where}:[/green] {ws}")
    console.print(f"LLM provider: [bold]{config.llm.provider}[/bold] (model: {config.llm.resolved_model()})")
    if config.llm.provider == "anthropic":
        console.print("Set [bold]ANTHROPIC_API_KEY[/bold] in your environment or in a .env file.")
    elif config.llm.provider == "litellm":
        console.print(
            f"Point [bold]llm.base_url[/bold] at your LiteLLM proxy (e.g. http://localhost:4000) "
            f"and set [bold]{config.llm.api_key_env}[/bold] in your environment if it needs a key."
        )
    else:
        console.print(f"Make sure Ollama is running at {config.llm.base_url}.")
    console.print("Next: [bold]qj learn <path-or-url>[/bold], then [bold]qj ask \"...\"[/bold]")
    if workspace is None and org != "default":
        console.print(f"Tip: set QJ_WORKSPACE={ws} to make this workspace the default.")


@app.command()
def status(workspace: Optional[Path] = WORKSPACE_OPT):
    """Show workspace, config, and learned-memory statistics."""
    ctx = _context(workspace)
    stats = ctx.catalog.stats()
    console.print(f"Workspace: [bold]{ctx.workspace}[/bold]  (org: {ctx.config.org})")
    console.print(
        f"LLM: {ctx.config.llm.provider} / {ctx.config.llm.resolved_model()}   "
        f"Embeddings: {ctx.config.embedding.provider} / {ctx.config.embedding.resolved_model()}"
    )
    console.print(
        f"Learned: {stats['documents']} documents, {stats['chunks']} chunks, {stats['sources']} sources"
    )
    sources = ctx.catalog.list_sources()
    if sources:
        table = Table(title="Sources")
        table.add_column("id")
        table.add_column("type")
        table.add_column("documents", justify="right")
        for s in sources:
            table.add_row(s["id"], s["type"], str(s["doc_count"]))
        console.print(table)


@app.command()
def learn(
    target: str = typer.Argument(..., help="A file, folder, URL, or a free-text fact in quotes"),
    name: Optional[str] = typer.Option(None, help="Source name (defaults to the target)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Ingest a file/folder/URL into memory, or store free text as a taught note."""
    ctx = _context(workspace)
    is_url = target.startswith(("http://", "https://"))
    is_path = Path(target).exists()

    if is_url or is_path:
        from quickjoiner.connectors.files import FilesConnector

        source_name = name or (target if is_url else Path(target).name)
        options = {"url": target} if is_url else {"path": str(Path(target).resolve())}
        connector = FilesConnector(name=source_name, options=options, workspace=ctx.workspace)
        ctx.catalog.upsert_source(connector.source_id, source_name, connector.type_name, options)
        with console.status(f"Learning from {target} ..."):
            stats = ctx.pipeline.ingest(connector.sync({}), connector.source_id)
        console.print(f"[green]Done:[/green] {stats.summary()}")
        for err in stats.errors[:5]:
            console.print(f"[yellow]warn:[/yellow] {err}")
    else:
        from quickjoiner.agent.tools import build_builtin_tools

        tools = {
            t.spec.name: t
            for t in build_builtin_tools(
                ctx.store, ctx.catalog, ctx.pipeline, ctx.config.retrieval, ctx.config.gaps
            )
        }
        result = tools["remember"].run(fact=target, topic=name)
        console.print(f"[green]{result}[/green]")


@app.command()
def ask(
    question: str = typer.Argument(..., help="Your question about the organization"),
    provider: Optional[str] = PROVIDER_OPT,
    model: Optional[str] = MODEL_OPT,
    format: Optional[str] = typer.Option(None, "--format", "-f", help="Export the answer: md | html | csv | pptx"),
    out: Optional[Path] = typer.Option(None, "--out", help="Export file path (default: <workspace>/exports/)"),
    no_stream: bool = typer.Option(False, "--no-stream", help="Wait and render the answer as markdown instead of streaming"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Ask one question, answered only from learned knowledge (with citations)."""
    ctx = _context(workspace)
    agent = ctx.build_agent(provider, model, sources=ctx.visible_sources(_session_user(ctx)))
    if no_stream:
        with console.status("Thinking..."):
            answer, _ = agent.ask(question)
        console.print(Markdown(answer))
    else:
        on_event, state = _stream_renderer()
        answer, _ = agent.ask(question, on_event=on_event)
        if state["streamed"]:
            console.print()
        else:  # provider didn't stream; render normally
            console.print(Markdown(answer))
    if format:
        _export_answer(ctx, answer, format, out, title=question[:60])


@app.command()
def chat(
    provider: Optional[str] = PROVIDER_OPT,
    model: Optional[str] = MODEL_OPT,
    project: Optional[str] = typer.Option(None, "--project", help="Project name/id to chat within (scopes memory + framing)"),
    session: Optional[str] = typer.Option(None, "--session", help="Session id to continue"),
    resume: bool = typer.Option(False, "--resume", help="Continue the most recent session (of --project if given)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Interactive chat. Sessions persist; history is compressed when it grows long."""
    from quickjoiner.sessions import SessionManager

    ctx = _context(workspace)
    manager = SessionManager(ctx)
    if resume and not session:
        project_row = manager.resolve_project(project)
        rows = ctx.catalog.list_sessions(project_row["id"] if project_row else None)
        session = rows[0]["id"] if rows else None
    sess = manager.open_session(session, project)
    history = manager.history(sess)

    llm = ctx.config.llm
    where = f" · project: {project}" if sess.get("project_id") else ""
    console.print(
        f"[bold]QuickJoiner chat[/bold] ({provider or llm.provider}){where} · "
        f"session {sess['id'][:8]}{' (resumed, ' + str(len(history)) + ' msgs)' if history else ''}. "
        "Type 'exit' to quit.\n"
    )
    my_sources = ctx.visible_sources(_session_user(ctx))
    agent = ctx.build_agent(provider, model, extra_system=manager.system_context(sess),
                            sources=my_sources)
    while True:
        try:
            question = console.input("[bold cyan]you>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            break
        on_event, state = _stream_renderer()
        answer, history = agent.ask(question, history, on_event=on_event)
        if state["streamed"]:
            console.print()
        else:
            console.print(Markdown(answer))
        console.print()
        manager.record_turn(sess["id"], history)
        if manager.maybe_compress(sess["id"]):
            sess = ctx.catalog.get_session(sess["id"])
            history = manager.history(sess)
            agent = ctx.build_agent(provider, model, extra_system=manager.system_context(sess),
                                    sources=my_sources)
            console.print("[dim]· compressed older turns into the session summary[/dim]\n")
    console.print(f"[dim]session saved: {sess['id']} (qj chat --session {sess['id'][:8]}… to continue)[/dim]")


projects_app = typer.Typer(help="Projects: purposeful groups of conversations with shared memory framing.")
app.add_typer(projects_app, name="projects")


@projects_app.command("create")
def projects_create(
    name: str = typer.Argument(..., help="Project name"),
    description: str = typer.Option("", "--description", "-d", help="What this project is about (injected into chats)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Create (or update) a project."""
    from quickjoiner.sessions import SessionManager

    ctx = _context(workspace)
    row = SessionManager(ctx).create_project(name, description)
    console.print(f"[green]Project ready:[/green] {row['id']} — chat in it with: qj chat --project {row['id']}")


@projects_app.command("list")
def projects_list(workspace: Optional[Path] = WORKSPACE_OPT):
    """List projects and their session counts."""
    ctx = _context(workspace)
    rows = ctx.catalog.list_projects()
    if not rows:
        console.print("No projects yet. Create one: qj projects create <name> -d \"...\"")
        return
    for p in rows:
        console.print(f"- [bold]{p['id']}[/bold]: {p['name']} ({p['session_count']} sessions) {p['description']}")


sessions_app = typer.Typer(help="Persistent chat sessions: list, inspect, distill into memory.")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list(
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """List sessions, most recent first."""
    from quickjoiner.sessions import SessionManager

    ctx = _context(workspace)
    project_row = SessionManager(ctx).resolve_project(project)
    rows = ctx.catalog.list_sessions(project_row["id"] if project_row else None)
    if not rows:
        console.print("No sessions yet.")
        return
    for s in rows:
        proj = f" [{s['project_id']}]" if s["project_id"] else ""
        console.print(
            f"- {s['id'][:8]}…{proj} {s['title'] or '(untitled)'} "
            f"(~{s['est_tokens']} tok, updated {s['updated_at'][:16]})"
        )


@sessions_app.command("distill")
def sessions_distill(
    session_id: str = typer.Argument(..., help="Session id (full or unique prefix)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Extract a summary + durable facts from a session into the vector memory."""
    from quickjoiner.sessions import SessionManager

    ctx = _context(workspace)
    matches = [s for s in ctx.catalog.list_sessions() if s["id"].startswith(session_id)]
    if len(matches) != 1:
        console.print(f"[red]{'No' if not matches else 'Ambiguous'} session {session_id!r}[/red]")
        raise typer.Exit(1)
    try:
        facts = SessionManager(ctx).distill(matches[0]["id"], provider=ctx.build_provider())
    except Exception as exc:
        console.print(f"[red]Distill failed: {exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Distilled into memory[/green] ({facts} durable facts extracted).")


@app.command()
def connect(
    type: str = typer.Argument(..., help="Connector type: files | git | github | gitlab | jira | confluence | azure_devops | octopus | onedrive | grafana | datadog | dynatrace | elastic | web_scrape"),
    name: str = typer.Option(..., "--name", "-n", help="Unique source name"),
    option: list[str] = typer.Option([], "--option", "-o", help="Connector option key=value (repeatable). Secrets can use env indirection: token=env:GITHUB_TOKEN"),
    skip_test: bool = typer.Option(False, help="Save without testing the connection"),
    share: Optional[bool] = typer.Option(None, "--share/--private", help="Share with everyone, or keep it yours only (signed in: private by default)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Register a source in the workspace config (then run: qj sync)."""
    from quickjoiner.auth import Auth, can_manage
    from quickjoiner.connectors.registry import create_connector

    ctx = _context(workspace)
    options: dict = {}
    for entry in option:
        if "=" not in entry:
            console.print(f"[red]Bad --option {entry!r}; expected key=value[/red]")
            raise typer.Exit(1)
        key, _, value = entry.partition("=")
        if "," in value:
            options[key] = [v.strip() for v in value.split(",")]
        else:
            options[key] = value

    auth_enabled = Auth(ctx.catalog).enabled
    user = _session_user(ctx)
    if auth_enabled and user is None:
        console.print("[red]Auth is on for this workspace — sign in first: qj login <name>[/red]")
        raise typer.Exit(1)
    existing = next((s for s in ctx.config.sources if s.name == name), None)
    if existing is not None and not can_manage(existing, user, auth_enabled):
        console.print(f"[red]The name {name!r} belongs to {existing.owner}. Pick another name.[/red]")
        raise typer.Exit(1)

    # Signed in: yours (private) unless --share. Open mode: commons.
    shared = share if share is not None else not (auth_enabled and user)
    source = SourceConfig(name=name, type=type, options=options, owner=user, shared=shared)
    connector = create_connector(source, ctx.workspace)

    if not skip_test:
        result = connector.test()
        color = "green" if result.ok else "red"
        console.print(f"[{color}]Connection test: {result.message}[/{color}]")
        if not result.ok:
            console.print("Fix the options or re-run with --skip-test to save anyway.")
            raise typer.Exit(1)

    ctx.config.sources = [s for s in ctx.config.sources if s.name != name] + [source]
    ctx.catalog.save_config(ctx.config)
    scope = "shared with everyone" if source.shared else f"private to {user}" if user else "shared"
    console.print(f"[green]Source '{name}' ({type}) saved — {scope}.[/green] Run [bold]qj sync {name}[/bold] to learn from it.")

    # Auto-wire nudge: a git URL for a GitHub/GitLab host has a matching API connector that
    # adds MRs/issues/pipelines + the ticket↔MR graph on top of the cloned code.
    if type == "git":
        from quickjoiner.connectors.git_repo import suggest_api_connector

        sug = suggest_api_connector(str(options.get("url", "")))
        if sug:
            api_name = f"{name} {sug['type']}"
            opts_str = " ".join(f'--option {k}={v}' for k, v in sug["options"].items())
            manual = f'qj connect {sug["type"]} --name "{api_name}" {opts_str}'
            if sys.stdin.isatty() and typer.confirm(
                f"\nThis looks like a {sug['label']}. Also connect its merge requests, "
                f"issues & pipelines (adds the ticket↔MR graph)?", default=True
            ):
                api_source = SourceConfig(name=api_name, type=sug["type"],
                                          options=sug["options"], owner=user, shared=shared)
                res = create_connector(api_source, ctx.workspace).test()
                if res.ok:
                    ctx.config.sources = ([s for s in ctx.config.sources if s.name != api_name]
                                          + [api_source])
                    ctx.catalog.save_config(ctx.config)
                    console.print(f"[green]Also saved '{api_name}' ({sug['type']}).[/green] "
                                  f"Run [bold]qj sync {api_name}[/bold].")
                else:
                    console.print(f"[yellow]Couldn't reach it ({res.message}); add it later with:[/yellow]\n  {manual}")
            elif not sys.stdin.isatty():
                console.print(f"[dim]Tip — also learn its MRs/issues/pipelines:[/dim]\n  {manual}")


@app.command()
def sync(
    name: Optional[str] = typer.Argument(None, help="Source name to sync (default: all configured sources)"),
    clean: bool = typer.Option(False, "--clean", help="Purge the source's documents/vectors/graph first, then full-resync from scratch (leaves everything consistent)."),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Pull from configured sources, ingest changes into memory (incremental).

    With --clean, each target is purged first (documents, vectors, FTS, and the graph
    edges they produced, plus orphaned graph nodes) and then fully re-synced — the
    reusable, non-corrupting resync."""
    from datetime import datetime, timezone

    from quickjoiner.agent.repo_docs import maybe_autogenerate
    from quickjoiner.connectors.registry import create_connector

    ctx = _context(workspace)
    visible_now = ctx.visible_sources(_session_user(ctx))
    targets = [s for s in visible_now if name is None or s.name == name]
    if not targets:
        console.print(f"[red]No configured source named {name!r}. See: qj sources[/red]" if name else "[yellow]No sources configured. Use qj connect first.[/yellow]")
        raise typer.Exit(1)

    for source in targets:
        connector = create_connector(source, ctx.workspace)
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        if clean:
            removed = ctx.catalog.delete_documents_for_source(connector.source_id)
            ctx.store.delete_source(connector.source_id)
            orphans = ctx.catalog.gc_orphan_entities()
            ctx.catalog.clear_sync_state(connector.source_id)
            console.print(f"[dim]cleaned {source.name}: removed {removed} docs, {orphans} orphan graph nodes[/dim]")
        state = {} if clean else ctx.catalog.get_sync_state(connector.source_id)
        started = datetime.now(timezone.utc).isoformat()
        console.print(f"Syncing [bold]{source.name}[/bold] ({source.type}) ...")
        try:
            with console.status("Pulling and ingesting..."):
                stats = ctx.pipeline.ingest(connector.sync(state), connector.source_id)
        except Exception as exc:
            console.print(f"[red]Sync failed for {source.name}: {exc}[/red]")
            continue
        ctx.catalog.set_sync_state_many(connector.source_id, state)
        ctx.catalog.set_sync_state(connector.source_id, "since", started)
        console.print(f"[green]{source.name}:[/green] {stats.summary()}")
        for err in stats.errors[:5]:
            console.print(f"[yellow]warn:[/yellow] {err}")
        _autogen = maybe_autogenerate(ctx, source, on_log=lambda m: console.print(f"[dim]{m}[/dim]"))
        if _autogen:
            console.print(f"[green]Architecture brief:[/green] {_autogen}")


@app.command()
def resync(
    name: str = typer.Argument(..., help="Source name to purge and fully re-sync"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Purge a source (documents, vectors, graph edges + orphan nodes, sync state) and
    re-sync it from scratch. Shorthand for `qj sync <name> --clean`."""
    sync(name=name, clean=True, workspace=workspace)


@app.command("drain-graph")
def drain_graph(
    name: Optional[str] = typer.Argument(None, help="Source name to scope the drain to (default: every source)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Mine relationships for documents whose deferred graph work never landed.

    LLM relationship extraction is deferred at ingest and normally retried by the next
    sync that re-provides the document — which never comes for a connector that ingests a
    moving window (recent sprints), so those documents keep their chunks and citations but
    never get any edges. This re-reads the text already indexed for them and finishes the
    job in place, with no connector round-trip. One LLM call per document, so it can take
    a while; Ctrl-C is safe — anything undrained stays queued for next time."""
    from quickjoiner.connectors.registry import create_connector

    ctx = _context(workspace)
    if not ctx.pipeline.extracts_triples:
        console.print("[yellow]LLM relationship extraction is off — set graph.extract_triples "
                      "(Settings → Knowledge graph) before draining.[/yellow]")
        raise typer.Exit(1)
    source_id = None
    if name is not None:
        source = next((s for s in ctx.visible_sources(_session_user(ctx)) if s.name == name), None)
        if source is None:
            console.print(f"[red]No configured source named {name!r}[/red]")
            raise typer.Exit(1)
        source_id = create_connector(source, ctx.workspace).source_id
    queued = ctx.catalog.count_graph_pending(source_id)
    if not queued:
        console.print("[green]Nothing queued — every ingested document has had its "
                      "relationships mined.[/green]")
        return
    console.print(f"Draining [bold]{queued}[/bold] document(s) with unmined relationships ...")
    stats = ctx.pipeline.drain_pending_graph(
        source_id=source_id, log=lambda line: console.print(f"[dim]{line}[/dim]")
    )
    console.print(f"[green]done:[/green] {stats.summary()}")
    for err in stats.errors[:5]:
        console.print(f"[yellow]warn:[/yellow] {err}")


@app.command("test")
def test_source(
    name: str = typer.Argument(..., help="Configured source name"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Test connectivity/credentials for a configured source."""
    from quickjoiner.connectors.registry import create_connector

    ctx = _context(workspace)
    source = next((s for s in ctx.visible_sources(_session_user(ctx)) if s.name == name), None)
    if source is None:
        console.print(f"[red]No configured source named {name!r}[/red]")
        raise typer.Exit(1)
    result = create_connector(source, ctx.workspace).test()
    color = "green" if result.ok else "red"
    console.print(f"[{color}]{result.message}[/{color}]")
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def sources(workspace: Optional[Path] = WORKSPACE_OPT):
    """List connected sources you can see (yours, shared, and commons)."""
    ctx = _context(workspace)
    user = _session_user(ctx)
    mine = {s.name: s for s in ctx.visible_sources(user)}
    all_configured = {s.name for s in ctx.config.sources}
    for s in ctx.catalog.list_sources():
        cfg = mine.get(s["name"])
        if cfg is None and s["name"] in all_configured:
            continue  # configured but not visible to this user
        badge = ""
        if cfg is not None and cfg.owner:
            badge = f", owner={cfg.owner}" + ("" if cfg.shared else ", private")
        console.print(f"- {s['id']} (type={s['type']}, documents={s['doc_count']}{badge})")


users_app = typer.Typer(help="Workspace users. Creating the first user turns authentication on.")
app.add_typer(users_app, name="users")


@users_app.command("add")
def users_add(
    username: str = typer.Argument(..., help="New username"),
    password: Optional[str] = typer.Option(None, "--password", help="Password (omit to be prompted)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Create a user. The first user enables auth for this workspace."""
    from quickjoiner.auth import Auth

    ctx = _context(workspace)
    auth = Auth(ctx.catalog)
    if auth.enabled and _session_user(ctx) is None:
        console.print("[red]Sign in first: qj login <your-username>[/red]")
        raise typer.Exit(1)
    pw = password or typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        auth.create_user(username, pw)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]User '{username}' created.[/green] Sign in with [bold]qj login {username}[/bold].")


@users_app.command("list")
def users_list(workspace: Optional[Path] = WORKSPACE_OPT):
    """List workspace users."""
    ctx = _context(workspace)
    rows = ctx.catalog.list_users()
    if not rows:
        console.print("No users yet — open mode. Create one with [bold]qj users add <name>[/bold].")
        return
    for u in rows:
        console.print(f"- {u['username']} (since {u['created_at'][:10]})")


@app.command()
def login(
    username: str = typer.Argument(..., help="Your username"),
    password: Optional[str] = typer.Option(None, "--password", help="Password (omit to be prompted)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Sign in; private connectors you own become usable in this workspace."""
    from quickjoiner.auth import SESSION_FILE, Auth

    ctx = _context(workspace)
    pw = password or typer.prompt("Password", hide_input=True)
    try:
        token = Auth(ctx.catalog).login(username, pw)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    (ctx.workspace / SESSION_FILE).write_text(token, encoding="utf-8")
    console.print(f"[green]Signed in as {username}.[/green]")


@app.command()
def logout(workspace: Optional[Path] = WORKSPACE_OPT):
    """Sign out of this workspace."""
    from quickjoiner.auth import SESSION_FILE, Auth

    ctx = _context(workspace)
    token_file = ctx.workspace / SESSION_FILE
    if token_file.exists():
        Auth(ctx.catalog).logout(token_file.read_text(encoding="utf-8").strip())
        token_file.unlink()
    console.print("Signed out.")


@app.command()
def whoami(workspace: Optional[Path] = WORKSPACE_OPT):
    """Show who is signed in, and whether auth is enabled."""
    from quickjoiner.auth import Auth

    ctx = _context(workspace)
    if not Auth(ctx.catalog).enabled:
        console.print("Open mode — no users yet, everything is shared. [dim](qj users add <name> to enable auth)[/dim]")
        return
    user = _session_user(ctx)
    console.print(f"Signed in as [bold]{user}[/bold]." if user else "Not signed in. Use [bold]qj login <name>[/bold].")


@app.command()
def brief(
    type: str = typer.Argument(..., help="Brief type: architecture | week1 | roadmap | quick-wins"),
    provider: Optional[str] = PROVIDER_OPT,
    model: Optional[str] = MODEL_OPT,
    format: Optional[str] = typer.Option(None, "--format", "-f", help="Also export as: html | csv | pptx (markdown is always saved)"),
    out: Optional[Path] = typer.Option(None, "--out", help="Export file path (default: <workspace>/exports/)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Generate an onboarding brief from learned knowledge (cited; gaps listed)."""
    from quickjoiner.agent.briefs import generate_brief

    ctx = _context(workspace)
    try:
        with console.status(f"Writing the {type} brief from learned knowledge..."):
            markdown, path = generate_brief(
                ctx, type, provider_override=provider, model_override=model
            )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except Exception as exc:  # provider/setup failures (e.g. missing API key)
        console.print(f"[red]Brief generation failed: {exc}[/red]")
        raise typer.Exit(1)
    console.print(Markdown(markdown))
    if path:
        console.print(f"\n[green]Saved:[/green] {path} (also ingested into memory)")
    if format and path:
        _export_answer(ctx, markdown, format, out, title=f"{type} brief")


@app.command("agents-md")
def agents_md(
    source: str = typer.Argument(..., help="Name of a configured git/files source (a cloned repo)"),
    provider: Optional[str] = PROVIDER_OPT,
    model: Optional[str] = MODEL_OPT,
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Generate (or refine) a repo's architecture brief from code structure + docs.

    Looks at the repo's file tree, code-graph facts (defines/imports), dependency
    map, and any retrieved docs — never runs code. If the repo already has a real
    AGENTS.md, refines it instead of overwriting it. Always saved under
    <workspace>/generated/<source>/AGENTS.md and re-ingested, never written into
    the repo's own working tree.
    """
    from quickjoiner.agent.repo_docs import generate_agents_md

    ctx = _context(workspace)
    try:
        with console.status(f"Writing the architecture brief for {source}..."):
            markdown, path = generate_agents_md(
                ctx, source, provider_override=provider, model_override=model
            )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except Exception as exc:  # provider/setup failures (e.g. missing API key)
        console.print(f"[red]Architecture brief generation failed: {exc}[/red]")
        raise typer.Exit(1)
    console.print(Markdown(markdown))
    console.print(f"\n[green]Saved:[/green] {path} (also ingested into memory)")


@app.command("eval")
def eval_cmd(
    evalset: Path = typer.Argument(..., help="Path to a YAML eval set (create one with --init)"),
    init: bool = typer.Option(False, "--init", help="Write a starter eval set to the given path and exit"),
    agent: bool = typer.Option(False, "--agent", help="Also run the end-to-end agent layer (needs an LLM)"),
    calibrate: bool = typer.Option(
        False, "--calibrate",
        help="Sweep the grounding threshold and report the value that best separates "
             "grounded answers from refusals on this set",
    ),
    apply: bool = typer.Option(
        False, "--apply",
        help="With --calibrate, write the recommended threshold to retrieval.min_score",
    ),
    compare: Optional[Path] = typer.Option(
        None, "--compare",
        help="Diff this run's summary against a previous report JSON; exits non-zero on "
             "a >2-point regression of any watched metric",
    ),
    provider: Optional[str] = PROVIDER_OPT,
    model: Optional[str] = MODEL_OPT,
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Evaluate grounding quality: retrieval metrics always; agent behavior with --agent."""
    import json as _json

    from quickjoiner.evals.harness import TEMPLATE, compare_reports, run_eval

    if init:
        if evalset.exists():
            console.print(f"[red]{evalset} already exists; not overwriting.[/red]")
            raise typer.Exit(1)
        evalset.parent.mkdir(parents=True, exist_ok=True)
        evalset.write_text(TEMPLATE, encoding="utf-8")
        console.print(f"[green]Starter eval set written:[/green] {evalset} — edit it, then run: qj eval {evalset}")
        return

    if apply and not calibrate:
        console.print("[red]--apply requires --calibrate.[/red]")
        raise typer.Exit(1)

    ctx = _context(workspace)
    try:
        with console.status("Running evals..."):
            report = run_eval(
                ctx, evalset, agent_layer=agent,
                provider_override=provider, model_override=model,
                calibrate_threshold=calibrate,
            )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except Exception as exc:  # provider/setup failures on the agent layer
        console.print(f"[red]Eval failed: {exc}[/red]")
        raise typer.Exit(1)

    table = Table(title=f"Retrieval layer — {report['name']}")
    table.add_column("case")
    table.add_column("hit rank", justify="right")
    table.add_column("top score", justify="right")
    table.add_column("grounded", justify="center")
    for c in report["retrieval"]["cases"]:
        if c["refusal_correct"] is not None:
            grounded = "[green]refused ok[/green]" if c["refusal_correct"] else "[red]leaked[/red]"
            table.add_row(c["case_id"], "-", f"{c['top_score']:.3f}", grounded)
        else:
            rank = str(c["hit_rank"]) if c["hit_rank"] else "[red]miss[/red]"
            grounded = "[green]yes[/green]" if c["cleared_threshold"] else "[red]no[/red]"
            table.add_row(c["case_id"], rank, f"{c['top_score']:.3f}", grounded)
    console.print(table)
    console.print(f"Summary: {report['retrieval']['summary']}")

    if agent and "agent" in report:
        atable = Table(title="Agent layer")
        atable.add_column("case")
        atable.add_column("refusal ok", justify="center")
        atable.add_column("cited", justify="center")
        atable.add_column("missing keywords")
        for c in report["agent"]["cases"]:
            atable.add_row(
                c["case_id"],
                "[green]yes[/green]" if c["refusal_correct"] else "[red]no[/red]",
                {True: "[green]yes[/green]", False: "[red]no[/red]", None: "-"}[c["cited"]],
                ", ".join(c["keywords_missing"]) or "-",
            )
        console.print(atable)
        console.print(f"Summary: {report['agent']['summary']}")
    console.print(f"[green]Report saved:[/green] {report['report_path']}")

    if calibrate and "calibration" in report:
        cal = report["calibration"]
        ctable = Table(title="Threshold calibration")
        ctable.add_column("recommended", justify="right")
        ctable.add_column("optimal band", justify="center")
        ctable.add_column("refusal acc", justify="right")
        ctable.add_column("grounded recall", justify="right")
        ctable.add_column("floor met", justify="center")
        ctable.add_row(
            f"{cal['threshold']:.2f}",
            f"{cal['plateau'][0]:.2f}-{cal['plateau'][1]:.2f}",
            f"{cal['refusal_accuracy']:.3f}",
            f"{cal['grounded_recall']:.3f}",
            "[green]yes[/green]" if cal["floor_met"] else f"[red]no (floor {cal['floor']:.2f})[/red]",
        )
        console.print(ctable)
        console.print(
            "The recommendation is the midpoint of the optimal band (maximum margin); "
            "grounded recall shows what fraction of answerable cases still clear it."
        )
        if not cal["floor_met"]:
            console.print(
                "[yellow]No threshold met the refusal-accuracy floor — this set may leak. "
                "Showing the safest (highest refusal-accuracy) point.[/yellow]"
            )
        if cal["thin"]:
            s = cal["samples"]
            console.print(
                f"[yellow]Thin eval set ({s['answerable']} answerable, {s['refusals']} refusal "
                "cases): the optimal band is wide and this number is not trustworthy. Author "
                "~30-50 representative cases (incl. borderline + refusals) before trusting or "
                "applying it.[/yellow]"
            )
        current = ctx.config.retrieval.min_score
        if apply:
            ctx.config.retrieval.min_score = cal["threshold"]
            ctx.catalog.save_config(ctx.config)
            console.print(
                f"[green]Applied:[/green] retrieval.min_score {current:.2f} -> {cal['threshold']:.2f}"
            )
        else:
            console.print(
                f"Current retrieval.min_score={current:.2f}. Re-run with --apply to set "
                f"it to {cal['threshold']:.2f}."
            )

    if compare is not None:
        try:
            old_report = _json.loads(Path(compare).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            console.print(f"[red]Could not read --compare report {compare}: {exc}[/red]")
            raise typer.Exit(1)
        diff = compare_reports(old_report, report)
        dtable = Table(title=f"Compare vs {Path(compare).name}")
        dtable.add_column("metric")
        dtable.add_column("layer")
        dtable.add_column("old", justify="right")
        dtable.add_column("new", justify="right")
        dtable.add_column("delta", justify="right")
        for row in diff["rows"]:
            old_s = f"{row['old']:.3f}" if isinstance(row["old"], (int, float)) else "-"
            new_s = f"{row['new']:.3f}" if isinstance(row["new"], (int, float)) else "-"
            if row["delta"] is None:
                delta_s = "-"
            elif row["regressed"]:
                delta_s = f"[red]v {row['delta']:+.3f}[/red]"
            elif row["delta"] > 0:
                delta_s = f"[green]^ {row['delta']:+.3f}[/green]"
            elif row["delta"] < 0:
                delta_s = f"[yellow]{row['delta']:+.3f}[/yellow]"
            else:
                delta_s = "0.000"
            dtable.add_row(row["metric"], row["layer"], old_s, new_s, delta_s)
        console.print(dtable)
        if diff["regressed"]:
            console.print("[red]Regression: a watched metric dropped by more than 2 points.[/red]")
            raise typer.Exit(1)
        console.print("[green]No watched metric regressed beyond tolerance.[/green]")


@app.command("bench")
def bench_cmd(
    pack: Path = typer.Argument(..., help="Path to a YAML bench pack (create one with --init). An eval set works too."),
    init: bool = typer.Option(False, "--init", help="Write a starter bench pack to the given path and exit"),
    agent: bool = typer.Option(False, "--agent", help="Also measure end-to-end answer latency + tokens (needs an LLM)"),
    repeats: int = typer.Option(3, "--repeats", help="Timed runs per query (after warm-up)"),
    warmup: int = typer.Option(1, "--warmup", help="Discarded warm-up runs — the first search loads the reranker model"),
    no_embed: bool = typer.Option(False, "--no-embed", help="Skip the embedder throughput leg"),
    compare: Optional[Path] = typer.Option(
        None, "--compare",
        help="Diff this run against a previous bench report JSON; exits non-zero if a "
             "watched metric got >20% slower or more expensive",
    ),
    provider: Optional[str] = PROVIDER_OPT,
    model: Optional[str] = MODEL_OPT,
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Measure speed and cost: per-stage retrieval latency, embedder throughput, and
    (with --agent) answer latency + tokens per answer.

    The speed twin of `qj eval`. Run it before and after any performance change and
    compare the two reports — a claim of "faster" without a bench table is a guess.
    """
    import json as _json

    from quickjoiner.bench.harness import TEMPLATE, compare_reports, run_bench

    if init:
        if pack.exists():
            console.print(f"[red]{pack} already exists; not overwriting.[/red]")
            raise typer.Exit(1)
        pack.parent.mkdir(parents=True, exist_ok=True)
        pack.write_text(TEMPLATE, encoding="utf-8")
        console.print(f"[green]Starter bench pack written:[/green] {pack} — edit it, then run: qj bench {pack}")
        return

    ctx = _context(workspace)
    try:
        with console.status("Benchmarking..."):
            report = run_bench(
                ctx, pack, agent_layer=agent, repeats=repeats, warmup=warmup,
                embed=not no_embed, provider_override=provider, model_override=model,
            )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except Exception as exc:  # provider/setup failures on the agent layer
        console.print(f"[red]Bench failed: {exc}[/red]")
        raise typer.Exit(1)

    summary = report["retrieval"]["summary"]
    cfg = report["config"]
    console.print(
        f"[dim]{cfg['documents']} documents / {cfg['chunks']} chunks · {cfg['embedding']} · "
        f"hybrid={cfg['hybrid']} reranker={cfg['reranker']}[/dim]"
    )
    stable = Table(title=f"Retrieval latency — {report['name']} "
                         f"({summary['queries']} queries x {repeats} runs)")
    stable.add_column("stage")
    stable.add_column("p50 ms", justify="right")
    stable.add_column("p95 ms", justify="right")
    stable.add_column("share of p50", justify="right")
    shares = summary.get("stage_share_of_p50", {})
    for name, stats in sorted(summary["stages_ms"].items(),
                              key=lambda kv: -kv[1].get("p50", 0)):
        stable.add_row(name, f"{stats['p50']:.2f}", f"{stats['p95']:.2f}",
                       f"{shares.get(name, 0) * 100:.0f}%")
    total = summary["search_total_ms"]
    stable.add_row("[bold]total[/bold]", f"[bold]{total['p50']:.2f}[/bold]",
                   f"[bold]{total['p95']:.2f}[/bold]", "")
    console.print(stable)
    if summary.get("counts"):
        console.print(f"[dim]Mean candidates: {summary['counts']}[/dim]")

    if "embed" in report:
        e = report["embed"]["summary"]
        console.print(
            f"Embedder [bold]{e['model']}[/bold] (device={e['device']}): "
            f"{e['chunks_per_sec']} chunks/sec — {e['ms_per_chunk']:.3f} ms/chunk "
            f"at batch {e['batch']}"
        )

    if agent and "agent" in report:
        a = report["agent"]["summary"]
        atable = Table(title="Agent layer")
        atable.add_column("metric")
        atable.add_column("p50", justify="right")
        atable.add_column("p95", justify="right")
        ft = a["first_token_ms"]
        if ft.get("samples"):
            atable.add_row("first token (ms)", f"{ft['p50']:.0f}", f"{ft['p95']:.0f}")
        else:
            atable.add_row("first token (ms)", f"[yellow]{ft.get('note', '-')}[/yellow]", "")
        atable.add_row("full answer (ms)", f"{a['answer_ms']['p50']:.0f}", f"{a['answer_ms']['p95']:.0f}")
        atable.add_row("tokens / answer", f"{a['tokens_per_answer']['p50']:.0f}",
                       f"{a['tokens_per_answer']['p95']:.0f}")
        console.print(atable)
        console.print(
            f"{a['rounds_per_answer']} model rounds and {a['tool_calls_per_answer']} tool "
            f"calls per answer; {a['prompt_tokens_total']} prompt + "
            f"{a['completion_tokens_total']} completion tokens over {a['answers']} answers."
        )
        if "cache_hit_rate" in a:
            rate = a["cache_hit_rate"]
            if rate > 0:
                console.print(f"[green]Prompt cache: {rate:.1%} of prompt tokens were cache reads.[/green]")
            else:
                # Deliberately provider-neutral: Anthropic caches explicitly (llm.prompt_cache
                # breakpoints), OpenAI-compatible backends do it automatically off a stable
                # prefix, and Ollama reports no cache counter at all — so 0 means different
                # things, and naming one config field would misdiagnose the other two.
                console.print(
                    "[yellow]Prompt cache: 0 of the prompt was served from cache. Either the "
                    "backend doesn't cache (Ollama reports no counter at all), the prompt is "
                    "under its minimum cacheable length, or the prefix isn't byte-stable.[/yellow]"
                )
    console.print(f"[green]Report saved:[/green] {report['report_path']}")

    if compare is not None:
        try:
            old_report = _json.loads(Path(compare).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            console.print(f"[red]Could not read --compare report {compare}: {exc}[/red]")
            raise typer.Exit(1)
        diff = compare_reports(old_report, report)
        dtable = Table(title=f"Compare vs {Path(compare).name}")
        dtable.add_column("layer")
        dtable.add_column("metric")
        dtable.add_column("old", justify="right")
        dtable.add_column("new", justify="right")
        dtable.add_column("change", justify="right")
        for row in diff["rows"]:
            old_s = f"{row['old']:.2f}" if isinstance(row["old"], (int, float)) else "-"
            new_s = f"{row['new']:.2f}" if isinstance(row["new"], (int, float)) else "-"
            if row["change"] is None:
                change_s = "-"
            elif row["regressed"]:
                change_s = f"[red]{row['change']:+.1%} slower[/red]"
            elif not row["gated"]:
                change_s = f"[dim]{row['change']:+.1%} (not gated)[/dim]"
            elif row["change"] < 0:
                change_s = f"[green]{row['change']:+.1%}[/green]"
            else:
                change_s = f"{row['change']:+.1%}"
            dtable.add_row(row["layer"], row["metric"], old_s, new_s, change_s)
        console.print(dtable)
        if diff["regressed"]:
            console.print(
                f"[red]Regression: a watched metric got more than "
                f"{diff['tolerance']:.0%} worse.[/red]"
            )
            raise typer.Exit(1)
        console.print("[green]No watched metric regressed beyond tolerance.[/green]")


@app.command()
def extract(
    path: Path = typer.Argument(..., help="A document to run the text extractor over"),
    full: bool = typer.Option(False, "--full", help="Print all extracted text, not a preview"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the extracted text to a file"),
):
    """Show exactly what QuickJoiner can read out of a file — no workspace, no ingestion.

    The answer to "it has plenty of text, why did nothing get learned?". Reports the
    characters extracted (per slide for a deck) so you can tell a parser gap from a
    genuinely picture-only document — the two look identical from the chat window.
    """
    from quickjoiner.ingest.extract import ExtractionError, extract_text, is_image, supported_extension

    if not path.is_file():
        console.print(f"[red]Not a readable file:[/red] {path}")
        raise typer.Exit(1)
    if is_image(path.name):
        console.print("[yellow]This is an image.[/yellow] Reading images needs vision support, "
                      "which isn't enabled yet.")
        raise typer.Exit(1)
    if not supported_extension(path.name):
        console.print(f"[yellow]Unsupported file type:[/yellow] {path.suffix or '(none)'}")
        raise typer.Exit(1)
    try:
        text = extract_text(path.read_bytes(), path.name)
    except ExtractionError as exc:
        console.print(f"[red]Could not extract text:[/red] {exc}")
        raise typer.Exit(1)

    console.print(f"[bold]{path.name}[/bold] — [green]{len(text)}[/green] characters extracted")
    slides = [s for s in text.split("\n\n") if s.startswith("Slide ")]
    if slides:
        table = Table(title="Per slide")
        table.add_column("slide")
        table.add_column("chars", justify="right")
        table.add_column("first line")
        for slide in slides:
            head, _, body = slide.partition("\n")
            first = (body.splitlines() or [""])[0]
            table.add_row(head.rstrip(":"), str(len(body)),
                          first[:60] + ("…" if len(first) > 60 else ""))
        console.print(table)
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]Written:[/green] {out}")
    elif not text.strip():
        console.print("[yellow]No text at all.[/yellow] This looks like a picture-only or scanned "
                      "document — reading it needs vision support, which isn't enabled yet.")
    else:
        console.print(text if full else text[:2000] + ("\n[dim]…(--full for all)[/dim]" if len(text) > 2000 else ""))


onedrive_app = typer.Typer(help="OneDrive / SharePoint: sign in with your own Microsoft 365 account and learn documents on demand.")
app.add_typer(onedrive_app, name="onedrive")


def _onedrive_source(ctx, name: str):
    """Resolve a configured OneDrive connector by name, or exit with a useful message."""
    from quickjoiner.connectors.onedrive import OneDriveConnector
    from quickjoiner.connectors.registry import create_connector

    source = next((s for s in ctx.config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in ctx.config.sources if s.type == "onedrive") or "(none)"
        console.print(f"[red]No connector named {name!r}.[/red] OneDrive connectors: {known}")
        raise typer.Exit(1)
    connector = create_connector(source, ctx.workspace)
    if not isinstance(connector, OneDriveConnector):
        console.print(f"[red]{name!r} is a {source.type} connector, not OneDrive/SharePoint.[/red]")
        raise typer.Exit(1)
    return source, connector


@onedrive_app.command("login")
def onedrive_login(
    name: str = typer.Argument(..., help="Name of the OneDrive connector to sign in"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Sign in to Microsoft 365 with the device-code flow.

    Prints a short code to type at microsoft.com/devicelogin. This is the flow that
    works everywhere QuickJoiner runs — a terminal over SSH, a Docker container, a
    machine with no browser — because nothing has to redirect back to this process.
    """
    from quickjoiner.connectors import msgraph

    ctx = _context(workspace)
    _source, connector = _onedrive_source(ctx, name)
    if not connector.client_id:
        console.print("[red]This connector has no Application (client) ID.[/red] "
                      "Set it first: qj connect onedrive --name … -o client_id=…")
        raise typer.Exit(1)
    try:
        flow = msgraph.begin_device_code(connector.tenant, connector.client_id, connector.scopes)
    except msgraph.GraphError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"\n[bold]{flow.get('message', '')}[/bold]\n")
    console.print("[dim]Waiting for you to finish signing in…[/dim]")
    try:
        bundle = msgraph.poll_device_code(
            connector.tenant, connector.client_id, flow["device_code"],
            interval=int(flow.get("interval", 5)), expires_in=int(flow.get("expires_in", 900)),
        )
    except msgraph.GraphError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    bundle.tenant, bundle.client_id = connector.tenant, connector.client_id
    msgraph.save_token(ctx.workspace, connector.source_id, bundle)
    try:
        me = msgraph.GraphClient(ctx.workspace, connector.source_id, connector.scopes).whoami()
        bundle.account = me.get("userPrincipalName") or me.get("displayName") or ""
        msgraph.save_token(ctx.workspace, connector.source_id, bundle)
    except msgraph.GraphError:
        pass  # signed in fine; we just couldn't put a name to the account
    console.print(f"[green]Signed in{' as ' + bundle.account if bundle.account else ''}.[/green]")
    console.print("[dim]Nothing is synced automatically. Learn a document with:\n"
                  f"  qj onedrive learn {name} <url-or-path>[/dim]")


@onedrive_app.command("status")
def onedrive_status(
    name: str = typer.Argument(..., help="Name of the OneDrive connector"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Show who this connector is signed in as, and what it has learned."""
    ctx = _context(workspace)
    _source, connector = _onedrive_source(ctx, name)
    result = connector.test()
    console.print(f"[{'green' if result.ok else 'red'}]{result.message}[/]")


@onedrive_app.command("logout")
def onedrive_logout(
    name: str = typer.Argument(..., help="Name of the OneDrive connector"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Delete this connector's stored Microsoft 365 refresh token."""
    from quickjoiner.connectors import msgraph

    ctx = _context(workspace)
    _source, connector = _onedrive_source(ctx, name)
    removed = msgraph.delete_token(ctx.workspace, connector.source_id)
    console.print("[green]Signed out.[/green]" if removed else "[yellow]Was not signed in.[/yellow]")


@onedrive_app.command("learn")
def onedrive_learn_cmd(
    name: str = typer.Argument(..., help="Name of the OneDrive connector"),
    targets: List[str] = typer.Argument(..., help="Shared links, or paths inside your own OneDrive"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Learn specific documents on demand — the CLI twin of
    `/qj learn from this onedrive document <url>`.

    Accepts any OneDrive/SharePoint link you can open, or a path in your own drive. A
    folder learns the readable files beneath it. Nothing else is touched: this
    connector never crawls.
    """
    from quickjoiner.connectors.msgraph import GraphError
    from quickjoiner.connectors.onedrive import is_communal_memory_warning

    ctx = _context(workspace)
    source, connector = _onedrive_source(ctx, name)
    console.print(f"[yellow]Note:[/yellow] {is_communal_memory_warning()}")
    try:
        documents, notes = connector.learn(list(targets))
    except GraphError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    for note in notes:
        console.print(f"[yellow]skipped:[/yellow] {note}")
    if not documents:
        console.print("[red]Nothing was learned.[/red]")
        raise typer.Exit(1)
    ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
    stats = ctx.pipeline.ingest(documents, connector.source_id)
    for doc in documents:
        console.print(f"[green]learned:[/green] {doc.title}")
    console.print(f"[green]{name}:[/green] {stats.summary()}")


browser_app = typer.Typer(help="Persistent browser profile for user-credential fallback (SSO/MFA logins).")
app.add_typer(browser_app, name="browser")


@browser_app.command("login")
def browser_login(
    url: str = typer.Argument(..., help="Login page to open (e.g. your SSO portal or the tool's URL)"),
    workspace: Optional[Path] = WORKSPACE_OPT,
    insecure: bool = typer.Option(
        False, "--insecure",
        help="Accept a certificate signed by a private/corporate CA (mirrors the connector's "
             "verify_tls=false). Only for internal hosts you trust.",
    ),
):
    """Open a real Chromium window; sign in, then close it. The session persists.

    Afterwards the URL is re-fetched headlessly through the saved session and the result
    reported — a login that did not take is otherwise indistinguishable from one that did,
    right up until a sync mysteriously ingests nothing."""
    from quickjoiner.connectors.browser.session import login, verify_session

    ws = _workspace(workspace)
    console.print(f"Opening browser for [bold]{url}[/bold] — sign in, then close the window.")
    try:
        report = login(ws, url, on_log=lambda m: console.print(f"[green]{m}[/green]"),
                       ignore_https_errors=insecure)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if report["hosts"]:
        console.print(f"[dim]captured {report['cookies']} cookie(s) "
                      f"({report['real_cookies']} session/auth) for: {', '.join(report['hosts'])}[/dim]")
    console.print("Verifying the saved session...")
    ok, detail = verify_session(ws, url, ignore_https_errors=insecure)
    if ok:
        console.print(f"[green]✓ Signed in.[/green] {detail}")
        console.print("Scrape connectors with use_browser=true can now use it.")
    else:
        console.print(f"[yellow]✗ Not signed in — the session was not captured.[/yellow] {detail}")
        console.print("[dim]Tip: complete the sign-in until the site's own page renders, then close "
                      "the window. If your SSO offers a 'Use Domain Credentials' / Windows-integrated "
                      "button, prefer the username+password form — Chromium here has no enterprise "
                      "auth allowlist.[/dim]")
        raise typer.Exit(1)


@browser_app.command("status")
def browser_status(
    url: Optional[str] = typer.Argument(None, help="Optionally verify the session against this URL"),
    workspace: Optional[Path] = WORKSPACE_OPT,
    insecure: bool = typer.Option(
        False, "--insecure",
        help="Accept a certificate signed by a private/corporate CA when verifying.",
    ),
):
    """Show the saved browser session — and, with a URL, whether it actually still works."""
    from quickjoiner.connectors.browser.session import (
        has_profile,
        profile_dir,
        session_hosts,
        verify_session,
    )

    ws = _workspace(workspace)
    if not has_profile(ws):
        console.print("[yellow]No browser profile yet.[/yellow] Run: qj browser login <url>")
        return
    console.print(f"[green]Browser profile exists:[/green] {profile_dir(ws)}")
    hosts = session_hosts(ws)
    if hosts:
        console.print(f"[dim]saved session cookies for: {', '.join(hosts)}[/dim]")
    else:
        console.print("[yellow]No saved session cookies[/yellow] — a profile alone does not mean "
                      "you are signed in anywhere.")
    if url:
        ok, detail = verify_session(ws, url, ignore_https_errors=insecure)
        console.print(f"[green]✓ Signed in.[/green] {detail}" if ok
                      else f"[yellow]✗ Not signed in.[/yellow] {detail}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8787, help="Port"),
    workspace: Optional[Path] = WORKSPACE_OPT,
):
    """Start the web UI + API (chat, sources dashboard, webhook receivers, scheduled syncs)."""
    import uvicorn

    from quickjoiner.api.app import create_app
    from quickjoiner.app import build_context
    from quickjoiner.scheduler import start_scheduler

    ws = _workspace(workspace)
    if not ws.exists():
        console.print(f"[red]Workspace {ws} does not exist. Run [bold]qj init[/bold] first.[/red]")
        raise typer.Exit(1)

    scheduler = start_scheduler(build_context(ws))
    if scheduler:
        sync_jobs = sum(1 for j in scheduler.get_jobs() if j.id.startswith("sync-"))
        console.print(f"[green]Scheduler started[/green] ({sync_jobs} periodic sync job(s) + context-file cleanup)")
    console.print(f"QuickJoiner UI: [bold]http://{host}:{port}[/bold]  (webhooks: POST /hooks/<source>)")
    try:
        uvicorn.run(create_app(ws), host=host, port=port, log_level="warning")
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    app()
