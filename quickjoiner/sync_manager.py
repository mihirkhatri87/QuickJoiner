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

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from quickjoiner.connectors.util import is_transient_network_error
from quickjoiner.sync_control import SyncControl, SyncStopped

_MAX_LOG_LINES = 500
_HEARTBEAT_SECONDS = 20  # "still syncing…" cadence while a connector is mid-pull
_DONE = object()  # sentinel pushed to subscribers when a job ends
_HISTORY_RETENTION_DAYS = 7  # rolling window kept in sync_events (UI asks for 24h of it)
RESET_SOURCE = "all memory"  # sentinel "source" name for the workspace-wide memory-reset job
DRAIN_SOURCE = "graph relationships"  # ...and for the workspace-wide graph-pending drain

# Self-healing auto-retry on a transient NETWORK failure mid-sync. A raised exception kills
# the connector's generator (a Python generator can't resume past a raise), so "resume" means
# re-invoking connector.sync(state) from the un-advanced watermark — idempotent hash-dedupe
# skips the already-ingested docs. Backoff doubles from 20s and we keep retrying for up to an
# hour after the first network failure; after the budget is spent the job parks in a real
# paused state (thread blocked on the gate) until a manual Resume (which restarts the budget)
# or Stop. Only network-classified errors retry — auth/config/code errors fail immediately.
_RETRY_INITIAL_BACKOFF = 20.0  # seconds; first wait after a network failure (then 40, 80, …)
_RETRY_MAX_WINDOW = 3600.0  # keep auto-retrying for up to this long after the first failure


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_event() -> threading.Event:
    e = threading.Event()
    e.set()
    return e


