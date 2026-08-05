"""Web scraper connector: extraction, link scoping, and crawl behavior (no network)."""

import httpx
import pytest

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.browser.scraper import (
    Blocked,
    WebScrapeConnector,
    browser_headers,
    canonical_url,
    default_prefixes,
    extract_links,
    page_document,
)
from quickjoiner.connectors.registry import create_connector


def _connector(tmp_path, **options):
    options.setdefault("start_urls", "https://site.test/")
    options.setdefault("rate_limit_seconds", 0)  # no sleeps in tests
    return create_connector(
        SourceConfig(name="s", type="web_scrape", options=options), tmp_path
    )

PAGE = """
<html><head><title>Runbook index</title></head>
<body>
<nav><a href="/nav-should-be-dropped">nav</a></nav>
<main>
<h1>Runbooks</h1>
<p>Operational runbooks for the payments platform. Start with the deploy runbook
before your first Friday release; it covers rollbacks and the smoke checklist.</p>
<a href="deploy.html">Deploy runbook</a>
<a href="deploy.html#section">Same page anchor</a>
<a href="https://elsewhere.example.org/out-of-scope">external</a>
<a href="mailto:oncall@acme.test">mail</a>
</main>
<footer>footer noise</footer>
</body></html>
"""

DEPLOY_PAGE = """
<html><head><title>Deploy runbook</title></head><body><main>
<p>Deploys run through Octopus every Friday at 2pm. To roll back, redeploy the
previous release from the dashboard and post in #deploy-help immediately.</p>
</main></body></html>
"""


def test_page_document_extracts_main_and_drops_chrome():
    doc = page_document("https://wiki.acme.test/runbooks/", PAGE)
    assert doc is not None
    # The content <h1> wins over <title>: many apps serve ONE static <title> for the whole
    # site, which makes every ingested page indistinguishable (see the title test below).
    assert doc.title == "Runbooks"
    assert "payments platform" in doc.text
    assert "nav-should-be-dropped" not in doc.text and "footer noise" not in doc.text


def test_page_title_prefers_the_page_heading_over_a_site_wide_title():
    """Observed live: 200 crawled pages of an internal dashboard ALL carried the title
    "Home page - AppRiver.ContinuousDelivery", so the document browser showed 200 identical
    rows and the retrieval breadcrumb learned nothing from any of them."""
    same_title = (
        '<html><head><title>Home page - CD</title></head><body><main><h1>{h}</h1>'
        "<p>Build history for this project, including the last ten deployments and "
        "who approved each one, plus the current pipeline status.</p></main></body></html>"
    )
    first = page_document("https://cd.test/Details?id=1", same_title.format(h="Payments API"))
    second = page_document("https://cd.test/Details?id=2", same_title.format(h="Billing API"))
    assert (first.title, second.title) == ("Payments API", "Billing API")

    # No <h1> at all -> the <title> is still used; no title either -> the url, never blank.
    plain = "<html><head><title>Only a title</title></head><body><main><p>{}</p></main></body></html>"
    assert page_document("https://cd.test/x", plain.format("y" * 200)).title == "Only a title"
    bare = "<html><body><main><p>{}</p></main></body></html>".format("z" * 200)
    assert page_document("https://cd.test/x", bare).title == "https://cd.test/x"


def test_page_title_ignores_a_brand_heading_in_the_page_chrome():
    """A site-name <h1> inside <header> must not become every page's title — DROP_TAGS
    removes the chrome before the heading is read, so the <title> is used instead."""
    html = (
        "<html><head><title>Deploy runbook</title></head><body>"
        "<header><h1>ACME Intranet</h1></header>"
        "<main><p>{}</p></main></body></html>".format("content " * 30)
    )
    assert page_document("https://acme.test/r", html).title == "Deploy runbook"


def test_page_document_skips_thin_pages():
    assert page_document("https://x.test/", "<html><body>tiny</body></html>") is None


