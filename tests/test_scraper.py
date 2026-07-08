"""Web scraper connector: extraction, link scoping, and crawl behavior (no network)."""

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.browser.scraper import (
    WebScrapeConnector,
    default_prefixes,
    extract_links,
    page_document,
)
from quickjoiner.connectors.registry import create_connector

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
