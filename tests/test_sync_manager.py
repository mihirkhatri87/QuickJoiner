"""SyncManager: interruptible ingest, clean/purge, stop-with-cleanup, live logs."""

from __future__ import annotations

import json
import threading
import time
import types

import httpx
import pytest

import quickjoiner.sync_manager as sm
from quickjoiner.connectors.util import is_transient_network_error
from quickjoiner.ingest.pipeline import IngestStats
from quickjoiner.sync_manager import SyncJob, SyncManager, _DONE


class _Doc:
    def __init__(self, title):
        self.title = title


class FakeCatalog:
    def __init__(self):
        self.calls = []
        self.events: dict[str, dict] = {}

    def record_sync_event(self, job_id, source, state, clean, started_at,
                          ended_at=None, stats=None, error=None, kind="sync"):
        self.events[job_id] = {
            "id": job_id, "source": source, "state": state, "clean": 1 if clean else 0,
            "stats_json": json.dumps(stats) if stats else "", "error": error or "",
            "started_at": started_at, "ended_at": ended_at, "kind": kind,
        }

    def list_sync_events(self, since, limit=50):
        rows = [r for r in self.events.values() if r["started_at"] >= since]
        return sorted(rows, key=lambda r: (r["started_at"], r["id"]), reverse=True)[:limit]

    def list_unfinished_syncs(self):
        return [r for r in self.events.values() if not r["ended_at"]]

    def prune_sync_events(self, before):
        self.calls.append("prune_sync_events")
        return 0

    def upsert_source(self, *a):
        self.calls.append("upsert_source")

    def get_sync_state(self, sid):
        return {}

    def set_sync_state(self, sid, k, v):
        self.calls.append("set_sync_state")

    def delete_documents_for_source(self, sid):
        self.calls.append("delete_documents_for_source")
        return 7

    def gc_orphan_entities(self):
        self.calls.append("gc_orphan_entities")
        return 3

    def clear_sync_state(self, sid):
        self.calls.append("clear_sync_state")

    def delete_source(self, sid):  # the catalog's `sources` row (distinct from the store's)
        self.calls.append("delete_source")

    def reset_knowledge(self, include_gaps=True):
        self.calls.append("reset_knowledge")
        return {"documents": 12, "entities": 5, "edges": 9}


class FakeStore:
    def __init__(self):
        self.calls = []

    def delete_source(self, sid):
        self.calls.append("delete_source")

    def reset(self):
        self.calls.append("reset")


class FakePipeline:
    """Consumes the document iterator (so the manager's pause/cancel-checking wrapper
    runs) and returns simple stats. Accepts the optional `control` the manager threads in
    for the deferred-graph phase; a test can drive it via `graph_batches`."""

    def __init__(self, graph_batches=0):
        self.graph_batches = graph_batches  # simulate N pausable post-ingest graph waves

    def ingest(self, docs, source_id, control=None):
        stats = IngestStats()
        for _ in docs:
            stats.added += 1
            stats.chunks += 1
        for i in range(self.graph_batches):  # the pausable/stoppable triple-drain tail
            if control is not None:
                control.stage("graph relationships", i, self.graph_batches)  # raises on cancel, blocks on pause
        return stats


class FakeConnector:
    source_id = "files:demo"

    def __init__(self, docs, gate=None):
        self._docs = docs
        self._gate = gate  # optional Event to block mid-stream for stop tests

    def sync(self, state):
        for i, d in enumerate(self._docs):
            if self._gate is not None and i == 1:
                self._gate.wait(timeout=5)  # hold after the first doc until released
            yield d


def _ctx(sources):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(sources=sources),
        workspace="/tmp/ws",
        catalog=FakeCatalog(),
        store=FakeStore(),
        pipeline=FakePipeline(),
    )


def _source(name="demo", type="files"):
    return types.SimpleNamespace(name=name, type=type, options={})


