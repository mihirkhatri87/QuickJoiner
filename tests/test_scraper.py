"""Web scraper connector: extraction, link scoping, and crawl behavior (no network)."""

import httpx
import pytest

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.browser.scraper import (
    Blocked,
    WebScrapeConnector,
    browser_headers,
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
    assert doc.title == "Runbook index"
    assert "payments platform" in doc.text
    assert "nav-should-be-dropped" not in doc.text and "footer noise" not in doc.text


def test_page_document_skips_thin_pages():
    assert page_document("https://x.test/", "<html><body>tiny</body></html>") is None


def test_extract_links_scoped_and_deduped():
    links = extract_links(
        "https://wiki.acme.test/runbooks/", PAGE, ["https://wiki.acme.test/runbooks/"]
    )
    assert links == ["https://wiki.acme.test/runbooks/deploy.html"]


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
    assert [d.title for d in docs] == ["Runbook index", "Deploy runbook"]
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