def test_extract_links_scoped_and_deduped():
    links = extract_links(
        "https://wiki.acme.test/runbooks/", PAGE, ["https://wiki.acme.test/runbooks/"]
    )
    assert links == ["https://wiki.acme.test/runbooks/deploy.html"]


def test_canonical_url_collapses_spellings_of_the_same_page():
    base = "https://site.test/a?x=1&y=2"
    for variant in (
        "https://SITE.test/a?x=1&y=2",          # host case
        "https://site.test:443/a?x=1&y=2",      # default port
        "https://site.test//a?x=1&y=2",         # duplicate slash
        "https://site.test/a?y=2&x=1",          # parameter order
        "https://site.test/a?x=1&y=2#section",  # fragment
        "https://site.test/a?x=1&y=2&utm_source=email",  # tracking parameter
    ):
        assert canonical_url(variant) == base, variant


def test_canonical_url_keeps_what_actually_identifies_a_page():
    """Query VALUES are content: `?team=30` and `?team=41` are different pages, and
    merging them would lose real material — the worse of the two possible errors. Escaping
    is preserved as %20 (not '+') so the stored uri is the one a user could paste."""
    assert canonical_url("https://site.test/?team=30") != canonical_url("https://site.test/?team=41")
    assert canonical_url("https://site.test/?tag=Identity%20Provider") == \
        "https://site.test/?tag=Identity%20Provider"
    assert canonical_url("https://site.test") == "https://site.test/"  # empty path -> root


def test_extract_links_never_leaves_the_start_hosts():
    """An internal app links out to the trackers and repos it references (live case: a
    build dashboard linking into TFS). Those are other systems — following them would
    ingest one under the other's connector name."""
    html = (
        '<html><body><main>'
        '<a href="https://cd.test/Details?id=1">in</a>'
        '<a href="https://tfs.corp/tfs/Work/_workitems/edit/4211">out</a>'
        "</main></body></html>"
    )
    # An over-broad prefix list alone would let the crawl wander; the host floor stops it.
    wide = ["https://cd.test/", "https://tfs.corp/"]
    assert extract_links("https://cd.test/", html, wide) == [
        "https://cd.test/Details?id=1", "https://tfs.corp/tfs/Work/_workitems/edit/4211"
    ]
    assert extract_links("https://cd.test/", html, wide, {"cd.test"}) == [
        "https://cd.test/Details?id=1"
    ]


def test_crawl_drops_pages_whose_content_duplicates_another_url(tmp_path, monkeypatch):
    """Live case: a dashboard's filter facets (`/?tag=CP`, `/?team=20`, …) each render the
    identical empty result list. They are distinct URLs, so each became its own document —
    23 of one crawl's 200-page budget spent on copies of one page."""
    body = "<p>{}</p>".format("Nothing matched this filter. " * 8)
    root = (
        '<html><head><title>Home</title></head><body><main>' + body +
        '<a href="/?tag=CP">CP</a><a href="/?tag=NGP">NGP</a>'
        '<a href="/Details?id=1">Real</a></main></body></html>'
    )
    facet = "<html><head><title>Home</title></head><body><main>" + body + "</main></body></html>"
    detail = (
        "<html><head><title>Home</title></head><body><main><h1>Payments</h1>"
        "<p>{}</p></main></body></html>".format("Genuinely different page text. " * 8)
    )
    pages = {
        "https://cd.test/": root,
        "https://cd.test/?tag=CP": facet,
        "https://cd.test/?tag=NGP": facet,
        "https://cd.test/Details?id=1": detail,
    }
    connector = _connector(tmp_path, start_urls="https://cd.test/", max_pages=10)
    monkeypatch.setattr(connector, "_fetch_http", lambda url: pages[url])

    docs = list(connector.sync({}))
    # The two facets render identically, so the second is dropped; the root (whose text
    # differs — it carries the facet links) and the genuinely different detail page both
    # survive. The guarantee is EXACT content equality, deliberately: anything looser would
    # start discarding pages that merely resemble one another.
    assert [d.uri for d in docs] == [
        "https://cd.test/", "https://cd.test/?tag=CP", "https://cd.test/Details?id=1"
    ]


