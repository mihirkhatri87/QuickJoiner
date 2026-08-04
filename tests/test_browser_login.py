"""Pure-logic tests for remote (headless, polled-screenshot) browser sign-in — no real
Playwright/browser involved. Route-level guards live in tests/test_api.py; live Docker
verification (real captured frames, a real OIDC site) is manual, per docs/PRIORITIES.md —
this covers what can be tested deterministically: mode selection, the input whitelist, and
the session registry.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import Mock

from quickjoiner.connectors.browser import login_jobs
from quickjoiner.connectors.browser.session import _apply_input

# ---------------------------------------------------------------- login_mode() / display_hint()


def test_login_mode_is_local_on_windows_regardless_of_display(monkeypatch):
    monkeypatch.setattr(login_jobs.os, "name", "nt")
    assert login_jobs.login_mode() == "local"
    assert login_jobs.display_hint() is None


def test_login_mode_is_local_on_linux_with_a_display(monkeypatch):
    monkeypatch.setattr(login_jobs.os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    assert login_jobs.login_mode() == "local"


def test_login_mode_is_remote_on_headless_linux_with_playwright_importable(monkeypatch):
    monkeypatch.setattr(login_jobs.os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("quickjoiner.connectors.browser.session._sync_playwright", lambda: object())
    assert login_jobs.login_mode() == "remote"
    assert login_jobs.display_hint() is None  # a headless host is no longer a dead end


def test_login_mode_is_unavailable_when_playwright_is_not_installed(monkeypatch):
    monkeypatch.setattr(login_jobs.os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    def boom():
        raise RuntimeError("Playwright is not installed. Install the browser extra first.")

    monkeypatch.setattr("quickjoiner.connectors.browser.session._sync_playwright", boom)
    assert login_jobs.login_mode() == "unavailable"
    assert login_jobs.display_hint() is not None


# ---------------------------------------------------------------- _apply_input


def test_apply_input_dispatches_each_known_event_type():
    page = Mock()
    _apply_input(page, {"type": "mousemove", "x": 10, "y": 20})
    page.mouse.move.assert_called_once_with(10.0, 20.0)

    page = Mock()
    _apply_input(page, {"type": "mousedown", "x": 5, "y": 6, "button": "right"})
    page.mouse.move.assert_called_once_with(5.0, 6.0)
    page.mouse.down.assert_called_once_with(button="right")

    page = Mock()
    _apply_input(page, {"type": "mouseup", "button": "middle"})
    page.mouse.up.assert_called_once_with(button="middle")

    page = Mock()
    _apply_input(page, {"type": "wheel", "deltaX": 1, "deltaY": -2})
    page.mouse.wheel.assert_called_once_with(1.0, -2.0)

    page = Mock()
    _apply_input(page, {"type": "keydown", "key": "Enter"})
    page.keyboard.down.assert_called_once_with("Enter")

    page = Mock()
    _apply_input(page, {"type": "keyup", "key": "Enter"})
    page.keyboard.up.assert_called_once_with("Enter")

    page = Mock()
    _apply_input(page, {"type": "type", "text": "hello"})
    page.keyboard.insert_text.assert_called_once_with("hello")


def test_apply_input_press_sends_a_chord_as_one_press():
    page = Mock()
    _apply_input(page, {"type": "press", "key": "Control+Home"})
    page.keyboard.press.assert_called_once_with("Control+Home")


def test_apply_input_select_all_uses_the_dom_not_a_synthesized_chord():
    """Measured against real headless Chromium: a synthesized Control+A does NOT apply the
    select-all *editing command* (the selection stays collapsed), so a retype appended instead
    of replacing. Select-all therefore goes through the DOM directly."""
    page = Mock()
    _apply_input(page, {"type": "press", "key": "Control+a"})
    page.keyboard.press.assert_not_called()
    assert "select" in page.evaluate.call_args[0][0]

    page = Mock()  # the macOS spelling takes the same path
    _apply_input(page, {"type": "press", "key": "Meta+A"})
    page.keyboard.press.assert_not_called()
    page.evaluate.assert_called_once()


def test_apply_input_falls_back_to_left_button_for_an_unrecognized_value():
    page = Mock()
    _apply_input(page, {"type": "mousedown", "x": 1, "y": 1, "button": "bogus"})
    page.mouse.down.assert_called_once_with(button="left")


def test_apply_input_swallows_unknown_or_malformed_events():
    """A bad input event must not kill a live remote sign-in the caller has no way to retry."""
    page = Mock()
    _apply_input(page, {"type": "not-a-real-event"})
    _apply_input(page, {"type": "mousemove"})  # missing x/y
    _apply_input(page, {})  # missing "type" entirely
    _apply_input(page, {"type": "keydown"})  # missing "key"
    assert page.mouse.move.call_count == 0
    assert page.keyboard.down.call_count == 0


# ---------------------------------------------------------------- RemoteSession registry


def test_remote_session_registry_round_trip(monkeypatch):
    monkeypatch.setitem(login_jobs._remote, "conn", login_jobs.RemoteSession())
    session = login_jobs._remote["conn"]

    assert login_jobs.push_input("conn", {"type": "mousemove", "x": 1, "y": 1}) is True
    assert session.input_queue.get_nowait() == {"type": "mousemove", "x": 1, "y": 1}

    q = login_jobs.subscribe_frames("conn")
    assert q is not None
    login_jobs._broadcast_frame("conn", "base64data")
    assert q.get_nowait() == "base64data"

    assert login_jobs.signal_done("conn") is True
    assert session.capture_event.is_set()

    login_jobs._close_remote("conn")
    assert q.get_nowait() is login_jobs.FRAME_DONE
    assert "conn" not in login_jobs._remote


# ---------------------------------------------------------------- verify_tls / internal CA


def test_context_opts_only_ignores_cert_errors_when_asked():
    """Trusting any certificate must be opt-in per connector — a default would silently
    defeat TLS for every site QuickJoiner touches."""
    from quickjoiner.connectors.browser.session import _context_opts

    assert "ignore_https_errors" not in _context_opts()
    assert _context_opts(True)["ignore_https_errors"] is True
    assert _context_opts(True)["user_agent"] == _context_opts()["user_agent"]  # rest unchanged


def test_scraper_reads_verify_tls_as_the_inverse_of_ignore_https_errors():
    from quickjoiner.connectors.browser.scraper import WebScrapeConnector

    def conn(**options):
        return WebScrapeConnector(
            "s", {"start_urls": "https://x.test/", **options}, Path("/tmp/ws")
        )

    assert conn()._ignore_https_errors() is False  # verified by default
    assert conn(verify_tls="false")._ignore_https_errors() is True
    assert conn(verify_tls="true")._ignore_https_errors() is False


def test_verify_session_names_a_certificate_failure_instead_of_blaming_the_sign_in(monkeypatch):
    """A cert error is not a sign-in problem — reporting it as one sends the user round a
    re-sign-in loop that cannot possibly succeed (observed live against an internal site)."""
    from quickjoiner.connectors.browser import session as session_mod

    def boom(*_a, **_kw):
        raise Exception("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://internal.test/")

    monkeypatch.setattr(session_mod, "browser_session", boom)
    ok, detail = session_mod.verify_session(Path("/tmp/ws"), "https://internal.test/")
    assert ok is False
    assert "Verify TLS certificate" in detail and "ERR_CERT_AUTHORITY_INVALID" in detail


def test_remote_session_functions_report_absence_when_nothing_is_running():
    assert login_jobs.push_input("missing", {"type": "mousemove"}) is False
    assert login_jobs.subscribe_frames("missing") is None
    assert login_jobs.signal_done("missing") is False


# ---------------------------------------------------------------- start()/_run() orchestration


def test_start_wires_a_remote_session_into_login_remote_and_cleans_up(monkeypatch, tmp_path):
    """`start()` under a forced "remote" mode should hand `login_remote` the SAME queue/event
    the API's input/done routes reach through `push_input`/`signal_done`, and tear the
    RemoteSession down once the job finishes — otherwise a stale session lingers forever."""
    monkeypatch.setattr(login_jobs, "login_mode", lambda: "remote")
    captured: dict = {}

    def fake_login_remote(workspace, url, on_log=None, on_frame=None,
                          input_queue=None, capture_event=None, ignore_https_errors=False):
        captured["input_queue"] = input_queue
        captured["capture_event"] = capture_event
        captured["ignore_https_errors"] = ignore_https_errors
        on_frame("frameA")
        return {"hosts": ["gated.test"], "cookies": 3, "real_cookies": 2}

    monkeypatch.setattr("quickjoiner.connectors.browser.session.login_remote", fake_login_remote)
    monkeypatch.setattr("quickjoiner.connectors.browser.session.verify_session",
                        lambda ws, url, ignore_https_errors=False: (
                            True, "https://gated.test/ returned content"))

    job = login_jobs.start(tmp_path, "gated", "https://gated.test/", ignore_https_errors=True)
    assert job.mode == "remote"

    for _ in range(200):
        if job.state in ("done", "error"):
            break
        time.sleep(0.005)
    assert job.state == "done"
    assert job.signed_in is True
    assert job.hosts == ["gated.test"]
    assert captured["input_queue"] is not None
    assert captured["capture_event"] is not None
    # An internal-CA site is unusable without this reaching the browser — the sign-in view
    # would render Chrome's certificate interstitial instead of the login page.
    assert captured["ignore_https_errors"] is True
    assert "gated" not in login_jobs._remote  # cleaned up in _run()'s finally


def test_context_opts_carry_container_safe_chromium_flags():
    """Docker gives a container 64MB of /dev/shm and Chromium keeps renderer shared memory
    there, so a real crawl exhausts it and tabs crash — surfacing as random page failures,
    never as anything naming shared memory. Applied at the single choke point every launch
    site goes through, so no launch can miss it."""
    from quickjoiner.connectors.browser.session import _context_opts

    for opts in (_context_opts(), _context_opts(True)):
        assert "--disable-dev-shm-usage" in opts["args"]
