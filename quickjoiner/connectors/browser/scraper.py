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
    same_host_only:      true (default) -> never follow a link off the start URLs' hosts,
                         whatever allow_prefixes says (an internal app links out to the
                         trackers/repos it references; those are other systems)
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

import hashlib
import random
import re
import time
from typing import Any, Callable, Iterator
from urllib import robotparser
from urllib.parse import (
    parse_qsl,
    quote,
    urldefrag,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import httpx
from bs4 import BeautifulSoup

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.browser.session import DESKTOP_UA
from quickjoiner.connectors.registry import register
from quickjoiner.ingest.extract import render_html_table

DROP_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form"]
DEFAULT_MAX_PAGES = 30

# Query parameters that identify a referral, not a page. They never change what is
# rendered, so leaving them in crawls the same page once per campaign link. Deliberately
# short and unambiguous — a plausible-but-real parameter (`ref`, `id`, `source`) is left
# alone, because dropping one silently merges two genuinely different pages.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "dclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl",
}

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


def canonical_url(url: str) -> str:
    """One canonical spelling per page, so `visited` actually dedupes.

    Two URLs differing only in case, a default port, duplicate slashes, parameter ORDER
    or a tracking parameter address the same page — but comparing raw strings crawls each
    spelling separately, and each becomes its own document (a doc_id is
    sha256(source_id|uri)). Query VALUES are preserved exactly and no parameter is dropped
    for being "probably a filter": `?team=30` and `?team=41` are genuinely different pages,
    and merging them would lose real content — the far worse error of the two.
    """
    parsed = urlparse(urldefrag(url).url)
    scheme, host = parsed.scheme.lower(), parsed.netloc.lower()
    for default in (("http", ":80"), ("https", ":443")):
        if scheme == default[0] and host.endswith(default[1]):
            host = host.rsplit(":", 1)[0]
    pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    # quote_via=quote keeps %20 as %20 rather than rewriting it to '+', so the stored uri
    # stays the URL the user would actually paste into a browser.
    query = urlencode(sorted(pairs), quote_via=quote)
    return urlunparse((scheme, host, re.sub(r"/{2,}", "/", parsed.path) or "/",
                       parsed.params, query, ""))


def page_title(url: str, region, site_title: str) -> str:
    """The page's own heading, preferring an <h1> inside the content over <title>.

    Plenty of apps set ONE static <title> for the whole site — observed live: 200 crawled
    pages all titled "Home page - AppRiver.ContinuousDelivery", which makes every ingested
    document look identical in the document browser and contributes nothing to the
    retrieval breadcrumb. `region` is the post-DROP_TAGS content, so a brand <h1> sitting
    in a <header>/<nav> is already gone; where a site really does repeat its name in the
    body <h1>, this is no worse than the <title> it replaces.
    """
    h1 = region.find("h1") if region is not None else None
    heading = h1.get_text(" ", strip=True) if h1 else ""
    return heading or site_title or url


# Text that means "this is the server telling you it failed", not content. Every entry is
# framework boilerplate a real page would have no reason to contain verbatim — ASP.NET Core's
# Error.cshtml, classic ASP.NET's yellow screen, IIS/nginx/Apache status pages.
_ERROR_MARKERS = (
    "an error occurred while processing your request",
    "server error in '/' application",
    "runtime error",
    "internal server error",
    "service unavailable",
    "http error 5",
    "500 - ",
    "503 - ",
    "the page cannot be displayed",
    "this page isn't working",
)
# An error page is short by construction — it has no content, that is the point. A genuine
# page *about* error handling (a runbook, an API's error-code reference) has substance, so
# requiring both a marker and brevity makes a false positive need to be an error page.
_ERROR_MAX_CHARS = 1500


