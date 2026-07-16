"""Background sync jobs: startable, stoppable, live-logged, and graph-safe.

One `SyncManager` per app runs a source's sync on a daemon thread and exposes it
as a job you can watch and stop from the UI/CLI:

* **Interruptible** — the job wraps the connector's document stream in a generator
  that checks a `threading.Event` before each document, so a stop request halts the
  ingest cleanly between documents (partial work already committed is idempotent).
* **Live logs** — every progress line is appended to the job and fan-out to any SSE
  subscribers, so the UI streams the sync as it happens.
* **Graph-safe cleanup** — a `clean` start (or a stop-with-cleanup) purges the source
  first/after via the catalog primitives: delete documents (which cascades their graph
  edges), drop the source's vectors + FTS, GC orphaned graph nodes, and clear the sync
  watermark. That leaves docs, vectors, FTS and the knowledge graph mutually consistent
  — a half-synced or stale-shape source can't leave a corrupted graph behind.

Purely orchestration: the actual pull/ingest is unchanged (`connector.sync` →
`pipeline.ingest`); this only wraps it with control + observability.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

_MAX_LOG_LINES = 500
_HEARTBEAT_SECONDS = 20  # "still syncing…" cadence while a connector is mid-pull
_DONE = object()  # sentinel pushed to subscribers when a job ends


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SyncJob:
    id: str
    source_name: str
    source_id: str
    state: str = "running"  # running | stopping | stopped | done | error
    clean: bool = False
    cleanup_on_stop: bool = False
    logs: list[str] = field(default_factory=list)
    stats: dict[str, Any] | None = None
    error: str | None = None
    ingested: int = 0
    started_at: str = field(default_factory=_now)
    ended_at: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    subscribers: list[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source_name, "state": self.state,
            "clean": self.clean, "stats": self.stats, "error": self.error,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "log_lines": len(self.logs),
        }


class SyncManager:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self._jobs: dict[str, SyncJob] = {}  # keyed by source_name (one active per source)
        self._lock = threading.Lock()
        self._counter = 0

    # -- queries --------------------------------------------------------------
    def job_for(self, source_name: str) -> SyncJob | None:
        return self._jobs.get(source_name)

    def status(self) -> list[dict[str, Any]]:
        return [j.summary() for j in self._jobs.values()]

    def is_running(self, source_name: str) -> bool:
        j = self._jobs.get(source_name)
        return bool(j and j.state in ("running", "stopping"))

    # -- control --------------------------------------------------------------
    def start(self, source_name: str, clean: bool = False) -> SyncJob:
        with self._lock:
            if self.is_running(source_name):
                raise RuntimeError(f"A sync is already running for {source_name!r}")
            source = next((s for s in self.ctx.config.sources if s.name == source_name), None)
            if source is None:
                raise KeyError(f"No configured source {source_name!r}")
            self._counter += 1
            from quickjoiner.connectors.registry import create_connector

            connector = create_connector(source, self.ctx.workspace)
            job = SyncJob(id=f"sync-{self._counter}", source_name=source_name,
                          source_id=connector.source_id, clean=clean)
            self._jobs[source_name] = job
        threading.Thread(target=self._run, args=(job, source, connector), daemon=True).start()
        return job

    def stop(self, source_name: str, cleanup: bool = False) -> SyncJob:
        job = self._jobs.get(source_name)
        if job is None or job.state not in ("running", "stopping"):
            raise RuntimeError(f"No running sync for {source_name!r}")
        job.cleanup_on_stop = cleanup
        job.state = "stopping"
        job.cancel.set()
        self._log(job, "⏹ stop requested" + (" — will clean up partial data" if cleanup else ""))
        return job

    # -- log fan-out ----------------------------------------------------------
    def _log(self, job: SyncJob, line: str) -> None:
        with job.lock:
            job.logs.append(line)
            if len(job.logs) > _MAX_LOG_LINES:
                del job.logs[: len(job.logs) - _MAX_LOG_LINES]
            subs = list(job.subscribers)
        for q in subs:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass

    def subscribe(self, source_name: str) -> tuple[SyncJob | None, queue.Queue]:
        """Register an SSE subscriber; it receives any backlog then live lines, and a
        `_DONE` sentinel when the job ends."""
        q: queue.Queue = queue.Queue(maxsize=1000)
        job = self._jobs.get(source_name)
        if job is not None:
            with job.lock:
                for line in job.logs:  # replay backlog so a late viewer sees the whole run
                    q.put_nowait(line)
                if job.state in ("running", "stopping"):
                    job.subscribers.append(q)
                else:
                    q.put_nowait(_DONE)
        return job, q

    # -- the job --------------------------------------------------------------
    def _purge(self, job: SyncJob) -> None:
        removed = self.ctx.catalog.delete_documents_for_source(job.source_id)
        self.ctx.store.delete_source(job.source_id)
        orphans = self.ctx.catalog.gc_orphan_entities()
        self.ctx.catalog.clear_sync_state(job.source_id)
        self._log(job, f"🧹 cleanup: removed {removed} documents, {orphans} orphan graph nodes")

    def _tracked(self, job: SyncJob, docs: Iterator) -> Iterator:
        for doc in docs:
            if job.cancel.is_set():
                self._log(job, f"⏹ stopping — {job.ingested} document(s) ingested before cancel")
                return
            job.ingested += 1
            if job.ingested <= 3 or job.ingested % 25 == 0:
                self._log(job, f"  · {job.ingested}: {getattr(doc, 'title', '')[:72]}")
            yield doc

    def _heartbeat(self, job: SyncJob, stop: threading.Event) -> None:
        """Emit a 'still syncing' line on a timer so long connector-internal phases
        (e.g. walking many empty teams before the first document) don't look hung."""
        start = time.monotonic()
        while not stop.wait(_HEARTBEAT_SECONDS):
            elapsed = int(time.monotonic() - start)
            self._log(job, f"⏳ still syncing — {job.ingested} documents so far ({elapsed}s)")

    def _run(self, job: SyncJob, source: Any, connector: Any) -> None:
        hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, args=(job, hb_stop), daemon=True).start()
        try:
            self.ctx.catalog.upsert_source(job.source_id, source.name, source.type, source.options)
            if job.clean:
                self._log(job, "🧹 clean sync — purging existing data first…")
                self._purge(job)
            self._log(job, f"▶ syncing {job.source_name!r}…")
            state = {} if job.clean else self.ctx.catalog.get_sync_state(job.source_id)
            started = _now()
            stats = self.ctx.pipeline.ingest(self._tracked(job, connector.sync(state)), job.source_id)

            if job.cancel.is_set():
                job.state = "stopped"
                if job.cleanup_on_stop:
                    self._purge(job)
                self._log(job, "⏹ sync stopped.")
            else:
                self.ctx.catalog.set_sync_state(job.source_id, "since", started)
                job.stats = {"added": stats.added, "updated": stats.updated,
                             "skipped": stats.skipped, "chunks": stats.chunks,
                             "errors": len(stats.errors)}
                self._log(job, f"✓ done — {stats.summary()}")
                self._autogenerate(job, source)
                job.state = "done"
        except Exception as exc:  # noqa: BLE001 — surface any failure on the job, never crash the thread
            job.state = "error"
            job.error = str(exc)
            self._log(job, f"✗ error: {exc}")
        finally:
            hb_stop.set()
            job.ended_at = _now()
            self._close(job)

    def _autogenerate(self, job: SyncJob, source: Any) -> None:
        try:
            from quickjoiner.agent.repo_docs import maybe_autogenerate

            path = maybe_autogenerate(self.ctx, source)
            if path:
                self._log(job, f"📝 generated architecture brief: {path}")
        except Exception:
            pass  # doc-gen must never fail a sync

    def _close(self, job: SyncJob) -> None:
        with job.lock:
            subs = list(job.subscribers)
            job.subscribers.clear()
        for q in subs:
            try:
                q.put_nowait(_DONE)
            except queue.Full:
                pass
