"""Deterministic architectural-layer classification for code entities (AI_ROADMAP #29).

One pure function, `classify_layer(path)`, tagging a source file with the architectural
role its own path states — never inferred by an LLM (invariant I3), so it works keyless
and offline and produces the same answer on every machine.

WHAT THE CORPUS TAUGHT, and why this table is not the one the roadmap sketched
(measured 2026-08-10 on the live 15,469 `defines` paths before any of this was written):

  * The roadmap's first-named signal — path segments like /controllers, /repositories,
    /components — tags **3.3%** of real files. This codebase is organised by DOMAIN
    (customeraccounts, sales, pricing, quotes, invoices), not by layer, which is normal
    for enterprise .NET and fatal to a directory-only rule.
  * Filename conventions are four times better (**13.1%**), because .NET/Java state a
    file's role in its name: `CustomerAccountRepository.cs`.
  * Even combined and generously extended, the six layers the roadmap names reach only
    **10.4%** of production files. An attribute that sparse cannot make a graph "read as
    an architecture" — it would leave it 90% untagged.
  * The two categories that DO dominate are ones the roadmap never mentions: **test
    scaffolding is 31.9%** of all defining files (`*Tests` alone is 28.3%), and vendored
    third-party code accounts for **2,407 of 15,469** `defines` edges — a single
    `jquery-1.4.4.js` contributing hundreds of symbols like `doscrollcheck` and
    `returnfalse` as first-class org entities.

So the taxonomy here is a superset of the roadmap's: its six layers are all present and
unchanged, plus `test` and `vendor`. Separating noise from production code is what
actually makes the graph legible on a real corpus, and it is the same mechanism, the same
storage and the same evidence.

AMBIGUOUS WORDS ARE LEFT UNTAGGED, on purpose. `handler` is an HTTP handler (api) in one
codebase and a CQRS command handler (service) in the next — and this corpus has 269
`*Command` and 569 `*Event` files, so it is probably the latter here, but "probably" is
not a basis for a stored attribute that colours a graph and biases retrieval. Likewise
`model` (domain vs persistence vs view), `client`, `configuration`, and the message
shapes `event`/`command`/`request`/`response`/`dto`/`contract`, which describe data
crossing a boundary rather than a layer. Untagged is an honest answer; a wrong layer is
not, and it is invisible once stored.
"""

from __future__ import annotations

import re

# The roadmap's six, plus the two the corpus forced. Ordered most- to least-specific for
# display; membership is what matters.
LAYERS = ("vendor", "test", "api", "service", "data", "ui", "utility", "infra")

# Third-party code checked into the repo. Matched as whole path SEGMENTS or on the
# filename, never as a substring of a longer word: the live corpus contains
# `GetOrderByVendorCodeResponseServiceModel.cs`, an org file about a *vendor code*
# business concept, which a naive substring match on "vendor" claims as third-party.
_VENDOR_DIRS = {
    "node_modules", "bower_components", "vendor", "vendors", "third_party",
    "thirdparty", "third-party", "packages", "jspm_packages", "site-packages",
    "dist", "bundles", "externals",
}
# Well-known libraries that get dropped into a `Scripts/` folder in older .NET web apps,
# where the folder name alone says nothing.
_VENDOR_FILE = re.compile(
    r"^(jquery|jquery-ui|jquery\.|angular|angular-|bootstrap|modernizr|knockout|"
    r"underscore|lodash|moment|respond|require|backbone|ember|prototype|mootools|"
    r"kendo|highcharts|d3|popper|swagger-ui)[.\-]", re.I)

_TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__", "testing", "unittests",
              "integrationtests", "e2e", "fixtures", "mocks"}
_TEST_TAILS = {"tests", "test", "spec", "specs", "fixture", "fixtures", "mock", "mocks",
               "stub", "stubs", "testbase", "testhelper", "testfixture"}

# Trailing CamelCase word of the filename -> layer. Only words whose architectural role
# is unambiguous across ecosystems; see the module docstring for what was deliberately
# left out and why.
_TAIL_LAYER = {
    "controller": "api", "apicontroller": "api", "endpoint": "api", "webservice": "api",
    "service": "service", "processor": "service", "provisioner": "service",
    "orchestrator": "service", "workflow": "service", "saga": "service",
    "repository": "data", "dao": "data", "entity": "data", "dbcontext": "data",
    "migration": "data", "schema": "data",
    # `view`, `page` and `screen` are NOT here despite being obvious UI words: validated
    # against the corpus, `Model/Page.cs` is an OpenAPI pagination model and a `*View` in
    # .NET is as often a database view as an MVC one. They survive only as DIRECTORY
    # signals (`/views/`, `/pages/`), where the folder disambiguates them.
    "component": "ui", "viewmodel": "ui",
    "helper": "utility", "helpers": "utility", "extensions": "utility",
    "builder": "utility", "factory": "utility", "converter": "utility",
    "serializer": "utility", "validator": "utility",
}

