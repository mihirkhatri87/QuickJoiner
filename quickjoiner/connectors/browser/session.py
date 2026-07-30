"""Playwright persistent browser profile per workspace.

`qj browser login <url>` opens a real Chromium window; the user signs in once
(SSO/MFA included) and closes it. Cookies/session state persist in the profile,
so connectors can afterwards fetch authenticated pages headlessly.

**Session cookies need explicit capture (2026-07-30, found on a real OIDC site).**
A persistent profile only gets you the cookies Chromium writes to disk — i.e. those
carrying an explicit `Expires`. The cookie that actually authenticates you very often
has none: ASP.NET Core's `.AspNetCore.Cookies`, and most `IsPersistent=false` sign-ins,
are **session cookies**, which Chromium keeps in memory and discards when the window
closes. The symptom is brutally quiet — the user signs in correctly, the profile fills
with `Correlation`/`Nonce` handshake cookies (those *do* carry Expires), the auth cookie
is silently gone, and every later fetch lands back on the login page and is dropped as
too short. Net effect: a sync that reports "0 documents" and nothing else.

So `login()` snapshots `context.storage_state()` **while the window is still open**
(that export includes session cookies, as `expires: -1`) into `browser_state.json`, and
`browser_session()` re-injects them with `add_cookies`. Persistent-profile behaviour is
unchanged and still does the heavy lifting; this only adds back what the profile cannot
hold on its own.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

PROFILE_DIRNAME = "browser_profile"
# Cookies the profile itself cannot persist (session cookies), captured at login.
STATE_FILENAME = "browser_state.json"
# How often to snapshot state while the login window is open. The user may close the
# window at any moment and the context dies with it, so the last snapshot before that
# is what we keep — 2s bounds how much of a just-completed sign-in can be lost.
_SNAPSHOT_SECONDS = 2.0

# A current desktop-Chrome UA. Headless Chromium otherwise advertises
# "HeadlessChrome/…", which basic bot filters reject outright — the single most
# common reason a real browser still gets blocked. Keep the major version roughly
# in step with the installed Chromium.
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Realistic desktop context. Applied to both the login window and headless fetch
# so the session the user signs in with matches the one that scrapes.
_CONTEXT_OPTS = dict(
    user_agent=DESKTOP_UA,
    viewport={"width": 1280, "height": 800},
    locale="en-US",
    timezone_id="America/New_York",
)

# Undo the handful of automation tells that headless Chromium leaves in the DOM.
# This is standard hardening so legitimate automation isn't misclassified — it is
# not a CAPTCHA/anti-abuse bypass, and it will not get you past a determined WAF.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || { runtime: {} };
"""

INSTALL_HINT = (
    "Playwright is not installed. Install the browser extra first:\n"
    '  uv pip install -e ".[browser]"  (or: pip install quickjoiner[browser])\n'
    "  playwright install chromium"
)


def profile_dir(workspace: Path) -> Path:
    return workspace / PROFILE_DIRNAME


def state_file(workspace: Path) -> Path:
    return workspace / STATE_FILENAME


def save_state(workspace: Path, state: dict) -> None:
    """Persist a `storage_state()` snapshot (best-effort — never break a login on it)."""
    try:
        state_file(workspace).write_text(json.dumps(state), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def load_state(workspace: Path) -> dict | None:
    path = state_file(workspace)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def session_hosts(workspace: Path) -> list[str]:
    """Hosts the saved state holds cookies for — what `qj browser status` reports, so
    "a profile exists" can be distinguished from "you are actually signed in somewhere"."""
    state = load_state(workspace) or {}
    return sorted({c.get("domain", "").lstrip(".") for c in state.get("cookies", []) if c.get("domain")})


def has_profile(workspace: Path) -> bool:
    return profile_dir(workspace).exists() and any(profile_dir(workspace).iterdir())


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    return sync_playwright


def login(workspace: Path, url: str, on_log=None) -> dict:
    """Open a headed Chromium window on `url`; block until the user closes it.

    Returns a report: `{"hosts": [...], "cookies": n, "signed_in": bool|None}`. While the
    window is open, `storage_state()` is snapshotted every few seconds so the **session**
    cookies that authenticate most SSO sites survive the window closing (see the module
    docstring — without this the profile keeps only the handshake cookies and every later
    fetch silently lands back on the login page)."""
    say = on_log or (lambda _m: None)
    sync_playwright = _sync_playwright()
    profile = profile_dir(workspace)
    profile.mkdir(parents=True, exist_ok=True)
    snapshot: dict = {}
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), headless=False, **_CONTEXT_OPTS
        )
        context.add_init_script(_STEALTH_JS)
        closed = {"yes": False}
        context.on("close", lambda _c: closed.__setitem__("yes", True))
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url)
        except Exception:  # noqa: BLE001
            pass  # the user can still navigate manually
        announced = False
        while not closed["yes"]:
            try:
                state = context.storage_state()
            except Exception:  # noqa: BLE001
                break  # context gone — keep whatever snapshot we already have
            if state.get("cookies"):
                snapshot = state
            # Tell the user the moment the sign-in round-trip lands, so they know when it
            # is safe to close instead of guessing (guessing is what silently loses it).
            if not announced:
                try:
                    current = page.url
                except Exception:  # noqa: BLE001
                    current = ""
                if current.startswith(url.rstrip("/").rsplit("/", 1)[0]) and _target_host(url) in current:
                    if any(_target_host(url) in c.get("domain", "") and not _is_handshake(c.get("name", ""))
                           for c in snapshot.get("cookies", [])):
                        say("✓ signed in — you can close the window now.")
                        announced = True
            time.sleep(_SNAPSHOT_SECONDS)
        try:
            context.close()
        except Exception:
            pass  # already closed by the user
    if snapshot.get("cookies"):
        save_state(workspace, snapshot)
    hosts = sorted({c.get("domain", "").lstrip(".") for c in snapshot.get("cookies", [])})
    real = [c for c in snapshot.get("cookies", []) if not _is_handshake(c.get("name", ""))]
    return {"hosts": hosts, "cookies": len(snapshot.get("cookies", [])), "real_cookies": len(real)}