def test_default_prefixes_scope_to_folder():
    assert default_prefixes(["https://wiki.acme.test/runbooks/index.html"]) == [
        "https://wiki.acme.test/runbooks/"
    ]


def test_crawl_follows_links_within_budget(tmp_path, monkeypatch):
    connector = create_connector(
        SourceConfig(
            name="wiki",
            type="web_scrape",
            options={"start_urls": "https://wiki.acme.test/runbooks/", "max_pages": 10},
        ),
        tmp_path,
    )
    assert isinstance(connector, WebScrapeConnector)
    assert Mode.SCRAPE in connector.modes and Mode.BROWSER in connector.modes

    pages = {
        "https://wiki.acme.test/runbooks/": PAGE,
        "https://wiki.acme.test/runbooks/deploy.html": DEPLOY_PAGE,
    }
    monkeypatch.setattr(connector, "_fetch_http", lambda url: pages[url])

    docs = list(connector.sync({}))
    assert [d.title for d in docs] == ["Runbooks", "Deploy runbook"]
    assert all(d.kind == "doc" for d in docs)


def test_crawl_respects_max_pages(tmp_path, monkeypatch):
    connector = create_connector(
        SourceConfig(
            name="wiki",
            type="web_scrape",
            options={"start_urls": "https://wiki.acme.test/runbooks/", "max_pages": 1},
        ),
        tmp_path,
    )
    pages = {
        "https://wiki.acme.test/runbooks/": PAGE,
        "https://wiki.acme.test/runbooks/deploy.html": DEPLOY_PAGE,
    }
    monkeypatch.setattr(connector, "_fetch_http", lambda url: pages[url])
    docs = list(connector.sync({}))
    assert len(docs) == 1  # budget stops the crawl after the first page


def _chain_page(n: int, total: int) -> str:
    link = f'<a href="/p{n + 1}.html">next</a>' if n + 1 < total else ""
    return (
        f"<html><head><title>P{n}</title></head><body><main><p>"
        f"page {n} carries a comfortably long paragraph of readable body content so the "
        f"extractor keeps it well above the eighty-character thin-page threshold."
        f"</p>{link}</main></body></html>"
    )


def test_crawl_respects_max_depth(tmp_path, monkeypatch):
    connector = create_connector(
        SourceConfig(
            name="chain",
            type="web_scrape",
            options={"start_urls": "https://site.test/p0.html",
                     "allow_prefixes": "https://site.test/",
                     "max_depth": 2, "max_pages": 50, "respect_robots": "false"},
        ),
        tmp_path,
    )
    monkeypatch.setattr(
        connector, "_fetch_http",
        lambda url: _chain_page(int(url.rsplit("p", 1)[1].split(".")[0]), 6),
    )
    docs = list(connector.sync({}))
    assert [d.title for d in docs] == ["P0", "P1", "P2"]  # start=0, two hops, stop


def test_crawl_survives_dead_links(tmp_path, monkeypatch):
    connector = create_connector(
        SourceConfig(
            name="wiki",
            type="web_scrape",
            options={"start_urls": "https://wiki.acme.test/runbooks/"},
        ),
        tmp_path,
    )

    def fetch(url):
        if url.endswith("deploy.html"):
            raise RuntimeError("boom 500")
        return PAGE

    monkeypatch.setattr(connector, "_fetch_http", fetch)
    docs = list(connector.sync({}))
    assert len(docs) == 1  # the dead link is skipped, crawl continues


def _mock_connector(tmp_path, handler, **options):
    """A connector whose HTTP client is backed by an in-memory transport."""
    c = _connector(tmp_path, **options)
    c._transport = httpx.MockTransport(handler)
    return c


def test_fetch_sends_browser_headers(tmp_path):
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, text="<html><body>ok</body></html>",
                              headers={"content-type": "text/html"})

    c = _mock_connector(tmp_path, handler)
    c._fetch_http("https://site.test/page")
    assert "python-httpx" not in seen["user-agent"]
    assert "Chrome" in seen["user-agent"]
    assert seen["accept-language"].startswith("en-US")


