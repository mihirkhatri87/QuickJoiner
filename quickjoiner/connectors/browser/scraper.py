"""Generic web scraper connector (last-resort mode): crawl configured URL prefixes,
extract main content, ingest. Fetches through the authenticated Playwright profile
when use_browser=true (for systems with no usable API), plain HTTP otherwise.

It is built to behave like a real, polite browser client — realistic headers,
per-host rate limiting, retry-with-backoff, and robots.txt respect — and to fall
back to a real headless browser when a plain HTTP fetch is refused (the common
cause of an nginx 444 / WAF 403 is the default `python-httpx` User-Agent). It does
NOT solve CAPTCHAs, rotate IPs, or forge TLS fingerprints; a site that still
refuses a real browser has decided it does not want automated access, and that
decision is respected. Scrape only sites you are authorized to, and mind their
terms of service.

Options:
    start_urls:          one URL or a list — crawl roots (required)
    allow_prefixes:      URL prefixes the crawler may follow (default: each start URL's folder)
    max_pages:           crawl budget (default 30)
    max_depth:           link-hop limit from a start URL (start page = depth 0; links on it
                         = depth 1, ...). Unset = unlimited (budget still applies).
    use_browser:         true -> always fetch via the persistent browser profile
    fallback_to_browser: true (default) -> if plain HTTP is blocked, retry the crawl
                         via a real headless browser (needs the browser extra)
    respect_robots:      true (default) -> honor robots.txt; set false only for sites you own
    rate_limit_seconds:  minimum delay between requests to the same host (default 1.0)
    max_retries:         transient-failure retries per URL (default 3)
    user_agent:          override the browser User-Agent string
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterator
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.browser.session import DESKTOP_UA
from quickjoiner.connectors.registry import register

DROP_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form"]
DEFAULT_MAX_PAGES = 30

# Statuses that mean "come back later" — worth a backoff+retry.
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Statuses that mean "we don't serve automated clients" — retrying the same way
# is pointless; hand off to the real-browser fallback instead. 444 is nginx's
# "closed connection, no response"; 5xx CDN codes are Cloudflare's block range.
BLOCK_STATUSES = {401, 403, 429, 444, 451, 503, 520, 521, 522, 526}


def browser_headers(user_agent: str) -> dict[str, str]:
    """Headers a current desktop Chrome actually sends on a top-level navigation."""
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _truthy(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Blocked(Exception):
    """A fetch was actively refused (block status or connection dropped)."""

    def __init__(self, url: str, status: int | None):
        self.url, self.status = url, status
        super().__init__(f"{url} -> {status if status is not None else 'connection refused'}")


def page_document(url: str, html: str) -> Document | None:
    """Extract readable text from a page; None if there is nothing worth keeping."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    text = "\n".join(line.strip() for line in main.get_text("\n").splitlines() if line.strip())
    if len(text) < 80:  # navigation shells, login redirects, empty pages
        return None
    return Document(uri=url, title=title, text=text, kind="doc")


def extract_links(base_url: str, html: str, allow_prefixes: list[str]) -> list[str]:
    """Same-crawl links: absolute, deduped, fragment-stripped, within the allowlist."""
    soup = BeautifulSoup(html, "html.parser")
    seen: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, a["href"])).url
        if not url.startswith(("http://", "https://")):
            continue
        if not any(url.startswith(prefix) for prefix in allow_prefixes):
            continue
        if url not in seen:
            seen.append(url)
    return seen


def default_prefixes(start_urls: list[str]) -> list[str]:
    """Allow each start URL's own folder by default (scoped, not whole-domain)."""
    prefixes = []
    for url in start_urls:
        parsed = urlparse(url)
        folder = url if url.endswith("/") else url.rsplit("/", 1)[0] + "/"
        prefixes.append(folder if parsed.path else url.rstrip("/") + "/")
    return prefixes