def _wait(mgr, name, states, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = mgr.job_for(name)
        if j and j.state in states:
            return j
        time.sleep(0.01)
    raise AssertionError(f"job never reached {states}")


# --------------------------------------------------------------- interruption unit

def test_tracked_stops_iterating_once_cancelled():
    from quickjoiner.sync_control import SyncStopped

    mgr = SyncManager(_ctx([]))
    job = SyncJob(id="x", source_name="demo", source_id="files:demo")
    gen = mgr._tracked(job, mgr._build_control(job), iter([_Doc("a"), _Doc("b"), _Doc("c")]))
    assert next(gen).title == "a"
    job.cancel.set()
    with pytest.raises(SyncStopped):  # cancel raises at the checkpoint before the next yield
        next(gen)


# --------------------------------------------------------------- clean start

def test_clean_start_purges_then_ingests(monkeypatch):
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("d1"), _Doc("d2")]))
    mgr = SyncManager(ctx)
    mgr.start("demo", clean=True)
    job = _wait(mgr, "demo", {"done", "error"})
    assert job.state == "done"
    assert job.stats["added"] == 2
    # purge primitives ran before ingest (clean sync)
    for c in ("delete_documents_for_source", "delete_source", "gc_orphan_entities", "clear_sync_state"):
        assert c in ctx.catalog.calls or c in ctx.store.calls


def test_double_start_same_source_conflicts(monkeypatch):
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a"), _Doc("b"), _Doc("c")], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    with pytest.raises(RuntimeError):
        mgr.start("demo")  # already running for this source
    gate.set()
    _wait(mgr, "demo", {"done"})


def test_stop_with_cleanup_purges_partial(monkeypatch):
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc(f"d{i}") for i in range(20)], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    mgr.stop("demo", cleanup=True)
    gate.set()  # let the blocked connector resume so the wrapper sees the cancel
    job = _wait(mgr, "demo", {"stopped"})
    assert job.state == "stopped"
    assert "delete_source" in ctx.store.calls  # cleanup ran on stop
    assert "set_sync_state" not in ctx.catalog.calls  # a stopped sync doesn't advance the watermark


def test_job_ids_are_unique_across_manager_instances(monkeypatch):
    """Ids key the persisted history, so a restarted process must not reuse them —
    the per-process counter alone would hand out 'sync-1' again after every restart."""
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a")]))
    ids = set()
    for _ in range(2):
        mgr = SyncManager(_ctx([_source()]))
        ids.add(mgr.start("demo").id)
        _wait(mgr, "demo", {"done"})
    assert len(ids) == 2


# --------------------------------------------------------------- stop latency

class SlowConnector:
    """Simulates a connector deep in a non-yielding phase (like TFS walking teams): it
    calls `self._control.check()` in a tight loop the way `_checkpoint()` does, so a stop
    must be observed within a couple of iterations rather than after the whole phase."""

    source_id = "files:demo"
    _control = None

    def sync(self, state):
        for _ in range(100000):
            self._control.check()  # cooperative checkpoint — raises SyncStopped on cancel
            time.sleep(0.001)
        yield _Doc("never reached")


def test_stop_is_honored_inside_a_non_yielding_connector_loop(monkeypatch):
    """The bug the user hit: a stop must reach a connector that is busy between yields.
    With cooperative checkpoints the worker halts almost immediately, not after the phase."""
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: SlowConnector())
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    mgr.stop("demo")
    t0 = time.monotonic()
    job = _wait(mgr, "demo", {"stopped"}, timeout=5.0)
    assert job.state == "stopped"
    assert time.monotonic() - t0 < 2.0  # nowhere near "10 minutes" — checkpoints see the stop


def test_pause_is_honored_inside_a_non_yielding_connector_loop(monkeypatch):
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: SlowConnector())
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    mgr.pause("demo")
    j = _wait(mgr, "demo", {"paused"}, timeout=3.0)
    assert j.state == "paused"
    time.sleep(0.1)
    assert mgr.job_for("demo").state == "paused"  # genuinely held mid-loop
    mgr.stop("demo")
    _wait(mgr, "demo", {"stopped"}, timeout=3.0)


# --------------------------------------------------------------- pause / resume

def test_pause_holds_the_document_loop_then_resume_completes(monkeypatch):
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc(f"d{i}") for i in range(6)], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})

    mgr.pause("demo")
    job = mgr.job_for("demo")
    assert job.state == "paused" and not job.gate.is_set()
    gate.set()  # let the connector past its mid-stream hold; the loop must still be gated
    time.sleep(0.15)
    assert job.state == "paused" and job.state != "done"  # pause actually holds it

    mgr.resume("demo")
    job = _wait(mgr, "demo", {"done"})
    assert job.state == "done" and job.stats["added"] == 6  # every doc ingested after resume