def test_fetch_raises_blocked_on_444(tmp_path):
    c = _mock_connector(tmp_path, lambda req: httpx.Response(444), max_retries=0)
    with pytest.raises(Blocked) as exc:
        c._fetch_http("https://site.test/blocked")
    assert exc.value.status == 444


def test_fetch_retries_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="<html><body>recovered content here</body></html>",
                              headers={"content-type": "text/html"})

    c = _mock_connector(tmp_path, handler, max_retries=3)
    c._backoff = staticmethod(lambda attempt: 0)  # no real waiting
    assert "recovered" in c._fetch_http("https://site.test/flaky")
    assert calls["n"] == 3


def test_robots_disallow_skips_url(tmp_path):
    robots = "User-agent: *\nDisallow: /private/\n"
    pages = {
        "https://site.test/robots.txt": robots,
        "https://site.test/": '<html><body><main><p>public landing page with enough text to keep '
                              'it above the thin-page threshold for indexing.</p>'
                              '<a href="/private/secret.html">secret</a>'
                              '<a href="/ok.html">ok</a></main></body></html>',
        "https://site.test/ok.html": '<html><head><title>OK</title></head><body><main><p>'
                                     'an allowed page with plenty of readable content to index, '
                                     'well past the thin-page threshold so it is kept as a doc.'
                                     '</p></main></body></html>',
    }

    def fetch(url):
        return pages.get(url, "")

    c = _connector(tmp_path, start_urls="https://site.test/", respect_robots="true", max_pages=10)
    c._fetch_http = fetch  # exercised by both robots.txt load and page fetches
    titles = [d.uri for d in c._crawl(["https://site.test/"], set(),
                                      ["https://site.test/"], 10, fetch)]
    assert "https://site.test/ok.html" in titles
    assert "https://site.test/private/secret.html" not in titles


def test_browser_headers_shape():
    h = browser_headers("UA/1.0")
    assert h["User-Agent"] == "UA/1.0" and "Sec-Fetch-Mode" in h


def test_browser_mode_requires_profile(tmp_path):
    connector = create_connector(
        SourceConfig(
            name="portal",
            type="web_scrape",
            options={"start_urls": "https://portal.acme.test/", "use_browser": "true"},
        ),
        tmp_path,
    )
    status = connector.test()
    assert not status.ok and "qj browser login" in status.message


# ---------------------------------------------- auth-wall detection (2026-07-30)
# A credential-gated site does not fail a fetch: it answers 200 with a sign-in page,
# which page_document then drops as too short. That produced a silent "0 documents"
# indistinguishable from an empty site — found live against an internal OIDC app.

def test_looks_like_login_detects_the_sso_redirect_and_the_form():
    from quickjoiner.connectors.browser.session import looks_like_login

    # 1. redirected to the identity provider — a different host is sufficient on its own,
    #    even though the login page's own text is short.
    assert looks_like_login(
        "https://staffaccount.apps.example.corp/SignIn?ReturnUrl=%2Fconnect",
        "https://plumber.example.corp/",
        "STAFF LOGIN\nUse Domain Credentials\nUsername\nPassword\nSign In",
        "Identity Server",
    )
    # 2. same-host login page — caught on text instead
    assert looks_like_login(
        "https://plumber.example.corp/login", "https://plumber.example.corp/login",
        "Please sign in\nUsername\nPassword", "Login",
    )
    # 3. real content on the right host is NOT flagged...
    assert not looks_like_login(
        "https://plumber.example.corp/repos", "https://plumber.example.corp/repos",
        "Repository map\n" + "billing-api  team-payments  logs: kibana-prod\n" * 60,
        "Repository map",
    )
    # 4. ...and neither is a long genuine page that merely talks *about* authentication
    assert not looks_like_login(
        "https://plumber.example.corp/docs/auth", "https://plumber.example.corp/docs/auth",
        "How single sign-on works here. Users sign in with a username and password.\n" * 40,
        "Authentication guide",
    )


