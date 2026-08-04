"""Interactive browser sign-in, driven from the web UI.

`qj browser login` is a blocking CLI flow: it opens a real Chromium window and waits for
the user to sign in. The UI needs the same thing without blocking an HTTP request, so a
login runs on a daemon thread and the browser polls its state.

**Two sign-in paths, chosen automatically (`login_mode()`).** `"local"`: the machine running
QuickJoiner has a real display (desktop dev machine, or `qj browser login` run directly on a
box with one) — a real Chromium window opens there, exactly as before. `"remote"` (2026-07-30):
no display, but Playwright is installed — the server runs the browser headless and streams it
to the caller via Chrome DevTools Protocol screencasting (`session.login_remote`) instead, so a
person can complete the sign-in from a browser tab on their own machine. This is what makes the
feature usable in Docker/cloud, where there is no display to put a window on at all.
`"unavailable"`: the `browser` extra isn't installed — nothing can run, `display_hint()` says so.

Deliberately narrow by design: the URL is taken from the **connector's own configuration**,
never from the request, so this endpoint cannot be used to make the server open an arbitrary
page. One login at a time per connector.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LoginJob:
    source: str
    url: str
    state: str = "opening"  # opening | waiting | verifying | done | error
    message: str = ""
    signed_in: bool | None = None
    hosts: list[str] = field(default_factory=list)
    mode: str = "local"  # local | remote — which path this job is running

    started_at: str = field(default_factory=_now)
    ended_at: str | None = None

    def summary(self) -> dict:
        return {
            "source": self.source, "url": self.url, "state": self.state,
            "message": self.message, "signed_in": self.signed_in, "hosts": self.hosts,
            "mode": self.mode,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "active": self.state in ("opening", "waiting", "verifying"),
        }


_jobs: dict[str, LoginJob] = {}
_lock = threading.Lock()


def login_mode() -> str:
    """Which sign-in path this host can actually run: `"local"` (a real display exists),
    `"remote"` (no display, but Playwright is importable — screencast it instead), or
    `"unavailable"` (the `browser` extra isn't installed at all).

    Only Linux is checked for a missing display: it is the one platform where a server
    realistically runs with no display at all (Docker), and where a headed launch would
    otherwise fail obscurely."""
    if os.name == "nt" or os.sys.platform == "darwin":  # type: ignore[attr-defined]
        return "local"
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return "local"
    try:
        from quickjoiner.connectors.browser.session import _sync_playwright

        _sync_playwright()
    except RuntimeError:
        return "unavailable"
    return "remote"


def display_hint() -> str | None:
    """Why NO sign-in path at all can run here, or None if at least one can (local window or
    remote screencast). A headless host with Playwright installed is no longer a dead end —
    only a host missing the `browser` extra entirely is."""
    if login_mode() != "unavailable":
        return None
    from quickjoiner.connectors.browser.session import INSTALL_HINT

    return INSTALL_HINT


# ---------------------------------------------------------------- remote (CDP-screencast) jobs
# A remote sign-in has no OS-level "window closed" signal, so the frontend drives it explicitly:
# it streams frames out through `subscribe_frames`, sends clicks/keys in through `push_input`,
# and ends the session with `signal_done`. One `RemoteSession` per connector, alive only while
# its job runs.


@dataclass
class RemoteSession:
    frame_subscribers: list = field(default_factory=list)
    input_queue: "queue.Queue" = field(default_factory=lambda: queue.Queue(maxsize=200))
    capture_event: threading.Event = field(default_factory=threading.Event)


_remote: dict[str, RemoteSession] = {}
# Closes a frame subscriber's SSE stream — a screencast has no content worth replaying, so
# unlike `SyncManager`'s log backlog this sentinel is the only thing ever needed post-hoc.
FRAME_DONE = object()


def _broadcast_frame(source_name: str, b64_jpeg: str) -> None:
    session = _remote.get(source_name)
    if session is None:
        return
    for q in list(session.frame_subscribers):
        try:
            q.put_nowait(b64_jpeg)
        except queue.Full:
            pass  # a dropped frame is fine — the next one supersedes it, this isn't a log


def _close_remote(source_name: str) -> None:
    session = _remote.pop(source_name, None)
    if session is None:
        return
    for q in session.frame_subscribers:
        try:
            q.put_nowait(FRAME_DONE)
        except queue.Full:
            pass


def subscribe_frames(source_name: str) -> "queue.Queue | None":
    """A new bounded queue of this connector's live screencast frames, or None if no remote
    sign-in is running for it. Small maxsize deliberately: a frame is cheap to drop, the next
    one supersedes it — this is a live view, not a log with a backlog to replay."""
    session = _remote.get(source_name)
    if session is None:
        return None
    q: "queue.Queue" = queue.Queue(maxsize=5)
    session.frame_subscribers.append(q)
    return q


def push_input(source_name: str, event: dict) -> bool:
    """Queue one input event for the running remote sign-in. False if none is running."""
    session = _remote.get(source_name)
    if session is None:
        return False
    try:
        session.input_queue.put_nowait(event)
    except queue.Full:
        pass  # events piling up faster than they drain — drop rather than block the request
    return True


def signal_done(source_name: str) -> bool:
    """"Done — capture session": tell the running remote sign-in to snapshot and close.
    False if none is running."""
    session = _remote.get(source_name)
    if session is None:
        return False
    session.capture_event.set()
    return True


def status(source_name: str) -> LoginJob | None:
    return _jobs.get(source_name)


def start(workspace: Path, source_name: str, url: str,
          ignore_https_errors: bool = False) -> LoginJob:
    """Start a sign-in for `url` on a background thread — a real window if this host has a
    display, otherwise a remote streamed session. Raises RuntimeError when one is already
    open for this connector or the host can't run either path (`browser` extra not installed).

    `ignore_https_errors` mirrors the connector's `verify_tls=false`: without it an internal
    site on a private CA fails every navigation, so the sign-in view renders Chrome's own
    certificate interstitial instead of the login page."""
    mode = login_mode()
    if mode == "unavailable":
        raise RuntimeError(display_hint())
    with _lock:
        existing = _jobs.get(source_name)
        if existing and existing.summary()["active"]:
            raise RuntimeError(f"A sign-in window is already open for {source_name!r}")
        job = LoginJob(source=source_name, url=url, mode=mode)
        _jobs[source_name] = job
        if mode == "remote":
            _remote[source_name] = RemoteSession()
    threading.Thread(target=_run, args=(job, workspace, ignore_https_errors), daemon=True).start()
    return job


def _run(job: LoginJob, workspace: Path, ignore_https_errors: bool = False) -> None:
    from quickjoiner.connectors.browser.session import login, login_remote, verify_session

    try:
        job.state = "waiting"

        def on_log(msg: str) -> None:
            job.message = msg

        if job.mode == "remote":
            job.message = "Waiting for you to sign in in the remote browser view."
            remote = _remote.get(job.source)
            report = login_remote(
                workspace,
                job.url,
                on_log=on_log,
                on_frame=lambda b64: _broadcast_frame(job.source, b64),
                input_queue=remote.input_queue if remote else None,
                capture_event=remote.capture_event if remote else None,
                ignore_https_errors=ignore_https_errors,
            )
        else:
            job.message = "Sign in in the window that opened on the QuickJoiner host, then close it."
            report = login(workspace, job.url, on_log=on_log,
                           ignore_https_errors=ignore_https_errors)
        job.hosts = report.get("hosts", [])
        job.state = "verifying"
        job.message = "Session closed — checking the saved session…"
        ok, detail = verify_session(workspace, job.url, ignore_https_errors=ignore_https_errors)
        job.signed_in = ok
        job.state = "done"
        job.message = detail if ok else (
            f"{detail} Complete the sign-in until the site's own page renders before closing. "
            "If your SSO offers a Windows-integrated button, use the username+password form "
            "instead — this browser has no enterprise auth allowlist."
        )
    except RuntimeError as exc:  # playwright missing / launch refused
        job.state, job.signed_in, job.message = "error", False, str(exc)
    except Exception as exc:  # noqa: BLE001
        job.state, job.signed_in = "error", False
        job.message = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        job.ended_at = _now()
        if job.mode == "remote":
            _close_remote(job.source)
