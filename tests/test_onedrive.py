"""OneDrive / SharePoint connector + Microsoft Graph auth.

Everything here runs offline. The pure converters are exercised directly (the house
pattern — no HTTP mocking needed to test payload→Document logic), and the few paths
that genuinely need a round trip use an injected `httpx.MockTransport`, the same seam
`test_litellm_provider.py` uses.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from quickjoiner.connectors import msgraph
from quickjoiner.connectors.msgraph import (
    GraphAuthError,
    GraphClient,
    TokenBundle,
    bundle_from_response,
    pkce_pair,
    save_token,
    scopes_for,
    token_path,
)
from quickjoiner.connectors.onedrive import (
    MAX_ITEM_BYTES,
    OneDriveConnector,
    access_scopes,
    item_document,
    item_ref,
    load_manifest,
    merge_manifest,
    notification_refs,
    search_body,
    search_hits,
    sharing_token,
    skip_reason,
)


# ------------------------------------------------------------------ pure converters

def test_sharing_token_matches_microsofts_documented_encoding():
    url = "https://contoso.sharepoint.com/:w:/r/sites/Eng/Shared Documents/spec.docx"
    token = sharing_token(url)
    assert token.startswith("u!")
    # Reverse it: base64url, padding stripped. This is the encoding Graph specifies;
    # getting it wrong is a 400 on every shared link, so it is worth pinning exactly.
    body = token[2:].replace("_", "/").replace("-", "+")
    decoded = base64.b64decode(body + "=" * (-len(body) % 4)).decode()
    assert decoded == url
    assert "=" not in token


def test_access_scopes_normalises_and_maps_to_least_privilege_graph_scopes():
    assert access_scopes({"access": "my_drive, sharepoint"}) == ["my_drive", "sharepoint"]
    assert access_scopes({"access": ["bogus"]}) == ["my_drive", "shared_with_me"]  # falls back
    assert access_scopes({}) == ["my_drive", "shared_with_me"]
    # Asking for only your own drive must NOT request the admin-consent scopes.
    own = scopes_for(["my_drive"])
    assert "Files.Read" in own and "Sites.Read.All" not in own and "Files.Read.All" not in own
    assert "offline_access" in own  # without it there is no refresh token, so no background sync
    assert "Sites.Read.All" in scopes_for(["sharepoint"])


def test_skip_reason_names_images_as_not_yet_rather_than_unsupported():
    # The distinction the user asked for: an image is a "not yet", a .exe is a "never".
    reason = skip_reason({"name": "diagram.png", "file": {}})
    assert reason is not None and "image" in reason and "vision" in reason
    assert "unsupported type" in (skip_reason({"name": "setup.exe", "file": {}}) or "")


@pytest.mark.parametrize("item, expected", [
    ({"name": "spec.docx", "file": {}}, None),
    ({"name": "notes.md", "file": {}}, None),
    ({"name": "data.csv", "file": {}}, None),
    ({"name": "bundle.zip", "file": {}}, None),
    ({"name": "deck.pptx", "file": {}}, None),
    ({"name": "book.xlsx", "file": {}}, None),
    ({"name": "Reports", "folder": {}}, "folder"),
    ({"name": "gone.docx", "file": {}, "deleted": {}}, "deleted"),
    ({"name": "huge.pdf", "file": {}, "size": MAX_ITEM_BYTES + 1}, "larger than 30 MB"),
])
def test_skip_reason_covers_the_document_types_we_do_and_dont_take(item, expected):
    assert skip_reason(item) == expected


def test_item_document_uses_weburl_so_citations_are_clickable():
    item = {
        "id": "01ABC", "name": "Design.docx", "webUrl": "https://contoso-my.sharepoint.com/x/Design.docx",
        "lastModifiedDateTime": "2026-07-01T10:00:00Z",
        "parentReference": {"driveId": "b!drive", "path": "/drive/root:/Projects/Alpha"},
        "lastModifiedBy": {"user": {"displayName": "Dana Ray"}},
    }
    doc = item_document(item, "the body text", "My OneDrive")
    assert doc.uri == "https://contoso-my.sharepoint.com/x/Design.docx"
    assert "Projects/Alpha" in doc.title and "Design.docx" in doc.title
    assert "the body text" in doc.text and "Dana Ray" in doc.text
    assert doc.metadata["drive_id"] == "b!drive" and doc.metadata["item_id"] == "01ABC"
    assert doc.updated_at == "2026-07-01T10:00:00Z"


def test_item_document_falls_back_to_a_stable_uri_when_graph_omits_weburl():
    doc = item_document({"id": "i1", "name": "x.md", "parentReference": {"driveId": "d1"}}, "t")
    assert doc.uri == "onedrive://d1/i1"  # the uri keys the doc id — never empty, never random


def test_item_ref_follows_remoteitem_for_shared_and_search_results():
    # A sharedWithMe entry is a shortcut; using its own ids 404s on download.
    shared = {"id": "shortcut", "remoteItem": {"id": "real", "parentReference": {"driveId": "owner"}}}
    assert item_ref(shared) == ("owner", "real")
    assert item_ref({"id": "plain", "parentReference": {"driveId": "d"}}) == ("d", "plain")


def test_merge_manifest_dedupes_by_drive_and_item():
    existing = [{"drive_id": "d", "item_id": "1", "name": "old.docx"}]
    merged = merge_manifest(existing, [
        {"drive_id": "d", "item_id": "1", "name": "new.docx"},  # same file, refreshed metadata
        {"drive_id": "d", "item_id": "2", "name": "other.docx"},
    ])
    assert len(merged) == 2
    assert {m["name"] for m in merged} == {"new.docx", "other.docx"}


def test_search_body_and_hits_round_trip_the_graph_search_shape():
    body = search_body("architecture", 99)
    assert body["requests"][0]["entityTypes"] == ["driveItem"]
    assert body["requests"][0]["size"] == 25  # clamped
    payload = {"value": [{"hitsContainers": [{"hits": [
        {"resource": {"name": "a.docx"}}, {"resource": {}},
    ]}]}]}
    assert [h["name"] for h in search_hits(payload)] == ["a.docx"]


def test_notification_refs_extracts_drive_and_item_from_a_change_notification():
    payload = {"value": [
        {"resource": "drives/b!abc/items/01XYZ", "resourceData": {"id": "01XYZ"}},
        {"resource": "me/mailFolders/inbox", "resourceData": {"id": "nope"}},  # not a drive
    ]}
    assert notification_refs(payload) == [("b!abc", "01XYZ")]


# ------------------------------------------------------------------------- auth

def test_pkce_challenge_is_the_s256_hash_of_the_verifier():
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and "=" not in challenge


def test_bundle_from_response_keeps_the_previous_refresh_token_when_none_is_returned():
    # Microsoft rotates refresh tokens but does not always return a new one. Blanking it
    # would silently turn a working connector into one that needs re-authentication.
    previous = TokenBundle(refresh_token="keep-me", tenant="t", client_id="c", account="a@b.com")
    refreshed = bundle_from_response({"access_token": "new", "expires_in": 3600}, previous)
    assert refreshed.refresh_token == "keep-me"
    assert refreshed.client_id == "c" and refreshed.tenant == "t"
    assert not refreshed.expired()


def test_token_bundle_treats_unknown_and_imminent_expiry_as_expired():
    assert TokenBundle().expired()  # no expires_at at all
    soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    assert TokenBundle(expires_at=soon).expired()  # inside the refresh skew
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    assert not TokenBundle(expires_at=later).expired()


def test_token_path_sanitises_a_source_id_that_is_not_a_legal_filename(tmp_path):
    path = token_path(tmp_path, "onedrive:my files")
    assert ":" not in path.name and " " not in path.name
    assert path.suffix == ".json"
    # Different source ids never collide on one token file.
    assert token_path(tmp_path, "onedrive:my_files") != path


def test_device_code_poll_waits_through_authorization_pending(monkeypatch):
    responses = [
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
    ]
    transport = httpx.MockTransport(lambda request: responses.pop(0))
    bundle = msgraph.poll_device_code(
        "organizations", "client", "dev-code", interval=1, transport=transport,
        sleep=lambda _s: None, now=lambda: 0.0,
    )
    assert bundle.access_token == "at" and bundle.refresh_token == "rt"


def test_dead_refresh_token_raises_a_sign_in_error_not_a_generic_failure():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "AADSTS700082: expired"}))
    with pytest.raises(GraphAuthError) as exc:
        msgraph.refresh_token(TokenBundle(refresh_token="dead", client_id="c"), ["Files.Read"],
                              transport=transport)
    assert "Sign-in required" in str(exc.value)


def test_graph_client_refreshes_an_expired_token_and_persists_the_rotated_one(tmp_path):
    stale = TokenBundle(
        access_token="old", refresh_token="rot-1", client_id="c", tenant="organizations",
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    save_token(tmp_path, "onedrive:x", stale)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={
                "access_token": "fresh", "refresh_token": "rot-2", "expires_in": 3600})
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"userPrincipalName": "dana@contoso.com"})

    client = GraphClient(tmp_path, "onedrive:x", ["Files.Read"], transport=httpx.MockTransport(handler))
    assert client.whoami()["userPrincipalName"] == "dana@contoso.com"
    assert seen == ["Bearer fresh"]
    # The rotated refresh token must reach disk, or the NEXT sync fails.
    on_disk = json.loads(token_path(tmp_path, "onedrive:x").read_text())
    assert on_disk["refresh_token"] == "rot-2"


# ------------------------------------------------------------- connector behaviour

def _signed_in(tmp_path, source_id="onedrive:drive"):
    save_token(tmp_path, source_id, TokenBundle(
        access_token="at", refresh_token="rt", client_id="c", account="dana@contoso.com",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
    ))


def _connector(tmp_path, handler, options=None):
    return OneDriveConnector(
        name="drive", options={"client_id": "c", **(options or {})}, workspace=tmp_path,
        transport=httpx.MockTransport(handler),
    )


def test_test_reports_not_signed_in_as_ok_so_the_connector_can_be_created(tmp_path):
    # The token is keyed by source_id, so it cannot exist before the connector does; a
    # failing test would block creation and deadlock the two.
    result = _connector(tmp_path, lambda r: httpx.Response(200, json={})).test()
    assert result.ok and "not signed in yet" in result.message
    assert "communal memory" in result.message  # the confidentiality warning is not buried


def test_test_without_a_client_id_fails(tmp_path):
    connector = OneDriveConnector(name="drive", options={}, workspace=tmp_path)
    assert not connector.test().ok


def test_learn_resolves_a_shared_link_downloads_and_records_it(tmp_path):
    _signed_in(tmp_path)
    item = {
        "id": "01ITEM", "name": "spec.md", "file": {},
        "webUrl": "https://contoso.sharepoint.com/spec.md",
        "parentReference": {"driveId": "b!d", "path": "/drive/root:/Specs"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/shares/" in path:
            return httpx.Response(200, json=item)
        if path.endswith("/content"):
            return httpx.Response(200, content=b"# Spec\n\nthe requirement is X.")
        return httpx.Response(404, json={"error": {"code": "notFound", "message": path}})

    connector = _connector(tmp_path, handler)
    docs, notes = connector.learn(["https://contoso.sharepoint.com/:t:/r/spec.md"])

    assert len(docs) == 1 and notes == []
    assert "the requirement is X." in docs[0].text
    assert docs[0].uri == "https://contoso.sharepoint.com/spec.md"
    # Learned items are remembered, so a later sync can refresh exactly these.
    manifest = load_manifest(tmp_path, "onedrive:drive")
    assert [(m["drive_id"], m["item_id"]) for m in manifest] == [("b!d", "01ITEM")]


def test_learn_reports_what_it_could_not_take_instead_of_silently_ingesting_less(tmp_path):
    _signed_in(tmp_path)
    image = {"id": "i", "name": "whiteboard.png", "file": {},
             "parentReference": {"driveId": "b!d"}}
    handler = lambda request: httpx.Response(200, json=image)  # noqa: E731
    docs, notes = _connector(tmp_path, handler).learn(["photo.png"])
    assert docs == []
    assert any("image" in n and "vision" in n for n in notes)


def test_learn_expands_a_folder_but_only_the_readable_members(tmp_path):
    _signed_in(tmp_path)
    folder = {"id": "F", "name": "Specs", "folder": {}, "parentReference": {"driveId": "b!d"}}
    children = {"value": [
        {"id": "a", "name": "a.md", "file": {}, "parentReference": {"driveId": "b!d"}},
        {"id": "b", "name": "logo.png", "file": {}, "parentReference": {"driveId": "b!d"}},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/children" in path:
            return httpx.Response(200, json=children)
        if path.endswith("/content"):
            return httpx.Response(200, content=b"body")
        return httpx.Response(200, json=folder)

    docs, notes = _connector(tmp_path, handler).learn(["Specs"])
    assert [d.title.split(" (")[0] for d in docs] == ["a.md"]
    assert any("image" in n for n in notes)


def test_sync_refreshes_only_what_was_learned_and_never_discovers_new_files(tmp_path):
    _signed_in(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"refreshed body")
        return httpx.Response(200, json={
            "id": "01ITEM", "name": "spec.md", "file": {},
            "webUrl": "https://contoso.sharepoint.com/spec.md",
            "parentReference": {"driveId": "b!d"}})

    connector = _connector(tmp_path, handler)
    # Nothing learned yet ⇒ a sync yields nothing at all, and asks Graph nothing.
    assert list(connector.sync({})) == []
    assert calls == []

    from quickjoiner.connectors.onedrive import save_manifest
    save_manifest(tmp_path, "onedrive:drive",
                  [{"drive_id": "b!d", "item_id": "01ITEM", "name": "spec.md"}])
    docs = list(connector.sync({}))
    assert len(docs) == 1 and "refreshed body" in docs[0].text
    # Only the learned item was fetched — no delta walk, no enumeration of the drive.
    assert all("/delta" not in c and "sharedWithMe" not in c for c in calls)


def test_handle_event_only_refreshes_items_already_learned(tmp_path):
    _signed_in(tmp_path)
    from quickjoiner.connectors.onedrive import save_manifest

    save_manifest(tmp_path, "onedrive:drive", [{"drive_id": "b!d", "item_id": "known"}])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"changed")
        return httpx.Response(200, json={"id": "known", "name": "k.md", "file": {},
                                         "parentReference": {"driveId": "b!d"}})

    connector = _connector(tmp_path, handler)
    unknown = {"value": [{"resource": "drives/b!d/items/stranger",
                          "resourceData": {"id": "stranger"}}]}
    assert list(connector.handle_event(unknown)) == []  # a webhook never widens what we ingest
    known = {"value": [{"resource": "drives/b!d/items/known", "resourceData": {"id": "known"}}]}
    assert len(list(connector.handle_event(known))) == 1


def test_on_deleted_removes_the_stored_refresh_token_and_manifest(tmp_path):
    _signed_in(tmp_path)
    from quickjoiner.connectors.onedrive import manifest_path, save_manifest

    save_manifest(tmp_path, "onedrive:drive", [{"drive_id": "d", "item_id": "i"}])
    connector = OneDriveConnector(name="drive", options={"client_id": "c"}, workspace=tmp_path)
    connector.on_deleted()
    # A live Microsoft 365 refresh token must not outlive the connector that owned it.
    assert not token_path(tmp_path, "onedrive:drive").exists()
    assert not manifest_path(tmp_path, "onedrive:drive").exists()


def test_live_tools_are_consolidated_per_type_and_are_read_only(tmp_path):
    one = OneDriveConnector(name="mine", options={"client_id": "c"}, workspace=tmp_path)
    two = OneDriveConnector(name="team", options={"client_id": "c"}, workspace=tmp_path)
    names = [t.spec.name for t in OneDriveConnector.type_tools([one, two])]
    assert names == ["onedrive_search", "onedrive_read_file"]  # one set, not two
    # With several connectors configured the tools gain a selector rather than multiplying.
    schema = OneDriveConnector.type_tools([one, two])[0].spec.input_schema
    assert "connector" in schema["properties"]
    assert "connector" not in OneDriveConnector.type_tools([one])[0].spec.input_schema["properties"]
    # No tool here mutates memory: learning is an explicit, confirmed API action.
    assert not any("learn" in n or "ingest" in n for n in names)
