"""Parallel file-reading helper for the files/git connectors (ingestion speed).

Verifies it reads everything, preserves order even when reads finish out of order, skips
None, and honors cooperative stop via the stage callback — the integrity guarantees that let
it replace the old sequential read loop without changing what gets ingested."""

from __future__ import annotations

import time

import pytest

from quickjoiner.connectors.base import Document
from quickjoiner.connectors.files import read_documents_parallel, read_workers
from quickjoiner.connectors.util import prefetch_pages
from quickjoiner.sync_control import SyncStopped


def _doc(i: int) -> Document:
    return Document(uri=f"u{i}", title=f"t{i}", text=f"x{i}", kind="doc")


def test_reads_all_and_preserves_order_despite_out_of_order_completion():
    # Earlier items sleep longer, so they finish AFTER later ones — output must still be in order.
    paths = list(range(20))

    def read_fn(i):
        time.sleep((20 - i) * 0.002)  # descending delay ⇒ reverse completion order
        return _doc(i)

    out = list(read_documents_parallel(paths, read_fn, workers=8))
    assert [d.title for d in out] == [f"t{i}" for i in range(20)]


def test_none_results_are_skipped():
    paths = list(range(10))
    out = list(read_documents_parallel(paths, lambda i: _doc(i) if i % 2 == 0 else None, workers=4))
    assert [d.title for d in out] == [f"t{i}" for i in range(0, 10, 2)]


def test_sequential_and_parallel_are_equivalent():
    paths = list(range(30))
    seq = [d.title for d in read_documents_parallel(paths, _doc, workers=1)]
    par = [d.title for d in read_documents_parallel(paths, _doc, workers=8)]
    assert seq == par == [f"t{i}" for i in range(30)]


def test_stage_callback_stop_is_honored():
    paths = list(range(100))
    seen = []

    def stage(done, total):
        if done >= 5:
            raise SyncStopped()

    def gen():
        for d in read_documents_parallel(paths, _doc, stage=stage, workers=4):
            seen.append(d.title)

    with pytest.raises(SyncStopped):
        gen()
    # Stopped early — nowhere near all 100 (bounded by the small prefetch window at most).
    assert len(seen) < 100


# -- prefetch_pages (sequential paginated APIs: jira/github/gitlab/octopus-events/ado) --------

def test_prefetch_pages_yields_all_pages_in_order():
    pages = {0: ([1, 2], 1), 1: ([3, 4], 2), 2: ([5], None)}

    def fetch(cursor):
        return pages[cursor or 0]

    out = [x for page in prefetch_pages(fetch, 0) for x in page]
    assert out == [1, 2, 3, 4, 5]


def test_prefetch_pages_single_page():
    out = list(prefetch_pages(lambda c: (["only"], None)))
    assert out == [["only"]]


def test_prefetch_pages_overlaps_next_fetch_with_consumption():
    # While the caller consumes page c, page c+1's fetch runs on the worker thread — that overlap
    # IS the speedup. Proven deterministically: the next fetch signals when it starts, and we wait
    # for that signal while "consuming" the current page.
    import threading

    started = {i: threading.Event() for i in range(4)}

    def fetch(cursor):
        c = cursor or 0
        started[c].set()
        return ([c], c + 1 if c < 3 else None)

    consumed = []
    for page in prefetch_pages(fetch, 0):
        c = page[0]
        consumed.append(c)
        if c < 3:
            assert started[c + 1].wait(timeout=2.0)  # next fetch began while we're on page c
    assert consumed == [0, 1, 2, 3]


def test_prefetch_pages_checkpoint_stop_is_honored():
    seen = []

    def fetch(cursor):
        c = cursor or 0
        return ([c], c + 1 if c < 100 else None)

    def checkpoint():
        if len(seen) >= 3:
            raise SyncStopped()

    with pytest.raises(SyncStopped):
        for page in prefetch_pages(fetch, 0, checkpoint):
            seen.append(page[0])
    assert len(seen) == 3  # stopped promptly


def test_read_workers_env_override(monkeypatch):
    monkeypatch.setenv("QJ_READ_WORKERS", "3")
    assert read_workers() == 3
    monkeypatch.setenv("QJ_READ_WORKERS", "999")
    assert read_workers() == 64  # capped
    monkeypatch.delenv("QJ_READ_WORKERS", raising=False)
    assert 2 <= read_workers() <= 8  # machine-scaled default