@dataclass
class SyncJob:
    id: str
    source_name: str
    source_id: str
    state: str = "running"  # running | paused | retrying | stopping | stopped | done | error
    kind: str = "sync"  # sync | cleanup (purge, no re-pull) | reset | drain (graph-only)
    clean: bool = False
    cleanup_on_stop: bool = False
    # A paused sync reconstructed after a process restart (see revive_paused): no live worker
    # holds it, so resume must re-spawn from the watermark rather than releasing a gate.
    cold: bool = False
    logs: list[str] = field(default_factory=list)
    stats: dict[str, Any] | None = None
    error: str | None = None
    ingested: int = 0
    phase: str = ""  # current stage label, e.g. "work items · Team A"
    phase_done: int | None = None  # progress within the phase, when a total is known
    phase_total: int | None = None
    started_at: str = field(default_factory=_now)
    ended_at: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    # Pause gate: SET means "proceed". Cleared to pause; the worker blocks on it before
    # the next document AND between graph-extraction batches, so a pause holds both the
    # long external pull and the deferred LLM triple-drain tail. `stop` also sets it, so a
    # paused worker wakes and then sees `cancel`. Starts set (a fresh job runs immediately).
    gate: threading.Event = field(default_factory=lambda: _set_event())
    subscribers: list[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def percent(self) -> int | None:
        """Estimated completion of the current phase, when the connector reported a total.
        Streaming pulls with no known total report a stage but no %."""
        if self.phase_total and self.phase_total > 0 and self.phase_done is not None:
            return max(0, min(100, round(100 * self.phase_done / self.phase_total)))
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source_name, "state": self.state,
            "kind": self.kind, "clean": self.clean, "stats": self.stats, "error": self.error,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "phase": self.phase, "phase_done": self.phase_done,
            "phase_total": self.phase_total, "percent": self.percent(),
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

    def recent(self, hours: int = 24) -> list[dict[str, Any]]:
        """Sync runs from the last `hours`, newest first — the notification feed.

        Reads the persisted `sync_events` window (so it survives a server restart, unlike
        the in-memory job map) and overlays the live job state where this process still
        owns the run. A row left 'running' by a process that died is reported honestly as
        `interrupted` rather than as a sync that is still going."""
        now = datetime.now(timezone.utc)
        cat = self.ctx.catalog
        try:
            cat.prune_sync_events((now - timedelta(days=_HISTORY_RETENTION_DAYS)).isoformat())
            rows = cat.list_sync_events((now - timedelta(hours=hours)).isoformat())
        except Exception:  # noqa: BLE001 — the feed is never worth failing a request over
            return [j.summary() for j in self._jobs.values()]

        live = {j.id: j for j in self._jobs.values()}
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for r in rows:
            seen.add(r["id"])
            job = live.get(r["id"])
            if job is not None:
                out.append({**job.summary(), "live": True})
                continue
            stale = r["state"] in ("running", "paused", "stopping", "retrying")
            out.append({
                "id": r["id"], "source": r["source"],
                "state": "interrupted" if stale else r["state"],
                "kind": r.get("kind") or "sync",
                "clean": bool(r["clean"]),
                "stats": json.loads(r["stats_json"]) if r["stats_json"] else None,
                "error": r["error"] or None,
                "started_at": r["started_at"], "ended_at": r["ended_at"],
                "log_lines": 0, "live": False,
            })
        # An in-flight job whose start predates the window (e.g. a sync paused before the
        # laptop was closed for days, then revived on restart) won't be in `rows` — surface it
        # anyway so the UI never loses a resumable/active job to the retention horizon.
        extra = [j.summary() | {"live": True} for j in self._jobs.values()
                 if j.id not in seen and j.state in ("running", "paused", "stopping", "retrying")]
        extra.sort(key=lambda s: s["started_at"], reverse=True)
        return extra + out

    def _record(self, job: SyncJob) -> None:
        """Persist the job's current state to the 24h history. Best-effort: the history
        is an observability nicety and must never break or fail a sync."""
        try:
            self.ctx.catalog.record_sync_event(
                job.id, job.source_name, job.state, job.clean,
                job.started_at, job.ended_at, job.stats, job.error, job.kind,
            )
        except Exception:  # noqa: BLE001
            pass

    def is_running(self, source_name: str) -> bool:
        """True when a job owns this source — including a paused or auto-retrying one. Guards
        double-start and delete/cleanup: a paused/retrying sync still holds the source, it's
        just not moving."""
        j = self._jobs.get(source_name)
        return bool(j and j.state in ("running", "paused", "stopping", "retrying"))

    def active_sources(self) -> list[str]:
        """Every source with a job currently in flight (running/paused/stopping/retrying)."""
        return sorted(j.source_name for j in self._jobs.values()
                      if j.state in ("running", "paused", "stopping", "retrying"))

    def revive_paused(self) -> int:
        """Reconstruct deliberately-paused syncs left by a previous process, so pause→resume
        survives the worker being killed / the laptop being closed in between. Call once at
        startup.

        A killed process loses the in-memory generator, so a revived pause can't literally
        continue — instead it is re-attached as a **cold** paused job that, on Resume, re-runs
        the connector from the un-advanced watermark (idempotent dedupe skips already-ingested
        docs). It does NOT auto-resume: the pause was deliberate, so it waits for the user.
        Every other unfinished row (a run that died mid-flight, or a paused one whose config is
        gone) is finalized as `interrupted` so it becomes terminal history and isn't re-examined
        on the next startup. Best-effort — a failure here must never block server startup."""
        try:
            rows = self.ctx.catalog.list_unfinished_syncs()
        except Exception:  # noqa: BLE001
            return 0
        revived = 0
        with self._lock:
            for r in rows:
                name, kind = r["source"], (r.get("kind") or "sync")
                source = next((s for s in self.ctx.config.sources if s.name == name), None)
                if r["state"] == "paused" and kind == "sync" and source is not None \
                        and name not in self._jobs:
                    job = SyncJob(id=r["id"], source_name=name,
                                  source_id=f"{source.type}:{source.name}", state="paused",
                                  kind=kind, cold=True, started_at=r["started_at"])
                    job.gate.clear()  # held until the user resumes
                    job.phase = "paused before shutdown — resume to continue"
                    self._jobs[name] = job
                    revived += 1
                else:
                    try:  # close the dangling row so it stops showing as unfinished
                        self.ctx.catalog.record_sync_event(
                            r["id"], name, "interrupted", bool(r["clean"]),
                            r["started_at"], _now(), None, r["error"] or None, kind)
                    except Exception:  # noqa: BLE001
                        pass
        return revived

    # -- control --------------------------------------------------------------
    def start(self, source_name: str, clean: bool = False) -> SyncJob:
        with self._lock:
            if self.is_running(source_name):
                raise RuntimeError(f"A sync is already running for {source_name!r}")
            if self.is_running(RESET_SOURCE):
                raise RuntimeError("A memory reset is running — wait for it to finish")
            if self.is_running(DRAIN_SOURCE):
                raise RuntimeError("A relationship drain is running — wait for it to finish")
            source = next((s for s in self.ctx.config.sources if s.name == source_name), None)
            if source is None:
                raise KeyError(f"No configured source {source_name!r}")
            self._counter += 1
            from quickjoiner.connectors.registry import create_connector

            connector = create_connector(source, self.ctx.workspace)
            # The suffix keeps ids unique across process restarts — the counter alone
            # would collide in the persisted history after a restart.
            job = SyncJob(id=f"sync-{self._counter}-{uuid.uuid4().hex[:8]}", source_name=source_name,
                          source_id=connector.source_id, clean=clean)
            self._jobs[source_name] = job
        self._record(job)
        threading.Thread(target=self._run, args=(job, source, connector), daemon=True).start()
        return job

    def start_cleanup(self, source_name: str, source_id: str) -> SyncJob:
        """Forget everything a source taught us — documents, vectors, FTS rows, the graph
        edges they proved, orphaned graph nodes, and the incremental watermark — without
        re-pulling. Runs as a normal job so it streams logs and lands in the activity feed.

        `source_id` is passed in rather than looked up: cleanup must also work for a source
        whose config has just been deleted (that is the case that leaves orphans behind)."""
        with self._lock:
            if self.is_running(source_name):
                raise RuntimeError(f"A job is already running for {source_name!r}")
            if self.is_running(RESET_SOURCE):
                raise RuntimeError("A memory reset is running — wait for it to finish")
            if self.is_running(DRAIN_SOURCE):
                raise RuntimeError("A relationship drain is running — wait for it to finish")
            self._counter += 1
            job = SyncJob(id=f"cleanup-{self._counter}-{uuid.uuid4().hex[:8]}",
                          source_name=source_name, source_id=source_id, kind="cleanup")
            self._jobs[source_name] = job
        self._record(job)
        threading.Thread(target=self._run_cleanup, args=(job,), daemon=True).start()
        return job

    def _run_cleanup(self, job: SyncJob) -> None:
        try:
            self._log(job, f"🧹 cleaning up {job.source_name!r} — removing learned data…")
            self._purge(job)
            # The `sources` row is deliberately left alone: a still-configured connector
            # must stay listed (with zero documents, like a freshly added one), and for a
            # deleted connector `save_config` has already reconciled the row away.
            self._log(job, "✓ cleanup complete.")
            job.state = "done"
        except Exception as exc:  # noqa: BLE001
            job.state = "error"
            job.error = str(exc)
            self._log(job, f"✗ cleanup failed: {exc}")
        finally:
            job.ended_at = _now()
            self._record(job)
            self._close(job)

    def start_reset(self) -> SyncJob:
        """Wipe ALL ingested knowledge as a background job — so it streams logs and lands in
        the activity feed / history exactly like a sync or cleanup, instead of a silent,
        invisible operation. Refuses while ANY job is in flight (it clears every source)."""
        with self._lock:
            active = self.active_sources()
            if active:
                raise RuntimeError(f"A job is running ({', '.join(active)}) — wait before resetting memory")
            self._counter += 1
            job = SyncJob(id=f"reset-{self._counter}-{uuid.uuid4().hex[:8]}",
                          source_name=RESET_SOURCE, source_id="", kind="reset")
            self._jobs[RESET_SOURCE] = job
        self._record(job)
        threading.Thread(target=self._run_reset, args=(job,), daemon=True).start()
        return job

    def _run_reset(self, job: SyncJob) -> None:
        try:
            self._log(job, "🧨 resetting all learned memory…")
            counts = self.ctx.catalog.reset_knowledge()
            self._log(job, f"🧹 removed {counts['documents']} documents, "
                           f"{counts['entities']} entities, {counts['edges']} graph edges")
            self.ctx.store.reset()
            self._log(job, "🧹 cleared all vectors + the full-text index")
            # Stats stay None (the sync-shaped {added,updated,…} doesn't fit a reset); the
            # counts live in the log line, and the UI renders reset by its `kind`.
            self._log(job, "✓ reset complete — the workspace is back to a clean state. Connectors kept.")
            job.state = "done"
        except Exception as exc:  # noqa: BLE001
            job.state = "error"
            job.error = str(exc)
            self._log(job, f"✗ reset failed: {exc}")
        finally:
            job.ended_at = _now()
            self._record(job)
            self._close(job)

    def start_drain(self, source_id: str | None = None) -> SyncJob:
        """Mine the relationships of documents whose connector will never re-provide them
        (AI_ROADMAP #25) — see `IngestPipeline.drain_pending_graph` for what that means and
        how faithfully it can be done. Runs as an ordinary job (live log, activity feed,
        pause/stop) because on a real corpus it is thousands of LLM calls, not a click.

        Refuses while any other job is in flight, and syncs refuse while it runs: a drain
        rewrites edges for documents across every source, so overlapping with a sync of one
        of those sources would have two writers replacing the same document's edges."""
        if not getattr(self.ctx.pipeline, "extracts_triples", False):
            raise RuntimeError(
                "LLM relationship extraction is off — enable graph.extract_triples "
                "(Settings → Knowledge graph) before draining"
            )
        with self._lock:
            active = self.active_sources()
            if active:
                raise RuntimeError(
                    f"A job is running ({', '.join(active)}) — wait before draining relationships"
                )
            self._counter += 1
            job = SyncJob(id=f"drain-{self._counter}-{uuid.uuid4().hex[:8]}",
                          source_name=DRAIN_SOURCE, source_id=source_id or "", kind="drain")
            self._jobs[DRAIN_SOURCE] = job
        self._record(job)
        threading.Thread(target=self._run_drain, args=(job,), daemon=True).start()
        return job

    def _run_drain(self, job: SyncJob) -> None:
        control = self._build_control(job)
        try:
            pending = self.ctx.catalog.count_graph_pending(job.source_id or None)
            self._log(job, f"🔗 mining relationships for {pending} document(s) "
                           "whose graph work never landed…")
            stats = self.ctx.pipeline.drain_pending_graph(
                source_id=job.source_id or None, control=control,
                log=lambda line: self._log(job, line),
            )
            job.stats = {
                "documents": stats.documents, "faithful": stats.faithful,
                "text_only": stats.text_only, "missing_text": stats.missing_text,
                "orphans": stats.orphans, "errors": stats.errors[:20],
            }
            job.ingested = stats.documents
            if stats.text_only:
                # Say which documents could only be partially rebuilt rather than
                # reporting a number that reads like a full recovery.
                self._log(job, f"ℹ {stats.text_only} document(s) predate the stored "
                               "deterministic payload — re-mined from their indexed text only")
            if stats.missing_text:
                self._log(job, f"ℹ {stats.missing_text} document(s) have no stored text "
                               "left to read — left queued, not guessed at")
            self._log(job, f"✓ {stats.summary()}")
            job.state = "done"
        except SyncStopped:
            job.state = "stopped"
            self._log(job, "■ stopped — undrained documents stay queued for the next run")
        except Exception as exc:  # noqa: BLE001
            job.state = "error"
            job.error = str(exc)
            self._log(job, f"✗ drain failed: {exc}")
        finally:
            job.ended_at = _now()
            self._record(job)
            self._close(job)

    def pause(self, source_name: str) -> SyncJob:
        """Hold a running sync in place. It stops after the current document (and between
        graph-extraction batches); already-committed work stays. Resume continues the same
        in-memory run — the connector's iterator keeps its position, so nothing re-pulls."""
        job = self._jobs.get(source_name)
        if job is None or job.state != "running":
            raise RuntimeError(f"No running sync to pause for {source_name!r}")
        job.state = "paused"
        job.gate.clear()
        self._log(job, "⏸ pause requested — holding after the current document")
        self._record(job)
        return job

    def resume(self, source_name: str) -> SyncJob:
        job = self._jobs.get(source_name)
        if job is None or job.state != "paused":
            raise RuntimeError(f"No paused sync to resume for {source_name!r}")
        if job.cold:
            return self._resume_cold(job)  # revived after a restart — re-spawn a worker
        job.state = "running"
        job.gate.set()  # wakes the worker blocked in _wait_if_paused
        self._log(job, "▶ resume requested")
        self._record(job)
        return job

    def _resume_cold(self, job: SyncJob) -> SyncJob:
        """Resume a sync that was paused in an earlier process (the laptop was closed / the
        server restarted between pause and resume). No live worker holds it, so this re-runs
        the connector from the un-advanced watermark on a fresh thread — idempotent hash-dedupe
        skips the docs the earlier run already committed. It's the same re-pull model as the
        network auto-retry; `clean=False` so an original clean sync (whose purge already ran
        before it paused) never re-purges."""
        with self._lock:
            if not job.cold:  # a concurrent resume already re-spawned the worker
                raise RuntimeError(f"A sync is already resuming for {job.source_name!r}")
            source = next((s for s in self.ctx.config.sources if s.name == job.source_name), None)
            if source is None:
                job.state, job.error, job.ended_at = "error", "source no longer configured", _now()
                self._record(job)
                self._jobs.pop(job.source_name, None)
                raise RuntimeError(f"{job.source_name!r} is no longer configured")
            from quickjoiner.connectors.registry import create_connector

            connector = create_connector(source, self.ctx.workspace)
            job.source_id = connector.source_id
            job.cold = False
            job.clean = False
            job.cancel = threading.Event()
            job.gate = _set_event()
            job.state = "running"
            job.phase, job.phase_done, job.phase_total = "", None, None
        self._record(job)
        self._log(job, "▶ resuming after restart — re-running from the last checkpoint "
                       "(already-ingested items are skipped)")
        threading.Thread(target=self._run, args=(job, source, connector), daemon=True).start()
        return job

    def stop(self, source_name: str, cleanup: bool = False) -> SyncJob:
        job = self._jobs.get(source_name)
        if job is None or job.state not in ("running", "paused", "stopping", "retrying"):
            raise RuntimeError(f"No running sync for {source_name!r}")
        if job.cold:
            # A revived pause has no worker to signal — finalize it here (there is no partial
            # in-memory pull; `cleanup` still purges what the earlier run had committed).
            job.cancel.set()
            job.state = "stopped"
            if cleanup:
                self._purge(job)
            job.ended_at = _now()
            self._log(job, "⏹ paused sync discarded"
                      + (" — cleaned up its partial data." if cleanup else "."))
            self._record(job)
            self._close(job)
            return job
        job.cleanup_on_stop = cleanup
        job.state = "stopping"
        job.cancel.set()
        job.gate.set()  # a paused/retrying worker must wake to observe the cancel
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
                if job.state in ("running", "paused", "stopping", "retrying"):
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

    def _build_control(self, job: SyncJob) -> SyncControl:
        """One SyncControl per job, shared by the connector's inner loops, the document
        loop, and the graph-extraction drain — so pause/stop is honored in all three and
        stage/% reports funnel to one place. `check()` raises SyncStopped on cancel and
        blocks on pause; `stage()` records the phase and logs it on change."""

        def wait_while_paused() -> None:
            if not job.gate.is_set() and not job.cancel.is_set():
                self._log(job, f"⏸ paused — {job.ingested} document(s) ingested, waiting to resume")
                job.gate.wait()
                if not job.cancel.is_set():
                    self._log(job, "▶ resumed")

        def on_stage(name: str, done: int | None, total: int | None) -> None:
            changed = name != job.phase
            job.phase, job.phase_done, job.phase_total = name, done, total
            if changed:
                extra = f" ({done}/{total})" if total else ""
                self._log(job, f"▸ {name}{extra}")

        return SyncControl(is_cancelled=job.cancel.is_set,
                           wait_while_paused=wait_while_paused, on_stage=on_stage)

    def _tracked(self, job: SyncJob, control: SyncControl, docs: Iterator) -> Iterator:
        for doc in docs:
            control.check()  # raises SyncStopped on cancel; blocks while paused
            job.ingested += 1
            if job.ingested <= 3 or job.ingested % 25 == 0:
                self._log(job, f"  · {job.ingested}: {getattr(doc, 'title', '')[:72]}")
            yield doc

    def _progress_line(self, job: SyncJob) -> str:
        """A truthful 'where are we now' for the heartbeat. Once the document pull finishes and
        the pipeline moves into the graph-relationships drain (or any other non-yielding phase),
        `job.ingested` stops changing — so leading with the doc count alone reads as "stuck on
        the same document". When the connector/pipeline is reporting a phase (`job.phase`, e.g.
        `graph relationships 56/3091` or `work items · Team A`), surface *that* + its progress,
        which is what's actually advancing; fall back to the running doc count only when no phase
        is being reported."""
        if job.phase:
            pct = job.percent()
            prog = f" {job.phase_done}/{job.phase_total}" if job.phase_total else ""
            pctstr = f" · {pct}%" if pct is not None else ""
            return f"{job.ingested} documents · {job.phase}{prog}{pctstr}"
        return f"still syncing — {job.ingested} documents so far"

    def _heartbeat(self, job: SyncJob, stop: threading.Event) -> None:
        """Emit a progress line on a timer so long connector-internal phases (walking many empty
        teams before the first document, or draining thousands of graph relationships after the
        pull) don't look hung — and report what's *actually* moving (see `_progress_line`), not a
        frozen doc count. Stays quiet-but-honest while paused rather than claiming progress."""
        start = time.monotonic()
        while not stop.wait(_HEARTBEAT_SECONDS):
            if job.state == "retrying":
                continue  # the auto-retry loop logs its own "retrying in Ns" countdown
            if not job.gate.is_set():
                self._log(job, f"⏸ paused — {job.ingested} documents ingested, waiting to resume")
            else:
                elapsed = int(time.monotonic() - start)
                self._log(job, f"⏳ {self._progress_line(job)} ({elapsed}s)")

    def _pull_with_auto_retry(self, job: SyncJob, connector: Any, control: SyncControl):
        """Run one pull+ingest, auto-retrying on a transient NETWORK failure.

        A raised exception kills the connector's generator (generators can't resume past a
        raise), so a retry re-invokes `connector.sync(state)` from the un-advanced watermark;
        idempotent hash-dedupe skips the docs already committed by the failed attempt. Backoff
        doubles from 20s and we keep retrying for up to an hour after the first failure. Once
        that budget is spent the job parks in a real paused state (`_enter_network_hold`) until
        a manual Resume — which restarts the whole budget — or Stop. Only network-classified
        errors retry; SyncStopped (manual stop) and any non-network error propagate at once."""
        backoff = _RETRY_INITIAL_BACKOFF
        window_start: float | None = None  # monotonic time of the first failure in this budget
        while True:
            if job.cancel.is_set():
                raise SyncStopped()
            state = {} if job.clean else self.ctx.catalog.get_sync_state(job.source_id)
            self._log(job, f"▶ syncing {job.source_name!r}…")
            try:
                stats = self.ctx.pipeline.ingest(
                    self._tracked(job, control, connector.sync(state)), job.source_id, control=control,
                )
                # The state dict comes back too: a connector may have written an opaque
                # cursor into it (OneDrive's learned-item bookkeeping, Graph deltaLinks),
                # and only the caller knows the run finished cleanly enough to commit it.
                return stats, state
            except SyncStopped:
                raise  # manual stop — not a network failure
            except Exception as exc:  # noqa: BLE001
                if not is_transient_network_error(exc):
                    raise  # auth/config/code error — fails identically however long we wait
                now = time.monotonic()
                if window_start is None:
                    window_start = now
                elapsed = now - window_start
                if elapsed >= _RETRY_MAX_WINDOW:
                    self._enter_network_hold(job, exc)  # blocks until manual resume or stop
                    if job.cancel.is_set():
                        raise SyncStopped()
                    window_start, backoff = None, _RETRY_INITIAL_BACKOFF  # resume = fresh budget
                    continue
                wait = min(backoff, _RETRY_MAX_WINDOW - elapsed)
                job.state = "retrying"
                job.phase, job.phase_done, job.phase_total = "auto-retry after network error", None, None
                self._log(job, f"⚠ network error during sync: {exc}")
                self._log(job, f"⏸ auto-paused — retrying in {int(wait)}s "
                               f"(network down {int(elapsed)}s of a {int(_RETRY_MAX_WINDOW / 60)}min budget)")
                self._record(job)
                if job.cancel.wait(timeout=wait):  # a manual Stop interrupts the backoff at once
                    raise SyncStopped()
                backoff *= 2
                job.state = "running"
                self._record(job)
                continue

    def _enter_network_hold(self, job: SyncJob, exc: Exception) -> None:
        """The 1-hour auto-retry budget is spent and the network still hasn't recovered: park
        the job in a genuine paused state (thread blocked on the gate) with clear logs, rather
        than failing it. `resume()` (state=='paused') wakes it to retry with a fresh budget;
        `stop()` sets the gate + cancel so it wakes to abandon the run."""
        job.state = "paused"
        job.gate.clear()
        job.phase, job.phase_done, job.phase_total = "network unavailable", None, None
        self._log(job, f"✗ network still unavailable after auto-retrying for "
                       f"{int(_RETRY_MAX_WINDOW / 60)} minutes: {exc}")
        self._log(job, "⏸ sync paused — the network hasn't recovered. Resume to retry from where "
                       "it left off, or Stop to abandon this run.")
        self._record(job)
        # A Stop racing the clear above sets cancel+gate; re-check so we never block on a gate
        # that has already been released for cancellation.
        if not job.cancel.is_set():
            job.gate.wait()  # released by resume() (retry) or stop() (abandon)
        if not job.cancel.is_set():
            self._log(job, "▶ resume requested — restarting the sync and the retry budget")

    def _run(self, job: SyncJob, source: Any, connector: Any) -> None:
        hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, args=(job, hb_stop), daemon=True).start()
        try:
            self.ctx.catalog.upsert_source(job.source_id, source.name, source.type, source.options)
            control = self._build_control(job)
            connector._control = control  # inner loops (e.g. TFS teams/sprints) honor stop/pause
            if job.clean:
                self._log(job, "🧹 clean sync — purging existing data first…")
                self._purge(job)
            # Timestamp the watermark from before the FIRST attempt so a run that spanned an
            # outage + retries can't miss items created while the network was down.
            started = _now()
            stats, pulled_state = self._pull_with_auto_retry(job, connector, control)

            # A stop that arrived exactly as the pull finished (no SyncStopped raised) is
            # still a stop — the watermark must not advance on a partial run.
            if job.cancel.is_set():
                raise SyncStopped()
            self.ctx.catalog.set_sync_state_many(job.source_id, pulled_state)
            self.ctx.catalog.set_sync_state(job.source_id, "since", started)
            job.stats = {"added": stats.added, "updated": stats.updated,
                         "skipped": stats.skipped, "chunks": stats.chunks,
                         "errors": len(stats.errors), "ingested": job.ingested}
            job.phase, job.phase_done, job.phase_total = "", None, None
            self._log(job, f"✓ done — {stats.summary()}")
            self._autogenerate(job, source)
            job.state = "done"
        except SyncStopped:
            job.state = "stopped"
            job.phase, job.phase_done, job.phase_total = "", None, None
            # A stopped run still did real work, and it is the run you most want throughput
            # from (a long crawl someone interrupted). Recording what it actually ingested
            # is the difference between a measurable partial run and a blank history row.
            job.stats = {"ingested": job.ingested, "partial": True}
            if job.cleanup_on_stop:
                self._purge(job)
            self._log(job, f"⏹ sync stopped — {job.ingested} document(s) ingested.")
        except Exception as exc:  # noqa: BLE001 — surface any failure on the job, never crash the thread
            job.state = "error"
            job.error = str(exc)
            job.stats = {"ingested": job.ingested, "partial": True}
            self._log(job, f"✗ error: {exc}")
        finally:
            hb_stop.set()
            job.ended_at = _now()
            self._record(job)
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