def _target_host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc


# OIDC/OAuth handshake crumbs: present even when sign-in never completed, so they must
# not be mistaken for a session (this is exactly what made the failure look like success).
_HANDSHAKE_PREFIXES = (
    ".AspNetCore.Correlation", ".AspNetCore.OpenIdConnect.Nonce",
    "OpenIdConnect.nonce", "OpenIdConnect.token", "state", "oauth_state", "csrf",
)


def _is_handshake(name: str) -> bool:
    return any(name.startswith(p) for p in _HANDSHAKE_PREFIXES)


@contextmanager
def browser_session(workspace: Path, headless: bool = True):
    """Yield a Playwright BrowserContext bound to the workspace's persistent profile."""
    sync_playwright = _sync_playwright()
    profile = profile_dir(workspace)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), headless=headless, **_CONTEXT_OPTS
        )
        context.add_init_script(_STEALTH_JS)
        # Re-inject the cookies the profile itself cannot hold (session cookies — see the
        # module docstring). Best-effort: a malformed/stale entry must never stop a crawl,
        # it just means we fall back to whatever the profile has.
        state = load_state(workspace)
        if state and state.get("cookies"):
            try:
                context.add_cookies(state["cookies"])
            except Exception:  # noqa: BLE001
                for cookie in state["cookies"]:  # salvage the ones that are still valid
                    try:
                        context.add_cookies([cookie])
                    except Exception:  # noqa: BLE001
                        pass
        try:
            yield context
        finally:
            context.close()


# Text that means "you are looking at a sign-in form", not at content. Deliberately a
# small, boring list: the check only ever *reports*, it never blocks a crawl, so a false
# positive costs a warning and a false negative costs nothing that wasn't already broken.
_LOGIN_MARKERS = (
    "sign in", "signin", "log in", "login", "password", "username",
    "single sign-on", "sso", "authenticate", "identity server",
)


def looks_like_login(final_url: str, requested_url: str, text: str, title: str = "") -> bool:
    """Whether a fetched page is really an auth wall (pure, so it is testable).

    Two independent signals, either sufficient: the browser ended up on a **different
    host** than the one asked for (the classic SSO redirect), or the page reads like a
    sign-in form. Host comparison alone would miss same-host login pages; text alone would
    flag a genuine page *about* authentication."""
    from urllib.parse import urlparse

    want, got = urlparse(requested_url).netloc.lower(), urlparse(final_url or "").netloc.lower()
    if got and want and got != want:
        return True
    blob = f"{title}\n{text[:1500]}".lower()
    hits = sum(1 for m in _LOGIN_MARKERS if m in blob)
    return hits >= 2 and len(text.strip()) < 2000


def verify_session(workspace: Path, url: str) -> tuple[bool, str]:
    """Fetch `url` headlessly through the saved session and say whether we got content
    or an auth wall. Returns `(ok, human-readable detail)`; never raises."""
    try:
        with browser_session(workspace, headless=True) as context:
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:  # noqa: BLE001
                    pass
                final, title = page.url, (page.title() or "")
                text = page.inner_text("body")
            finally:
                page.close()
    except RuntimeError as exc:  # playwright missing
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not load the page: {type(exc).__name__}: {str(exc)[:160]}"
    if looks_like_login(final, url, text, title):
        return False, f"landed on {final.split('?')[0]} ({title[:60]!r}) — that is a sign-in page."
    return True, f"{url} returned {len(text.strip())} characters of content ({title[:60]!r})."


def fetch_html(context, url: str, timeout_ms: int = 30000) -> str:
    """Fetch one page's rendered HTML inside an existing browser_session context."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # dynamic pages may never go idle; DOM content is enough
        return page.content()
    finally:
        page.close()