def test_pause_only_pauses_a_running_job():
    mgr = SyncManager(_ctx([_source()]))
    with pytest.raises(RuntimeError):
        mgr.pause("demo")  # nothing running
    with pytest.raises(RuntimeError):
        mgr.resume("demo")  # nothing paused


def test_stop_while_paused_cancels_cleanly(monkeypatch):
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc(f"d{i}") for i in range(20)], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    mgr.pause("demo")
    _wait(mgr, "demo", {"paused"})
    mgr.stop("demo")  # must wake the paused worker so it observes the cancel
    gate.set()
    job = _wait(mgr, "demo", {"stopped"})
    assert job.state == "stopped"


def test_paused_job_blocks_a_second_start(monkeypatch):
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a"), _Doc("b"), _Doc("c")], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    mgr.pause("demo")
    _wait(mgr, "demo", {"paused"})
    with pytest.raises(RuntimeError):
        mgr.start("demo")  # a paused sync still owns the source
    gate.set()
    mgr.resume("demo")
    _wait(mgr, "demo", {"done"})


def test_pause_holds_the_graph_extraction_tail(monkeypatch):
    """The long tail on a big corpus is the deferred triple drain, not the pull — pausing
    must reach it too. FakePipeline runs pausable graph 'batches' after ingest."""
    ctx = _ctx([_source()])
    ctx.pipeline = FakePipeline(graph_batches=50)
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a")]))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    # Pause as soon as we can; the graph phase should still be draining.
    for _ in range(200):
        j = mgr.job_for("demo")
        if j and j.state == "running":
            mgr.pause("demo")
            break
        time.sleep(0.005)
    j = mgr.job_for("demo")
    if j.state == "paused":
        time.sleep(0.1)
        assert mgr.job_for("demo").state == "paused"  # held mid-drain, not finished
        mgr.resume("demo")
    _wait(mgr, "demo", {"done"})


# --------------------------------------------------------------- cleanup jobs

def test_cleanup_purges_everything_without_re_pulling():
    """A cleanup forgets what a source taught us — documents (cascading graph edges),
    vectors/FTS, orphan graph nodes and the watermark — and never calls the connector.
    The `sources` row survives: a still-configured connector stays listed with zero
    documents, exactly like one that has been added but not yet synced."""
    ctx = _ctx([_source()])
    mgr = SyncManager(ctx)
    job = mgr.start_cleanup("demo", "files:demo")
    _wait(mgr, "demo", {"done", "error"})

    assert job.state == "done" and job.kind == "cleanup"
    for call in ("delete_documents_for_source", "gc_orphan_entities", "clear_sync_state"):
        assert call in ctx.catalog.calls
    assert "delete_source" not in ctx.catalog.calls  # the connector stays listed
    assert "delete_source" in ctx.store.calls  # vectors + FTS do go
    assert ctx.catalog.events[job.id]["kind"] == "cleanup"


def test_cleanup_works_for_an_already_deleted_connector():
    """The orphan case: the config is gone, so source_id can only come from the caller.
    Cleanup must still run — this is precisely when knowledge would otherwise be stranded."""
    ctx = _ctx([])  # no configured sources at all
    mgr = SyncManager(ctx)
    mgr.start_cleanup("gone", "files:gone")
    job = _wait(mgr, "gone", {"done", "error"})
    assert job.state == "done"
    assert "delete_documents_for_source" in ctx.catalog.calls


def test_cleanup_refuses_while_a_sync_is_running(monkeypatch):
    """Purging under a running ingest would race it — one job per source, always."""
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a"), _Doc("b"), _Doc("c")], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    with pytest.raises(RuntimeError):
        mgr.start_cleanup("demo", "files:demo")
    gate.set()
    _wait(mgr, "demo", {"done"})


# --------------------------------------------------------------- reset job

def test_reset_runs_as_a_job_and_wipes_via_catalog_and_store():
    ctx = _ctx([])
    mgr = SyncManager(ctx)
    job = mgr.start_reset()
    assert job.kind == "reset" and job.source_name == "all memory"
    done = _wait(mgr, "all memory", {"done", "error"})
    assert done.state == "done"
    assert "reset_knowledge" in ctx.catalog.calls and "reset" in ctx.store.calls
    # It streamed logs (the observability the synchronous reset lacked) and recorded history.
    assert any("reset complete" in line for line in done.logs)
    assert done.id in ctx.catalog.events and ctx.catalog.events[done.id]["kind"] == "reset"