def test_login_state_roundtrip_keeps_session_cookies(tmp_path):
    """The whole point: a session cookie (expires -1) is what a persistent profile CANNOT
    keep, so it must survive save/load — otherwise sign-in silently evaporates."""
    from quickjoiner.connectors.browser.session import (
        load_state,
        save_state,
        session_hosts,
    )

    state = {"cookies": [
        {"name": ".AspNetCore.Cookies", "value": "x", "domain": "plumber.example.corp",
         "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": ".AspNetCore.Correlation.abc", "value": "y", "domain": "plumber.example.corp",
         "path": "/", "expires": 1.0, "httpOnly": True, "secure": True, "sameSite": "None"},
    ], "origins": []}
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    assert session_hosts(tmp_path) == ["plumber.example.corp"]
    assert load_state(tmp_path)["cookies"][0]["expires"] == -1  # session cookie preserved


def test_handshake_cookies_are_not_mistaken_for_a_session():
    """OIDC leaves Correlation/Nonce crumbs even when sign-in never completed — counting
    those as 'signed in' is exactly what made the failure look like success."""
    from quickjoiner.connectors.browser.session import _is_handshake

    assert _is_handshake(".AspNetCore.Correlation.7JEHBXl9QMt4roq1")
    assert _is_handshake(".AspNetCore.OpenIdConnect.Nonce.CfDJ8NGeFphj")
    assert not _is_handshake(".AspNetCore.Cookies")
    assert not _is_handshake("idsrv.session")


def test_missing_state_file_is_not_an_error(tmp_path):
    from quickjoiner.connectors.browser.session import load_state, session_hosts

    assert load_state(tmp_path) is None and session_hosts(tmp_path) == []


def test_crawl_reports_progress_and_honors_a_stop(tmp_path):
    """The crawl loop is where a browser-rendered page can cost tens of seconds, so it must
    report a stage (the UI otherwise shows a frozen "syncing…" for the whole crawl — reported
    live) and checkpoint (a Stop otherwise waits for the current page)."""
    from quickjoiner.connectors.browser.scraper import WebScrapeConnector
    from quickjoiner.sync_control import SyncStopped

    class _Ctrl:
        def __init__(self, stop_after):
            self.stop_after, self.checks, self.stages = stop_after, 0, []

        def check(self):
            self.checks += 1
            if self.checks >= self.stop_after:
                raise SyncStopped()

        def stage(self, name, done=None, total=None):
            self.stages.append((name, done, total))

    page = "<html><body>" + ("word " * 60) + "<a href='/b'>b</a></body></html>"
    conn = WebScrapeConnector(
        "s", {"start_urls": "https://site.test/", "respect_robots": "false"}, tmp_path
    )
    conn._control = _Ctrl(stop_after=99)
    docs = list(conn._crawl(["https://site.test/"], set(), (), 10, lambda _u: page, 1))
    assert docs, "the crawl should still yield documents with the control wired"
    names = [s[0] for s in conn._control.stages]
    assert any("site.test" in n for n in names), conn._control.stages
    # Denominator is the KNOWN frontier (seen + queued), never the raw page budget.
    assert all(total is not None and total <= 10 for _n, _d, total in conn._control.stages)

    stopped = WebScrapeConnector(
        "s", {"start_urls": "https://site.test/", "respect_robots": "false"}, tmp_path
    )
    stopped._control = _Ctrl(stop_after=1)
    with pytest.raises(SyncStopped):
        list(stopped._crawl(["https://site.test/"], set(), (), 10, lambda _u: page, 1))


def test_http_client_honors_verify_tls(tmp_path):
    """robots.txt is fetched through this client, so an internal-CA host burned the whole
    retry/backoff budget on every host before the crawl could even start."""
    from quickjoiner.connectors.browser.scraper import WebScrapeConnector

    strict = WebScrapeConnector("s", {"start_urls": "https://x.test/"}, tmp_path)
    assert strict._get_client()._transport is not None  # built without error
    relaxed = WebScrapeConnector(
        "s", {"start_urls": "https://x.test/", "verify_tls": "false"}, tmp_path
    )
    assert relaxed._ignore_https_errors() is True
    assert relaxed._get_client() is not None


