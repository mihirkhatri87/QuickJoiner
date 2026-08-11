"""Architectural-layer classification (ingest/layers.py, AI_ROADMAP #29).

Written against what the live corpus actually contains, because a rule table invented
from taste is exactly how this feature fails: measured before it was written, the
roadmap's own suggested signals tagged 10.4% of real files, and two of the three
highest-volume categories (test scaffolding, vendored libraries) were ones it never named.
"""

from quickjoiner.ingest.code_graph import extract_code_graph
from quickjoiner.ingest.layers import LAYERS, classify_layer, is_vendored


def test_filename_role_beats_directory_and_both_beat_nothing():
    """The filename is the file's own statement about its role and was measured four
    times more often present than a layer-named directory (13.1% vs 3.3%)."""
    assert classify_layer("src/AppRiver/CustomerAccountRepository.cs") == "data"
    assert classify_layer("src/Controllers/AccountThing.cs") == "api"
    assert classify_layer("src/Services/Whatever.cs") == "service"
    # A domain-organised path with no role word anywhere says nothing, and that is the
    # honest answer for most of a real codebase.
    assert classify_layer("Source/AppRiver.Sales/Pricing/QuoteCalculation.cs") is None


def test_camel_case_is_read_from_the_original_filename_not_a_lowercased_one():
    """Regression: `_segments` lowercased every segment, so the CamelCase regex that reads
    the trailing role word matched nothing on any multi-word name. Collapsed measured test
    detection from 31.9% to 0.5% — invisible to short fixture names, caught only by
    scoring the classifier against the real corpus."""
    assert classify_layer("src/SubscriptionInfoDenormalizerTests.cs") == "test"
    assert classify_layer("src/UpdatePriceListCommandHandlerTests.cs") == "test"
    assert classify_layer("src/CustomerAccountRepository.cs") == "data"


def test_dotted_test_project_segments_are_recognised():
    """.NET names test PROJECTS, not folders: the live corpus has
    AppRiver.Nautical.Domain.Tests and AppRiver.Provisioning.UnitTests, which an exact
    match on "tests" misses entirely."""
    assert classify_layer("Source/AppRiver.Nautical.Domain.Tests/Given_Something.cs") == "test"
    assert classify_layer("Source/AppRiver.Provisioning.UnitTests/Install/Thing.cs") == "test"
    assert classify_layer("src/AppRiver.Connector.Test/Controllers/Given_X.cs") == "test"
    # ...without swallowing a word that merely contains "test".
    assert classify_layer("src/Contest/ScoreRepository.cs") == "data"


def test_a_test_for_a_controller_is_test_scaffolding_not_the_api_layer():
    assert classify_layer("src/Api.Tests/AccountControllerTests.cs") == "test"
    assert classify_layer("src/Api/AccountController.cs") == "api"


def test_vendored_code_is_recognised_by_segment_or_library_name():
    assert is_vendored("app/node_modules/left-pad/index.js")
    assert is_vendored("Source/AppRiver.Provisioning/Scripts/jquery-1.4.4.js")
    assert is_vendored("wwwroot/js/site.min.js")
    assert classify_layer("app/node_modules/left-pad/index.js") == "vendor"


def test_vendor_is_matched_as_a_path_segment_never_as_a_word_in_a_filename():
    """The live corpus contains GetOrderByVendorCodeResponseServiceModel.cs — an org file
    about a *vendor code* business concept. A substring match on "vendor" claims it as
    third-party code, which is both wrong and silent."""
    path = "src/OpenApi/Model/GetOrderByVendorCodeResponseServiceModel.cs"
    assert not is_vendored(path)
    assert classify_layer(path) is None
    # A hand-written script sitting beside vendored ones is not itself vendored.
    assert not is_vendored("Source/App/Scripts/app.custom.js")


def test_ambiguous_role_words_are_left_untagged_rather_than_guessed():
    """`handler` is an HTTP handler in one codebase and a CQRS command handler in the
    next; `model` is domain, persistence or view depending on the shop. A wrong layer is
    invisible once stored, so these deliberately return None. `page`/`view` are excluded
    for the same reason — validated against Model/Page.cs, an OpenAPI pagination model
    that a naive UI rule mislabels."""
    # Wrapped in a DOMAIN-named folder, not a layer-named one: `/Domain/` and `/Services/`
    # legitimately do name a layer, so using one here would test the directory rule
    # instead of the filename rule this is about.
    for name in ("SubscriptionEventHandler.cs", "CustomerModel.cs", "OrderDto.cs",
                 "PriceChangedEvent.cs", "SecureContentClient.cs", "AppConfiguration.cs",
                 "Model/Page.cs"):
        assert classify_layer(f"Source/AppRiver.Billing/{name}") is None, name


def test_directory_disambiguates_the_words_the_filename_cannot():
    """`/views/` and `/pages/` DO name a layer, so the same words work as directories."""
    assert classify_layer("app/views/Dashboard.cs") == "ui"
    assert classify_layer("app/pages/AddUserPage.ts") == "ui"


def test_extension_and_wellknown_filenames_are_the_last_resort():
    assert classify_layer("app/whatever/Thing.tsx") == "ui"
    assert classify_layer("deploy/main.tf") == "infra"
    assert classify_layer("Dockerfile") == "infra"


def test_every_returned_layer_is_a_declared_one():
    """Lockstep: the UI colours by this vocabulary, so a rule table cannot invent a value
    that no consumer knows about."""
    paths = [
        "src/Api/AccountController.cs", "src/CustomerRepository.cs", "src/X.Tests/A.cs",
        "node_modules/x/i.js", "app/views/D.cs", "deploy/main.tf", "src/JsonExtensions.cs",
        "src/Services/Thing.cs", "src/Nothing/Special.cs",
    ]
    for p in paths:
        layer = classify_layer(p)
        assert layer is None or layer in LAYERS, (p, layer)


def test_code_graph_tags_defined_symbols_with_their_files_layer():
    """The layer rides the entity tuple as an optional 4th element, so the other
    extractors' 3-tuples are untouched."""
    text = "public class AccountController { }"
    entities, edges = extract_code_graph(
        text, "https://git/x.git::src/Api/AccountController.cs", "repo:x")
    symbols = [e for e in entities if e[2] == "symbol"]
    assert symbols and all(len(e) == 4 and e[3] == "api" for e in symbols)


def test_imported_modules_carry_no_layer_from_the_importing_file():
    """A module's layer is a property of ITS source, which the importing file cannot see.
    Tagging it with the consumer's layer would label every library by whoever used it."""
    entities, _ = extract_code_graph(
        "import os\nclass Thing: pass\n", "https://git/x.git::src/Api/thing.py", "repo:x")
    modules = [e for e in entities if e[2] == "module"]
    assert modules and all(len(e) == 3 for e in modules)


def test_layer_comes_from_the_full_uri_not_the_truncated_edge_detail():
    """code_graph stores only the last 80 chars of the path as human-readable evidence;
    classifying that instead of the uri would drop a layer directory off the front."""
    long_uri = "https://git.example.com/very/deep/" + "x" * 60 + "/node_modules/lib/a.js"
    entities, _ = extract_code_graph("function f(){}", long_uri, "repo:x")
    symbols = [e for e in entities if e[2] == "symbol"]
    assert symbols and symbols[0][3] == "vendor"
