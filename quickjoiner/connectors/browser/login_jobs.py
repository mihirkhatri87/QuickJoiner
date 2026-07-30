"""Interactive browser sign-in, driven from the web UI.

`qj browser login` is a blocking CLI flow: it opens a real Chromium window and waits for
the user to sign in. The UI needs the same thing without blocking an HTTP request, so a
login runs on a daemon thread and the browser polls its state.

**The window opens on the machine running the server, not the one viewing the UI.** For a
local-first workspace those are the same machine and this is seamless; on a headless host
(Docker, a remote server) there is no display and the job says so plainly instead of
hanging — `display_hint()` checks for that up front rather than after a 30s timeout.

Deliberately narrow by design: the URL is taken from the **connector's own configuration**,
never from the request, so this endpoint cannot be used to make the server open an arbitrary
page. One login at a time per connector.
"""

from __future__ import annotations

import os
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
    started_at: str = field(default_factory=_now)
    ended_at: str | None = None

    def summary(self) -> dict:
        return {
            "source": self.source, "url": self.url, "state": self.state,
            "message": self.message, "signed_in": self.signed_in, "hosts": self.hosts,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "active": self.state in ("opening", "waiting", "verifying"),
        }


_jobs: dict[str, LoginJob] = {}
_lock = threading.Lock()


def display_hint() -> str | None:
    """Why a headed browser cannot open here, or None if it can.

    Only Linux is checked: it is the one platform where a server realistically runs with no
    display at all (Docker), and where a headed launch would otherwise fail obscurely."""
    if os.name == "nt" or os.sys.platform == "darwin":  # type: ignore[attr-defined]
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    return (
        "This server has no display, so it cannot open a sign-in window. Run "
        "`qj browser login <url>` on a machine with a desktop that shares this workspace."
    )


def status(source_name: str) -> LoginJob | None:
    return _jobs.get(source_name)


def start(workspace: Path, source_name: str, url: str) -> LoginJob:
    """Open a sign-in window for `url` on a background thread. Raises RuntimeError when one
    is already open for this connector or the host has no display."""
    hint = display_hint()
    if hint:
        raise RuntimeError(hint)
    with _lock:
        existing = _jobs.get(source_name)
        if existing and existing.summary()["active"]:
            raise RuntimeError(f"A sign-in window is already open for {source_name!r}")
        job = LoginJob(source=source_name, url=url)
        _jobs[source_name] = job
    threading.Thread(target=_run, args=(job, workspace), daemon=True).start()
    return job


def _run(job: LoginJob, workspace: Path) -> None:
    from quickjoiner.connectors.browser.session import login, verify_session

    try:
        job.state = "waiting"
        job.message = "Sign in in the window that opened on the QuickJoiner host, then close it."

        def on_log(msg: str) -> None:
            job.message = msg

        report = login(workspace, job.url, on_log=on_log)
        job.hosts = report.get("hosts", [])
        job.state = "verifying"
        job.message = "Window closed — checking the saved session…"
        ok, detail = verify_session(workspace, job.url)
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
