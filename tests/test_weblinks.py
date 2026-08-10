"""Where a citation opens (agent/weblinks.py).

A document's uri is its identity, not always a browsable address. The dangerous case is
the cloned repo file: `<clone-url>::<path>` BEGINS with "https://", so anything that only
checks the scheme offers it as a link and it 404s with full confidence. These tests pin
both halves — the shapes that translate, and the ones that must produce no link at all.
"""

import pytest

from quickjoiner.agent.weblinks import DEFAULT_REF, citable_link, repo_file_url

GITLAB = "https://gitlab.otxlab.net/zix/Development/secure-cloud/appriver.connector.git"


def test_a_cloned_repo_file_becomes_a_real_blob_url():
    assert citable_link(f"{GITLAB}::Source/AppRiver.Connector/Program.cs") == (
        "https://gitlab.otxlab.net/zix/Development/secure-cloud/appriver.connector"
        f"/-/blob/{DEFAULT_REF}/Source/AppRiver.Connector/Program.cs"
    )


def test_the_composite_uri_is_never_offered_raw():
    """Regression: it starts with https:// and passed a scheme-only check, so citing a
    repo file produced a link straight to a 404."""
    link = citable_link(f"{GITLAB}::README.md")
    assert "::" not in link and link.endswith("/README.md")


@pytest.mark.parametrize("remote,expected", [
    ("https://github.com/org/repo.git", "https://github.com/org/repo/blob/HEAD/src/a.py"),
    ("git@github.com:org/repo.git", "https://github.com/org/repo/blob/HEAD/src/a.py"),
    ("https://gitlab.com/grp/sub/repo", "https://gitlab.com/grp/sub/repo/-/blob/HEAD/src/a.py"),
    ("https://bitbucket.org/t/repo.git", "https://bitbucket.org/t/repo/src/HEAD/src/a.py"),
])
def test_each_forge_uses_its_own_web_layout(remote, expected):
    assert repo_file_url(remote, "src/a.py") == expected


def test_an_unknown_host_yields_no_link_rather_than_a_guessed_path():
    """Every forge spells its blob path differently; inventing one for a host we don't
    recognise would 404 while looking authoritative."""
    assert repo_file_url("https://git.internal.corp/team/repo.git", "a.py") is None
    assert citable_link("https://git.internal.corp/team/repo.git::a.py") is None


def test_credentials_in_a_remote_never_reach_the_link():
    url = repo_file_url("https://oauth2:glpat-SECRET@gitlab.com/org/repo.git", "a.py")
    assert url is not None and "SECRET" not in url and "oauth2" not in url


def test_paths_are_encoded_but_stay_readable():
    url = repo_file_url("https://github.com/o/r.git", "src/My File (v2).cs")
    assert url.endswith("/src/My%20File%20%28v2%29.cs")


def test_an_ordinary_web_uri_passes_through():
    page = "https://appriver.atlassian.net/wiki/spaces/DEVKB/pages/1/Caffeine"
    assert citable_link(page) == page


@pytest.mark.parametrize("uri", [
    "file:///C:/Users/me/docs/handbook.md",   # blocked by browsers from an http page
    "conversation://default/abc-123",          # internal, opens nothing
    "",
    "   ",
])
def test_schemes_that_open_nothing_are_not_linked(uri):
    assert citable_link(uri) is None