def test_reset_refuses_while_a_sync_is_running(monkeypatch):
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a"), _Doc("b"), _Doc("c")], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})
    with pytest.raises(RuntimeError):
        mgr.start_reset()  # clears every source — must not race a live sync
    gate.set()
    _wait(mgr, "demo", {"done"})


def test_sync_refuses_while_a_reset_is_running(monkeypatch):
    """The mirror guard: once a reset is in flight, a new sync/cleanup can't start."""
    import quickjoiner.sync_manager as sm

    ctx = _ctx([_source()])
    # Freeze the reset mid-run so we can observe the guard.
    orig = sm.SyncManager._run_reset
    gate = threading.Event()

    def slow_reset(self, job):
        gate.wait(timeout=5)
        orig(self, job)

    monkeypatch.setattr(sm.SyncManager, "_run_reset", slow_reset)
    mgr = SyncManager(ctx)
    mgr.start_reset()
    _wait(mgr, "all memory", {"running"})
    with pytest.raises(RuntimeError):
        mgr.start("demo")
    gate.set()
    _wait(mgr, "all memory", {"done"})


# --------------------------------------------------------------- 24h history feed

def test_history_records_start_then_final_state(monkeypatch):
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a"), _Doc("b")]))
    mgr = SyncManager(ctx)
    job = mgr.start("demo")
    _wait(mgr, "demo", {"done"})
    # one row per run, not one per state change
    assert list(ctx.catalog.events) == [job.id]
    row = ctx.catalog.events[job.id]
    assert row["state"] == "done" and row["ended_at"]
    assert json.loads(row["stats_json"])["added"] == 2


def test_recent_flags_live_jobs_and_survives_restart(monkeypatch):
    gate = threading.Event()
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a"), _Doc("b")], gate=gate))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"running"})

    live = mgr.recent()
    assert [(r["source"], r["state"], r["live"]) for r in live] == [("demo", "running", True)]

    # A new manager over the same catalog = the process restarted. The run it can no
    # longer see is reported as interrupted, never as one that is still going.
    restarted = SyncManager(ctx).recent()
    assert [(r["state"], r["live"]) for r in restarted] == [("interrupted", False)]

    gate.set()
    _wait(mgr, "demo", {"done"})


def test_recent_survives_a_catalog_without_history(monkeypatch):
    """The feed is observability: a backend that can't serve it degrades to live jobs."""
    ctx = _ctx([_source()])
    ctx.catalog.list_sync_events = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table"))
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("a")]))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"done"})
    assert [r["source"] for r in mgr.recent()] == ["demo"]


# --------------------------------------------------------------- network auto-retry

class FlakyConnector:
    """Fails the first `fail_times` sync() invocations with a (by default) network error,
    then yields its documents. A raised exception kills the generator, so each retry is a
    fresh sync() call — exactly what the manager's auto-retry does."""

    source_id = "files:demo"
    _control = None

    def __init__(self, docs, fail_times=1, exc=None):
        self._docs = docs
        self._fail_times = fail_times
        self._exc = exc if exc is not None else httpx.ConnectError("network down")
        self.calls = 0

    def sync(self, state):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        for d in self._docs:
            yield d


def test_classifier_separates_network_from_request_errors():
    assert is_transient_network_error(httpx.ConnectError("x"))
    assert is_transient_network_error(httpx.ReadTimeout("x"))
    assert is_transient_network_error(TimeoutError())
    assert is_transient_network_error(OSError(110, "timed out"))  # ETIMEDOUT
    req = httpx.Request("GET", "http://x")
    assert is_transient_network_error(
        httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req)))
    # NOT transient: a request/auth/config error fails identically however long we wait.
    assert not is_transient_network_error(
        httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req)))
    assert not is_transient_network_error(ValueError("bad config"))
    assert not is_transient_network_error(OSError(2, "no such file"))


