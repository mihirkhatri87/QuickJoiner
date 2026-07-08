"""Playwright persistent browser profile per workspace.

`qj browser login <url>` opens a real Chromium window; the user signs in once
(SSO/MFA included) and closes it. Cookies/session state persist in the profile,
so connectors can afterwards fetch authenticated pages headlessly.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

PROFILE_DIRNAME = "browser_profile"

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
        context = p.chromium.launch_persistent_context(str(profile), headless=False)
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
        context = p.chromium.launch_persistent_context(str(profile), headless=headless)
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
