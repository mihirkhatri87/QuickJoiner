"""Generic web scraper connector (last-resort mode): crawl configured URL prefixes,
extract main content, ingest. Fetches through the authenticated Playwright profile
when use_browser=true (for systems with no usable API), plain HTTP otherwise.

Options:
    start_urls:     one URL or a list — crawl roots (required)
    allow_prefixes: URL prefixes the crawler may follow (default: each start URL's folder)
    max_pages:      crawl budget (default 30)
    use_browser:    true -> fetch via the persistent browser profile (qj browser login first)
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register

DROP_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form"]
DEFAULT_MAX_PAGES = 30


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

    def _use_browser(self) -> bool:
        return str(self.options.get("use_browser", "")).lower() in {"1", "true", "yes"}

    def test(self) -> ConnectionStatus:
        starts = self._start_urls()
        if not starts:
            return ConnectionStatus(False, "No 'start_urls' configured")
        if self._use_browser():
            from quickjoiner.connectors.browser.session import has_profile

            if not has_profile(self.workspace):
                return ConnectionStatus(
                    False, "use_browser=true but no browser profile; run: qj browser login <url>"
                )
            return ConnectionStatus(True, f"Browser profile ready; {len(starts)} start URL(s)")
        try:
            resp = httpx.get(starts[0], timeout=30.0, follow_redirects=True)
            return ConnectionStatus(resp.status_code < 400, f"GET {starts[0]} -> {resp.status_code}")
        except Exception as exc:
            return ConnectionStatus(False, f"Fetch failed: {exc}")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        starts, prefixes, budget = self._start_urls(), self._allow_prefixes(), self._max_pages()
        queue = list(starts)
        visited: set[str] = set()

        if self._use_browser():
            from quickjoiner.connectors.browser.session import browser_session, fetch_html

            with browser_session(self.workspace) as context:
                yield from self._crawl(queue, visited, prefixes, budget,
                                       lambda url: fetch_html(context, url))
        else:
            yield from self._crawl(queue, visited, prefixes, budget, self._fetch_http)

    def _fetch_http(self, url: str) -> str:
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        if "html" not in resp.headers.get("content-type", "html"):
            return ""
        return resp.text

    def _crawl(self, queue, visited, prefixes, budget, fetch) -> Iterator[Document]:
        while queue and len(visited) < budget:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                html = fetch(url)
            except Exception:
                continue  # dead links shouldn't kill the crawl
            if not html:
                continue
            doc = page_document(url, html)
            if doc:
                yield doc
            for link in extract_links(url, html, prefixes):
                if link not in visited and link not in queue:
                    queue.append(link)