def looks_like_error_page(text: str, title: str = "") -> bool:
    """Whether a fetched page is a server error rather than content (pure, so testable).

    Deliberately stricter than `session.looks_like_login`, which only ever *reports*: this
    one SKIPS, and a false positive silently drops a real page. So it needs a framework
    boilerplate marker AND the brevity that makes a page contentless — and the crawl reports
    what it skipped, so an over-eager rule shows up as a suspicious count rather than as a
    thin corpus nobody questions.
    """
    body = (text or "").strip()
    if len(body) > _ERROR_MAX_CHARS:
        return False
    blob = f"{title}\n{body}".lower()
    return any(marker in blob for marker in _ERROR_MARKERS)


def page_document(url: str, html: str) -> Document | None:
    """Extract readable text from a page; None if there is nothing worth keeping."""
    soup = BeautifulSoup(html, "html.parser")
    # Read <title> before DROP_TAGS runs — it lives in <head>, but keeping the two reads
    # together makes the ordering requirement for page_title's <h1> obvious.
    site_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    for tag in soup(DROP_TAGS):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup
    # Tables are rendered as markdown pipe rows rather than flattened, for the reasons in
    # ingest/extract.py: get_text() puts every cell on its own line, which loses the column
    # a value belonged to and silently DROPS a blank cell, shifting the rest of the row.
    # Reusing that renderer keeps one implementation — this crawler does its own extraction
    # (it has already stripped DROP_TAGS and picked a content region), so without this call
    # the table work would reach every source except the scraped pages that motivated it.
    for table in main.find_all("table"):
        if table.find("table") is None:  # leaf tables only; a wrapper is page layout
            table.replace_with("\n" + render_html_table(table) + "\n")
    text = "\n".join(line.strip() for line in main.get_text("\n").splitlines() if line.strip())
    if len(text) < 80:  # navigation shells, login redirects, empty pages
        return None
    return Document(uri=url, title=page_title(url, main, site_title), text=text, kind="doc")


def allowed_hosts(start_urls: list[str]) -> set[str]:
    """The hosts a crawl may touch: exactly those its start URLs name."""
    return {h for h in (urlparse(u).netloc.lower() for u in start_urls) if h}