@register
class WebScrapeConnector(Connector):
    type_name = "web_scrape"
    modes = Mode.SCRAPE | Mode.BROWSER

    def _start_urls(self) -> list[str]:
        raw = self.options.get("start_urls") or self.options.get("url") or []
        return [raw] if isinstance(raw, str) else list(raw)

    def _allow_prefixes(self) -> list[str]:
        raw = self.options.get("allow_prefixes") or []
        prefixes = [raw] if isinstance(raw, str) else list(raw)
        return prefixes or default_prefixes(self._start_urls())

    def _max_pages(self) -> int:
        return int(self.options.get("max_pages", DEFAULT_MAX_PAGES))

    def _max_depth(self) -> int | None:
        raw = self.options.get("max_depth")
        try:
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _use_browser(self) -> bool:
        return _truthy(self.options.get("use_browser", ""), False)

    def _fallback_to_browser(self) -> bool:
        return _truthy(self.options.get("fallback_to_browser", True), True)

    def _respect_robots(self) -> bool:
        return _truthy(self.options.get("respect_robots", True), True)

    def _rate_limit(self) -> float:
        try:
            return max(0.0, float(self.options.get("rate_limit_seconds", 1.0)))
        except (TypeError, ValueError):
            return 1.0

    def _retries(self) -> int:
        try:
            return max(0, int(self.options.get("max_retries", 3)))
        except (TypeError, ValueError):
            return 3

    def _user_agent(self) -> str:
        return str(self.options.get("user_agent") or DESKTOP_UA)

    # -- HTTP client (lazy, reused across the crawl) -------------------------
    def _get_client(self) -> httpx.Client:
        client = getattr(self, "_client", None)
        if client is None:
            kwargs = dict(
                headers=browser_headers(self._user_agent()),
                follow_redirects=True,
                timeout=30.0,
                transport=getattr(self, "_transport", None),
            )
            try:
                client = httpx.Client(http2=True, **kwargs)  # match a browser's h2
            except Exception:
                client = httpx.Client(**kwargs)  # h2 extra not installed
            self._client = client
        return client

    def _throttle(self, url: str) -> None:
        delay = self._rate_limit()
        if delay <= 0:
            return
        host = urlparse(url).netloc
        last = getattr(self, "_last_fetch", {})
        wait = delay - (time.monotonic() - last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        last[host] = time.monotonic()
        self._last_fetch = last

    def test(self) -> ConnectionStatus:
        starts = self._start_urls()
        if not starts:
            return ConnectionStatus(False, "No 'start_urls' configured")
        if self._use_browser():
            from quickjoiner.connectors.browser.session import has_profile, verify_session

            if not has_profile(self.workspace):
                return ConnectionStatus(
                    False, "use_browser=true but no browser profile; run: qj browser login <url>"
                )
            # A profile *directory* existing proves nothing — the session it holds may be
            # absent or expired, and this used to report OK for a connector that could not
            # fetch a single page (the sync then reported a bare "0 documents"). Actually
            # fetch the start URL through the session and say what came back.
            ok, detail = verify_session(self.workspace, starts[0])
            if not ok:
                return ConnectionStatus(
                    False, f"Signed-in session not working: {detail} "
                           f"Run: qj browser login {starts[0]}"
                )
            return ConnectionStatus(True, f"Signed in; {detail}")
        try:
            self._fetch_http(starts[0])
            return ConnectionStatus(True, f"GET {starts[0]} -> ok")
        except Blocked as exc:
            hint = " — will retry via a real browser on sync" if self._fallback_to_browser() else \
                   " — set fallback_to_browser=true (needs the browser extra) or use_browser=true"
            return ConnectionStatus(False, f"Blocked ({exc.status}){hint}")
        except Exception as exc:
            return ConnectionStatus(False, f"Fetch failed: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        starts, prefixes, budget = self._start_urls(), self._allow_prefixes(), self._max_pages()
        try:
            if self._use_browser() or self._should_fall_back(starts):
                yield from self._crawl_via_browser(starts, prefixes, budget)
            else:
                yield from self._crawl(list(starts), set(), prefixes, budget, self._fetch_http,
                                       self._max_depth())
        finally:
            client = getattr(self, "_client", None)
            if client is not None:
                client.close()
                self._client = None

    def _should_fall_back(self, starts: list[str]) -> bool:
        """Probe the first start URL over HTTP; if refused, switch to the browser."""
        if not self._fallback_to_browser():
            return False
        try:
            self._fetch_http(starts[0])
            return False  # HTTP works — stay on the fast path
        except Blocked:
            pass
        except Exception:
            return False  # a normal error (DNS, timeout) — browser won't help
        try:  # only fall back if Playwright is actually available
            from quickjoiner.connectors.browser.session import _sync_playwright

            _sync_playwright()
            return True
        except Exception:
            return False

    def _crawl_via_browser(self, starts, prefixes, budget) -> Iterator[Document]:
        from quickjoiner.connectors.browser.session import (
            browser_session,
            fetch_html,
            looks_like_login,
        )

        with browser_session(self.workspace) as context:
            auth_walls: list[str] = []

            def fetch(url: str) -> str:
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:  # noqa: BLE001
                        pass
                    # An expired/absent session doesn't fail — it 200s with a login page,
                    # which page_document then drops as too short. That produced a silent
                    # "0 documents" indistinguishable from an empty site. Count them so the
                    # sync can say WHY it found nothing.
                    try:
                        if looks_like_login(page.url, url, page.inner_text("body"), page.title() or ""):
                            auth_walls.append(url)
                            return ""
                    except Exception:  # noqa: BLE001
                        pass
                    return page.content()
                finally:
                    page.close()

            yield from self._crawl(list(starts), set(), prefixes, budget, fetch, self._max_depth())
            if auth_walls:
                self._stage(
                    f"⚠ {len(auth_walls)} page(s) returned a sign-in page, not content — "
                    f"the browser session has expired or was never captured. "
                    f"Run: qj browser login {starts[0]}"
                )

    def _fetch_http(self, url: str) -> str:
        """Fetch one page politely: rate-limited, retried on transient failures,
        raising Blocked on an active refusal so the caller can fall back."""
        self._throttle(url)
        client = self._get_client()
        retries = self._retries()
        last_status: int | None = None
        for attempt in range(retries + 1):
            try:
                resp = client.get(url)
            except httpx.HTTPError:
                if attempt < retries:
                    time.sleep(self._backoff(attempt))
                    continue
                raise Blocked(url, None)  # connection dropped/reset (e.g. bare 444)
            last_status = resp.status_code
            if resp.status_code < 400:
                if "html" not in resp.headers.get("content-type", "html"):
                    return ""
                return resp.text
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                time.sleep(self._backoff(attempt))
                continue
            if resp.status_code in BLOCK_STATUSES:
                raise Blocked(url, resp.status_code)
            resp.raise_for_status()
        raise Blocked(url, last_status)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0 ** attempt, 8.0) + random.uniform(0, 0.5)

    # -- robots.txt (cached per host; fetched through the same seam) ---------
    def _robots_allow(self, url: str) -> bool:
        if not self._respect_robots():
            return True
        parsed = urlparse(url)
        host = parsed.netloc
        cache = getattr(self, "_robots", {})
        if host not in cache:
            cache[host] = self._load_robots(f"{parsed.scheme}://{host}/robots.txt")
            self._robots = cache
        rules = cache[host]
        if rules is None:
            return True  # no robots.txt, or it was unreachable -> allowed
        return rules.can_fetch(self._user_agent(), url)

    def _load_robots(self, robots_url: str):
        try:
            text = self._fetch_http(robots_url)
        except Exception:
            return None  # unreachable/blocked robots.txt -> don't over-block
        if not text:
            return None
        rules = robotparser.RobotFileParser()
        rules.parse(text.splitlines())
        return rules

    def _crawl(self, starts, visited, prefixes, budget,
               fetch: Callable[[str], str], max_depth: int | None = None) -> Iterator[Document]:
        queue: list[tuple[str, int]] = [(url, 0) for url in starts]
        enqueued = set(url for url in starts)
        while queue and len(visited) < budget:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            if not self._robots_allow(url):
                continue  # disallowed by robots.txt
            try:
                html = fetch(url)
            except Exception:
                continue  # dead links / per-page blocks shouldn't kill the crawl
            if not html:
                continue
            doc = page_document(url, html)
            if doc:
                yield doc
            if max_depth is not None and depth >= max_depth:
                continue  # deep enough — don't follow this page's links
            for link in extract_links(url, html, prefixes):
                if link not in enqueued:
                    enqueued.add(link)
                    queue.append((link, depth + 1))