def test_network_error_auto_retries_then_recovers(monkeypatch):
    """A transient network failure mid-pull must NOT fail the sync: it auto-pauses, waits the
    backoff, then re-runs the connector from the un-advanced watermark and completes."""
    monkeypatch.setattr(sm, "_RETRY_INITIAL_BACKOFF", 0.05)
    ctx = _ctx([_source()])
    conn = FlakyConnector([_Doc("d1"), _Doc("d2")], fail_times=1)
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector", lambda s, ws: conn)
    mgr = SyncManager(ctx)
    mgr.start("demo")
    job = _wait(mgr, "demo", {"done", "error"})
    assert job.state == "done" and job.stats["added"] == 2
    assert conn.calls == 2  # failed once, retried once, succeeded
    assert any("network error" in ln for ln in job.logs)
    assert any("retrying in" in ln for ln in job.logs)
    assert "set_sync_state" in ctx.catalog.calls  # watermark advanced only after success


def test_non_network_error_fails_immediately_without_retrying(monkeypatch):
    """An auth/config/code error must fail fast — retrying for an hour would be pointless."""
    monkeypatch.setattr(sm, "_RETRY_INITIAL_BACKOFF", 0.05)
    ctx = _ctx([_source()])
    conn = FlakyConnector([_Doc("d1")], fail_times=99, exc=ValueError("bad token"))
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector", lambda s, ws: conn)
    mgr = SyncManager(ctx)
    mgr.start("demo")
    job = _wait(mgr, "demo", {"done", "error"})
    assert job.state == "error" and "bad token" in (job.error or "")
    assert conn.calls == 1  # no retry
    assert not any("retrying" in ln for ln in job.logs)
    assert "set_sync_state" not in ctx.catalog.calls  # a failed run never advances the watermark


def test_retry_budget_exhausts_into_a_resumable_pause(monkeypatch):
    """Once the 1h budget is spent the job parks in a real paused state (not error); a manual
    Resume restarts the budget and, when the network is back, the sync completes."""
    monkeypatch.setattr(sm, "_RETRY_INITIAL_BACKOFF", 0.05)
    monkeypatch.setattr(sm, "_RETRY_MAX_WINDOW", 0.0)  # first failure immediately exhausts it
    ctx = _ctx([_source()])
    conn = FlakyConnector([_Doc("d1")], fail_times=1)
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector", lambda s, ws: conn)
    mgr = SyncManager(ctx)
    mgr.start("demo")
    job = _wait(mgr, "demo", {"paused"})  # network never recovered within the budget
    assert any("network still unavailable" in ln for ln in job.logs)
    assert mgr.is_running("demo")  # a network-held job still owns the source
    mgr.resume("demo")  # network is back — retry from where it left off
    job = _wait(mgr, "demo", {"done"})
    assert job.state == "done" and job.stats["added"] == 1


def test_stop_during_a_network_hold_abandons_cleanly(monkeypatch):
    monkeypatch.setattr(sm, "_RETRY_MAX_WINDOW", 0.0)
    ctx = _ctx([_source()])
    conn = FlakyConnector([_Doc("d1")], fail_times=99)  # never recovers
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector", lambda s, ws: conn)
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"paused"})
    mgr.stop("demo")
    job = _wait(mgr, "demo", {"stopped"})
    assert job.state == "stopped"
    assert "set_sync_state" not in ctx.catalog.calls


def test_stop_interrupts_the_backoff_wait(monkeypatch):
    """A manual Stop during the countdown must cancel it at once, not wait out the backoff."""
    monkeypatch.setattr(sm, "_RETRY_INITIAL_BACKOFF", 30.0)  # long enough to observe the state
    ctx = _ctx([_source()])
    conn = FlakyConnector([_Doc("d1")], fail_times=99)
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector", lambda s, ws: conn)
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"retrying"})
    t0 = time.monotonic()
    mgr.stop("demo")
    job = _wait(mgr, "demo", {"stopped"}, timeout=5.0)
    assert job.state == "stopped" and time.monotonic() - t0 < 2.0  # nowhere near the 30s backoff


# ------------------------------------------------ heartbeat honesty

