"""SyncManager: interruptible ingest, clean/purge, stop-with-cleanup, live logs."""

from __future__ import annotations

import threading
import time
import types

import pytest

from quickjoiner.ingest.pipeline import IngestStats
from quickjoiner.sync_manager import SyncJob, SyncManager, _DONE


class _Doc:
    def __init__(self, title):
        self.title = title


class FakeCatalog:
    def __init__(self):
        self.calls = []

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


class FakeStore:
    def __init__(self):
        self.calls = []

    def delete_source(self, sid):
        self.calls.append("delete_source")


class FakePipeline:
    """Consumes the document iterator (so the manager's cancel-checking wrapper runs)
    and returns simple stats."""

    def ingest(self, docs, source_id):
        stats = IngestStats()
        for _ in docs:
            stats.added += 1
            stats.chunks += 1
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
    mgr = SyncManager(_ctx([]))
    job = SyncJob(id="x", source_name="demo", source_id="files:demo")
    gen = mgr._tracked(job, iter([_Doc("a"), _Doc("b"), _Doc("c")]))
    assert next(gen).title == "a"
    job.cancel.set()
    with pytest.raises(StopIteration):  # cancel checked before the next yield
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