# ---------------------------------------------------------- server error pages

ASPNET_ERROR = """<html><head><title>Error</title></head><body>
<h1 class="text-danger">Error.</h1>
<h2 class="text-danger">An error occurred while processing your request.</h2>
<p>Request ID: 00-8f2a1c9b4d-00</p>
<h3>Development Mode</h3>
<p>Swapping to the <strong>Development</strong> environment displays detailed
information about the error that occurred.</p>
</body></html>"""


def test_aspnet_error_page_is_recognised_not_ingested():
    """Measured on a real crawl: 363 of 728 ingested documents were this exact page,
    fetched with a 200 because ASP.NET Core renders it in place."""
    from quickjoiner.connectors.browser.scraper import looks_like_error_page, page_document

    doc = page_document("https://app.corp/Details?id=9", ASPNET_ERROR)
    assert doc is not None  # it is long enough to pass the 80-char content floor
    assert looks_like_error_page(doc.text, doc.title)


def test_a_real_page_about_errors_is_not_mistaken_for_one():
    """A runbook or an API error-code reference mentions these phrases legitimately. The
    brevity requirement is what separates them: an error page has no content by design."""
    from quickjoiner.connectors.browser.scraper import looks_like_error_page

    runbook = ("Handling a 500 - Internal Server Error in the payments service.\n"
               + "When the gateway reports an internal server error, first check the "
                 "circuit breaker state and the upstream health probe. " * 20)
    assert len(runbook) > 1500
    assert not looks_like_error_page(runbook, "Runbook: internal server error")


def test_an_ordinary_short_page_is_not_an_error_page():
    from quickjoiner.connectors.browser.scraper import looks_like_error_page

    assert not looks_like_error_page("Team Caffeine owns the billing service.", "Caffeine")


def test_crawl_skips_error_pages_still_follows_their_links_and_reports_the_count(tmp_path):
    from quickjoiner.connectors.browser.scraper import WebScrapeConnector

    good = ('<html><body><h1>Team Caffeine</h1><p>%s</p>'
            '<a href="https://app.corp/b">b</a></body></html>' % ("Members and duties. " * 10))
    pages = {
        "https://app.corp/a": ASPNET_ERROR.replace("</body>",
                                                   '<a href="https://app.corp/b">b</a></body>'),
        "https://app.corp/b": good,
    }
    stages: list[str] = []
    conn = WebScrapeConnector("web_scrape:t", {"start_urls": ["https://app.corp/a"],
                                               "respect_robots": False}, tmp_path)
    conn._stage = lambda name, done=None, total=None: stages.append(name)

    docs = list(conn._crawl(["https://app.corp/a"], set(), ["https://app.corp/"], 10,
                            fetch=lambda u: pages.get(u, "")))
    # The error page is dropped, but its link was still followed — the page failed, the
    # site did not.
    assert [d.uri for d in docs] == ["https://app.corp/b"]
    assert any("skipped 1 page(s) that returned a server error" in s for s in stages)
    assert any("https://app.corp/a" in s for s in stages)


def test_scraped_pages_preserve_tables_as_markdown_rows():
    """The crawler does its own extraction, so it needs the same table renderer the
    document extractor uses — otherwise the table work reaches every source EXCEPT the
    scraped pages that motivated it."""
    from quickjoiner.connectors.browser.scraper import page_document

    html = ("<html><body><h1>Autobots</h1><p>Members:</p><table>"
            "<tr><th>Name</th><th>Email</th><th>Location</th></tr>"
            "<tr><td>Caleb Spring</td><td></td><td>Dallas</td></tr></table>"
            "<p>" + ("filler " * 30) + "</p></body></html>")
    doc = page_document("https://app.corp/TeamDetails?team=Autobots", html)
    assert "| Name | Email | Location |" in doc.text
    # The blank email is preserved, so Location cannot shift into the Email column.
    assert "| Caleb Spring |  | Dallas |" in doc.text