def test_heartbeat_reports_the_active_phase_not_a_frozen_doc_count():
    """The reported bug: once the pull finishes and the graph-relationships drain starts,
    `job.ingested` stops moving, so the old heartbeat kept logging 'still syncing — N documents'
    with the same N — reading as stuck on the last document. The line must instead surface the
    phase that is actually advancing."""
    mgr = SyncManager(_ctx([]))
    job = SyncJob(id="x", source_name="tfs", source_id="azure_devops:tfs")
    job.ingested = 10988
    # During the pull, before any phase is reported: the doc count is the live signal.
    assert mgr._progress_line(job) == "still syncing — 10988 documents so far"
    # Pull done, draining graph relationships: lead with the phase + its real progress/%.
    job.phase, job.phase_done, job.phase_total = "graph relationships", 56, 3091
    line = mgr._progress_line(job)
    assert "graph relationships 56/3091" in line and "2%" in line
    assert "10988 documents" in line  # still honest about the pull total, just not leading


# ------------------------------------------------ durable pause across a restart

def _seed_paused(ctx, name="demo", jid="sync-old-1"):
    """A row a previous process left behind: paused, never ended (ended_at IS NULL)."""
    ctx.catalog.record_sync_event(jid, name, "paused", False, "2020-01-01T00:00:00+00:00")
    return jid


def test_revive_reconstructs_a_paused_sync_as_cold():
    """A deliberately-paused sync must survive the process dying: on startup it's re-attached
    as a cold paused job that still owns the source and can be resumed."""
    ctx = _ctx([_source()])
    jid = _seed_paused(ctx)
    mgr = SyncManager(ctx)
    assert mgr.revive_paused() == 1
    job = mgr.job_for("demo")
    assert job is not None and job.state == "paused" and job.cold and job.id == jid
    assert mgr.is_running("demo")  # holds the source until resumed or discarded
    # Surfaces in the feed even though its start predates the 24h retention window.
    assert any(r["source"] == "demo" and r["state"] == "paused" for r in mgr.recent())


def test_cold_resume_repulls_from_watermark_and_completes(monkeypatch):
    ctx = _ctx([_source()])
    _seed_paused(ctx)
    conn = FakeConnector([_Doc("d1"), _Doc("d2")])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector", lambda s, ws: conn)
    mgr = SyncManager(ctx)
    mgr.revive_paused()
    mgr.resume("demo")  # no live worker existed — resume must re-spawn one
    job = _wait(mgr, "demo", {"done"})
    assert job.state == "done" and job.stats["added"] == 2
    assert "set_sync_state" in ctx.catalog.calls  # completed → watermark advanced


def test_cold_stop_discards_without_hanging():
    """Stopping a revived pause has no worker to signal — it must finalize directly, not
    wedge in 'stopping' forever."""
    ctx = _ctx([_source()])
    _seed_paused(ctx)
    mgr = SyncManager(ctx)
    mgr.revive_paused()
    job = mgr.stop("demo")
    assert job.state == "stopped" and not mgr.is_running("demo")


def test_revive_finalizes_a_dead_running_row_as_interrupted():
    """A run that died mid-flight (state 'running', not paused) is not resumable — it's closed
    out as interrupted so it stops showing as unfinished on later startups."""
    ctx = _ctx([_source()])
    ctx.catalog.record_sync_event("sync-dead-1", "demo", "running", False,
                                  "2020-01-01T00:00:00+00:00")
    mgr = SyncManager(ctx)
    assert mgr.revive_paused() == 0
    assert mgr.job_for("demo") is None  # not revived
    row = ctx.catalog.events["sync-dead-1"]
    assert row["state"] == "interrupted" and row["ended_at"]  # finalized


def test_revive_finalizes_a_paused_row_whose_config_is_gone():
    """A pause is only resumable while its connector still exists; otherwise it's interrupted."""
    ctx = _ctx([])  # no configured sources
    _seed_paused(ctx, name="gone", jid="sync-gone-1")
    mgr = SyncManager(ctx)
    assert mgr.revive_paused() == 0
    assert ctx.catalog.events["sync-gone-1"]["state"] == "interrupted"


def test_subscribe_replays_backlog_and_signals_done(monkeypatch):
    ctx = _ctx([_source()])
    monkeypatch.setattr("quickjoiner.connectors.registry.create_connector",
                        lambda s, ws: FakeConnector([_Doc("only")]))
    mgr = SyncManager(ctx)
    mgr.start("demo")
    _wait(mgr, "demo", {"done"})
    job, q = mgr.subscribe("demo")  # subscribing after completion replays logs + _DONE
    drained = [q.get_nowait() for _ in range(q.qsize())]
    assert any("done" in line for line in drained if line is not _DONE)
    assert drained[-1] is _DONE
