"""Playwright persistent browser profile per workspace.

`qj browser login <url>` opens a real Chromium window; the user signs in once
(SSO/MFA included) and closes it. Cookies/session state persist in the profile,
so connectors can afterwards fetch authenticated pages headlessly.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

PROFILE_DIRNAME = "browser_profile"

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


def has_profile(workspace: Path) -> bool:
    return profile_dir(workspace).exists() and any(profile_dir(workspace).iterdir())


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    return sync_playwright


def login(workspace: Path, url: str) -> None:
    """Open a headed Chromium window on `url`; block until the user closes it."""
    sync_playwright = _sync_playwright()
    profile = profile_dir(workspace)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), headless=False, **_CONTEXT_OPTS
        )
        context.add_init_script(_STEALTH_JS)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)
        try:
            context.wait_for_event("close", timeout=0)  # user closes the window
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass  # already closed by the user


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
        try:
            yield context
        finally:
            context.close()


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
