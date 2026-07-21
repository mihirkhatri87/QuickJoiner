"""Cooperative pause/stop + stage reporting for sync jobs.

Python can't kill a worker thread, so a sync only halts where the code *cooperatively*
checks — between the documents a connector yields, and, for long non-yielding stretches
(TFS walks ~11 HTTP calls per team before its first yield), wherever the connector calls
`self._checkpoint()`. `SyncControl.check()` is that point: it blocks while paused and
raises `SyncStopped` once cancelled, so a deep loop unwinds promptly instead of running to
completion. Keeping every hot loop within a few seconds of a `check()` is what bounds
stop/pause latency (target: ≤20s) — the one thing it cannot interrupt is a single external
call already in flight, which is bounded by that call's own timeout.

`stage(name, done, total)` is the reverse channel: the connector/pipeline reports which
phase it's in and how far along, so the UI can show stages and an estimated %.

This module is dependency-free (imported by `connectors.base`, `ingest.pipeline`, and
`sync_manager`) so wiring it in never risks an import cycle.
"""

from __future__ import annotations

from typing import Callable, Optional


class SyncStopped(Exception):
    """Raised by `SyncControl.check()` when the job has been cancelled. The sync worker
    catches it and records a clean stop — it is control flow, not an error."""


class SyncControl:
    def __init__(
        self,
        is_cancelled: Callable[[], bool],
        wait_while_paused: Callable[[], None],
        on_stage: Optional[Callable[[str, Optional[int], Optional[int]], None]] = None,
    ):
        self._is_cancelled = is_cancelled
        self._wait_while_paused = wait_while_paused
        self._on_stage = on_stage

    def check(self) -> None:
        """Pause/stop checkpoint. Blocks while paused; raises SyncStopped if cancelled
        (checked both before and after a pause, so a stop during a pause is honored)."""
        if self._is_cancelled():
            raise SyncStopped()
        self._wait_while_paused()
        if self._is_cancelled():
            raise SyncStopped()

    def stage(self, name: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
        """Report the current phase (+ optional progress) for the UI. Best-effort — a
        reporting failure must never break a sync — and it also runs a `check()`, so
        every stage transition is a cancellation point too."""
        if self._on_stage is not None:
            try:
                self._on_stage(name, done, total)
            except Exception:  # noqa: BLE001
                pass
        self.check()


class _NoopControl(SyncControl):
    """Default on a connector that no manager has attached a control to (direct
    construction in tests, the CLI's synchronous path). Never pauses, never cancels."""

    def __init__(self) -> None:
        super().__init__(is_cancelled=lambda: False, wait_while_paused=lambda: None)

    def check(self) -> None:  # fast path — no callbacks
        return

    def stage(self, name: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
        return


NOOP_CONTROL = _NoopControl()
