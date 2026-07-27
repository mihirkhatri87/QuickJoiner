"""Ephemeral in-memory progress for ONE ad-hoc document ingest — how many chunks have
been embedded out of how many, keyed by a token the frontend mints itself.

This exists for exactly one flow: `POST /api/chat/attachments/{id}/learn`. Embedding a
large document (e.g. everything inside a zip, concatenated) can take minutes on a
CPU-only machine, and that request is otherwise a single opaque call with nothing to show
while it runs (reported live as "the system freezes for a few minutes"). Real percentage
needs the token to exist BEFORE the slow work starts so the frontend can poll concurrently
with the request that's doing the work — an HTTP response can't be sent twice, so the
token is minted client-side and passed in on the same POST, and this module is what a
second, parallel GET reads from.

Deliberately NOT the SyncManager job model: that's a background thread + persisted history
for connector syncs; this is one counter for the lifetime of a single in-flight request,
safe to lose on a restart. Thread-safe because the ingest itself runs off the event loop
via `run_in_threadpool` while the polling GET is served concurrently on the loop.
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_ROWS: dict[str, dict] = {}
_TTL_SECONDS = 300  # generous: a slow poller should still catch the final state


def start(token: str) -> None:
    with _LOCK:
        _prune_locked()
        _ROWS[token] = {"done": 0, "total": 0, "ts": time.monotonic()}


def update(token: str, done: int, total: int) -> None:
    with _LOCK:
        if token in _ROWS:
            _ROWS[token] = {"done": done, "total": total, "ts": time.monotonic()}


def get(token: str) -> dict | None:
    with _LOCK:
        _prune_locked()
        row = _ROWS.get(token)
        return {"done": row["done"], "total": row["total"]} if row else None


def _prune_locked() -> None:
    cutoff = time.monotonic() - _TTL_SECONDS
    for k in [k for k, v in _ROWS.items() if v["ts"] < cutoff]:
        _ROWS.pop(k, None)
