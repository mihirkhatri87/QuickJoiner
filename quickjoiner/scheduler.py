"""APScheduler-based periodic syncs: sources with sync_interval_minutes learn continuously."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from quickjoiner.app import AppContext
from quickjoiner.connectors.registry import create_connector

log = logging.getLogger("quickjoiner.scheduler")


def _sync_job(ctx: AppContext, source_name: str) -> None:
    source = next((s for s in ctx.config.sources if s.name == source_name), None)
    if source is None:
        return
    try:
        connector = create_connector(source, ctx.workspace)
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        state = ctx.catalog.get_sync_state(connector.source_id)
        started = datetime.now(timezone.utc).isoformat()
        stats = ctx.pipeline.ingest(connector.sync(state), connector.source_id)
        ctx.catalog.set_sync_state_many(connector.source_id, state)
        ctx.catalog.set_sync_state(connector.source_id, "since", started)
        log.info("scheduled sync %s: %s", source_name, stats.summary())
        from quickjoiner.agent.repo_docs import maybe_autogenerate

        brief_path = maybe_autogenerate(ctx, source, on_log=lambda m: log.info("%s", m))
        if brief_path:
            log.info("scheduled sync %s: generated architecture brief %s", source_name, brief_path)
    except Exception:
        log.exception("scheduled sync failed for %s", source_name)


CONTEXT_CLEANUP_INTERVAL_HOURS = 6


def _context_cleanup_job(ctx: AppContext) -> None:
    """Delete per-question chat attachments past their retention window (chat.context_retention_days).
    Files are removed; the catalog rows stay (marked deleted) so history keeps the name + date."""
    try:
        from quickjoiner import chat_attachments

        n = chat_attachments.cleanup_expired(ctx)
        if n:
            log.info("context-attachment cleanup: deleted %d expired file(s)", n)
    except Exception:
        log.exception("context-attachment cleanup failed")


def start_scheduler(ctx: AppContext) -> BackgroundScheduler:
    """Always returns a running scheduler: it registers periodic syncs for sources with a
    `sync_interval_minutes`, AND a standing sweep that deletes expired chat context files —
    the latter must run even in a workspace with no interval-synced sources."""
    scheduler = BackgroundScheduler()
    for source in (s for s in ctx.config.sources if s.sync_interval_minutes):
        scheduler.add_job(
            _sync_job,
            "interval",
            minutes=source.sync_interval_minutes,
            args=[ctx, source.name],
            id=f"sync-{source.name}",
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        _context_cleanup_job, "interval", hours=CONTEXT_CLEANUP_INTERVAL_HOURS,
        args=[ctx], id="context-attachment-cleanup", max_instances=1, coalesce=True,
    )
    _context_cleanup_job(ctx)  # sweep once at startup (catches files that expired while down)
    scheduler.start()
    return scheduler