# Directory segment -> layer. Weaker than the filename (see `classify_layer`), and only
# words that name a layer rather than a domain.
_DIR_LAYER = {
    "controllers": "api", "api": "api", "apis": "api", "endpoints": "api",
    "routes": "api", "handlers": "api",
    "services": "service", "domain": "service", "usecases": "service",
    "repositories": "data", "persistence": "data", "migrations": "data",
    "entities": "data", "dal": "data",
    "components": "ui", "views": "ui", "pages": "ui", "screens": "ui", "widgets": "ui",
    "helpers": "utility", "utils": "utility", "util": "utility", "extensions": "utility",
    "infrastructure": "infra", "infra": "infra", "deploy": "infra", "deployment": "infra",
    "terraform": "infra", "k8s": "infra", "kubernetes": "infra", "pipelines": "infra",
}

_UI_EXTS = {".tsx", ".jsx", ".vue", ".svelte", ".razor", ".cshtml", ".html", ".scss", ".css"}
_INFRA_EXTS = {".tf", ".tfvars", ".bicep"}
_INFRA_FILES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml", "jenkinsfile",
                "makefile", "azure-pipelines.yml", ".gitlab-ci.yml"}


def _segments(path: str) -> list[str]:
    """Path segments, ORIGINAL CASE preserved. A repo document's uri is
    `<clone-url>::<path>`, so the `::` is split too — otherwise the first real segment
    stays glued to the clone url.

    Case matters here even though every comparison below is case-insensitive: the
    filename's CamelCase is the signal `_tail_word` reads, and lowercasing at this level
    silently destroyed it (`SubscriptionInfoDenormalizerTests` became one unsplittable
    word, which collapsed test detection from a measured 31.9% to 0.5% — caught by
    validating against the real corpus, not by the unit tests, which used names short
    enough to survive)."""
    cleaned = path.split("?")[0].split("#")[0].replace("::", "/")
    return [s for s in re.split(r"[\\/]+", cleaned) if s]


def _filename(path: str) -> str:
    segs = _segments(path)
    return segs[-1] if segs else ""


def _is_test_dir(segment: str) -> bool:
    """Whether a directory segment names test code. Dot-separated parts are checked
    individually because .NET names test PROJECTS, not folders: the live corpus has
    `AppRiver.Nautical.Domain.Tests`, `AppRiver.Provisioning.UnitTests` and
    `AppRiver.Connector.Test`, none of which an exact match on "tests" would catch.
    Splitting on dots keeps that precise — a segment like `contest` has no part equal to
    a test word, so it stays untouched."""
    low = segment.lower()
    if low in _TEST_DIRS:
        return True
    return any(part in _TEST_DIRS for part in low.split("."))


def _tail_word(filename: str) -> str:
    """Trailing CamelCase word of a filename stem — how .NET/Java/TS state a file's role.
    `CustomerAccountRepository.cs` -> "repository". Falls back to the whole stem for
    lowercase names so `utils.py` still resolves."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    words = re.findall(r"[A-Z][a-z0-9]+|[A-Z]+(?![a-z])", stem)
    if words:
        return words[-1].lower()
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def _ext(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1] if "." in filename else ""


def is_vendored(path: str) -> bool:
    """Third-party code checked into the repo — its symbols are the library's, not the
    org's. Separate from `classify_layer` because callers may want to skip such files
    entirely rather than merely label them."""
    segs = _segments(path)
    if any(s.lower() in _VENDOR_DIRS for s in segs[:-1]):
        return True
    name = _filename(path).lower()
    if name.endswith(".min.js") or name.endswith(".min.css"):
        return True
    return bool(_VENDOR_FILE.match(name))


def classify_layer(path: str) -> str | None:
    """The architectural layer a file's own path states, or None when it says nothing.

    Precedence, and the reason for each step:
      1. vendor  — third-party code is not the org's architecture at all, so this wins
                   outright; a vendored file's directory and name describe the library.
      2. test    — a test for a controller is test scaffolding, not the API layer. Judged
                   before the layer rules for exactly that reason.
      3. filename — the file's own statement about its role, and measured four times more
                   often present than the directory (13.1% vs 3.3% on the live corpus).
      4. directory — deepest matching segment wins, being the most specific; a repo-level
                   `/services/` says less about a file than the folder it actually sits in.
      5. extension — `.tsx` is a UI file wherever it lives; a Dockerfile is infra.
    Anything else returns None. Most files in a real codebase have no architectural layer
    and saying so is the honest outcome.
    """
    if not path:
        return None
    if is_vendored(path):
        return "vendor"

    segs = _segments(path)
    name = _filename(path)
    tail = _tail_word(name)
    if any(_is_test_dir(s) for s in segs[:-1]) or tail in _TEST_TAILS:
        return "test"

    if tail in _TAIL_LAYER:
        return _TAIL_LAYER[tail]

    deepest = None
    for seg in segs[:-1]:
        if seg.lower() in _DIR_LAYER:
            deepest = _DIR_LAYER[seg.lower()]
    if deepest:
        return deepest

    if name.lower() in _INFRA_FILES:
        return "infra"
    ext = _ext(name.lower())
    if ext in _UI_EXTS:
        return "ui"
    if ext in _INFRA_EXTS:
        return "infra"
    return None
