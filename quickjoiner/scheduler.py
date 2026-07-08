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
        ctx.catalog.set_sync_state(connector.source_id, "since", started)
        log.info("scheduled sync %s: %s", source_name, stats.summary())
    except Exception:
        log.exception("scheduled sync failed for %s", source_name)


def start_scheduler(ctx: AppContext) -> BackgroundScheduler | None:
    jobs = [s for s in ctx.config.sources if s.sync_interval_minutes]
    if not jobs:
        return None
    scheduler = BackgroundScheduler()
    for source in jobs:
        scheduler.add_job(
            _sync_job,
            "interval",
            minutes=source.sync_interval_minutes,
            args=[ctx, source.name],
            id=f"sync-{source.name}",
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    return scheduler