def extract_links(base_url: str, html: str, allow_prefixes: list[str],
                  hosts: set[str] | None = None) -> list[str]:
    """Same-crawl links: absolute, canonicalized, deduped, within the allowlist.

    `hosts` is a hard floor independent of `allow_prefixes`: a page on an internal app
    routinely links out to the trackers and repos it references (the live case: a build
    dashboard whose pages link into TFS), and following those would ingest a different
    system entirely under this connector's name. A prefix list already scopes the crawl,
    but it is user-editable and a single over-broad entry would let the crawl wander —
    so the host check is applied as well, not instead.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: list[str] = []
    for a in soup.find_all("a", href=True):
        raw = urljoin(base_url, a["href"])
        if not raw.startswith(("http://", "https://")):
            continue
        if hosts is not None and urlparse(raw).netloc.lower() not in hosts:
            continue
        url = canonical_url(raw)
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

    def _same_host_only(self) -> bool:
        return _truthy(self.options.get("same_host_only", True), True)

    def _ignore_https_errors(self) -> bool:
        """`verify_tls=false` — accept a certificate this host can't chain to a trusted root.
        Needed for an internal site issued by a private/corporate CA, which otherwise fails
        every navigation with ERR_CERT_AUTHORITY_INVALID (including the sign-in view)."""
        return not _truthy(self.options.get("verify_tls", True), True)

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
                # Same `verify_tls` the browser paths honour. Without it an internal-CA host
                # fails TLS here too — and since robots.txt is fetched through this client,
                # every host burned the full retry/backoff budget before the crawl could start.
                verify=not self._ignore_https_errors(),
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
            ok, detail = verify_session(self.workspace, starts[0],
                                        ignore_https_errors=self._ignore_https_errors())
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

        with browser_session(self.workspace,
                             ignore_https_errors=self._ignore_https_errors()) as context:
            auth_walls: list[str] = []
            # ONE page reused for the whole crawl, not a new tab per URL. Each tab is a
            # separate Chromium renderer process, and creating/destroying one per page kept a
            # pool of them alive — real memory churn in a container that has very little
            # headroom (measured: 13 processes and ~1.2GB during a crawl). Navigating a single
            # page is equivalent for our purposes: cookies live on the context, and a
            # navigation resets page-level JS state anyway.
            page = context.new_page()

            def fetch(url: str) -> str:
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

            try:
                yield from self._crawl(list(starts), set(), prefixes, budget, fetch,
                                       self._max_depth())
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass  # context teardown closes it anyway
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
        hosts = allowed_hosts(starts) if self._same_host_only() else None
        queue: list[tuple[str, int]] = [(canonical_url(url), 0) for url in starts]
        enqueued = {url for url, _ in queue}
        # Canonical URLs stop the SAME page being fetched twice; this stops two genuinely
        # different URLs that render byte-identical content becoming two documents. Live
        # case: a dashboard's tag/team filter facets (`/?tag=CP`, `/?team=20`, …) that all
        # render the same empty result list — 23 of one crawl's 200-page budget.
        seen_text: set[str] = set()
        duplicates = 0
        # A server error page is not content, but it fetches with a 200 on plenty of apps
        # (ASP.NET Core renders Error.cshtml in-place), so nothing upstream rejects it.
        # Measured on a real crawl: 363 of 728 ingested documents were the identical
        # "An error occurred while processing your request" page — answerable and citable.
        errors = 0
        first_error = ""
        while queue and len(visited) < budget:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            # A browser-rendered page can take tens of seconds, so without a checkpoint here a
            # Stop waits for the whole page; and without a stage the UI has nothing to show but
            # "syncing…" for the entire crawl. The total is the KNOWN frontier (pages seen so
            # far plus those still queued), capped at the budget — it grows as links are
            # discovered rather than pretending the page limit is a real total, and it
            # converges on the true count as the queue drains.
            self._checkpoint()
            self._stage(
                f"crawling {urlparse(url).netloc}",
                len(visited),
                min(budget, len(visited) + len(queue)),
            )
            if not self._robots_allow(url):
                continue  # disallowed by robots.txt
            try:
                html = fetch(url)
            except Exception:
                continue  # dead links / per-page blocks shouldn't kill the crawl
            if not html:
                continue
            doc = page_document(url, html)
            if doc and looks_like_error_page(doc.text, doc.title):
                errors += 1
                first_error = first_error or url
                doc = None  # still follow its links: the page failed, the site did not
            if doc:
                digest = hashlib.sha256(doc.text.encode("utf-8", errors="replace")).hexdigest()
                if digest in seen_text:
                    duplicates += 1
                else:
                    seen_text.add(digest)
                    yield doc
            if max_depth is not None and depth >= max_depth:
                continue  # deep enough — don't follow this page's links
            for link in extract_links(url, html, prefixes, hosts):
                if link not in enqueued:
                    enqueued.add(link)
                    queue.append((link, depth + 1))
        # Say what the crawl left out. Both of these were silent before, and both change how
        # you'd read the result: a truncated crawl looks like a complete one, and a pile of
        # identical-looking pages looks like a broken crawler rather than a filtered site.
        if duplicates:
            self._stage(f"skipped {duplicates} page(s) whose content duplicated another URL")
        if errors:
            # Named, never silent: a crawl that quietly drops half its pages is
            # indistinguishable from a thin site, and this count is the signal that the
            # site is erroring — or that the rule above is too eager.
            self._stage(
                f"⚠ skipped {errors} page(s) that returned a server error rather than "
                f"content (first: {first_error})"
            )
        if queue and len(visited) >= budget:
            self._stage(
                f"⚠ stopped at the {budget}-page limit with {len(queue)} link(s) still queued — "
                f"raise max_pages to crawl further"
            )
