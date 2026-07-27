"""OneDrive for Business / SharePoint connector (Microsoft Graph, delegated).

**Per user, by construction.** Every call is made with a delegated token belonging to
one signed-in person, so the connector can read exactly what that person can read —
their own drive, files other people shared *with* them, and the SharePoint/Teams
libraries they have access to. There is no application-permission path here: a user
cannot accidentally grant QuickJoiner more of the tenant than they can see themselves.

**On demand, not on a schedule.** This connector deliberately does NOT crawl a drive.
A corporate OneDrive is tens of thousands of files, most of them noise, much of it
confidential-by-accident; bulk-ingesting it is both expensive and the wrong default.
Instead it is a *credentialed reader* that ingests exactly what you point it at:

    /qj learn from this onedrive document <url or path>

which resolves that URL/path through the signed-in user's own access, extracts its
text, and runs it through the normal pipeline — so it is chunked, embedded, cited and
graphed like anything else. A folder URL learns the files under it (bounded, reported).

What `sync` does, then, is **refresh what you already taught it** — it re-reads the
items in this connector's learned-item manifest and lets the pipeline's hash dedupe
decide whether anything changed. It never discovers new files. That keeps scheduled
syncs meaningful (your learned documents stay current) without turning the connector
into the crawler it deliberately isn't.

Text extraction is entirely `ingest/extract.py` — the same choke point the files/git
connectors and the upload endpoints use — so Word, PowerPoint, Excel, PDF, Markdown,
HTML, CSV and JSON arrive as text with no parsing code of its own here.

**Deletions are not propagated**: a file deleted in OneDrive stays in learned memory
until the connector is cleaned up, because a connector has no handle on the catalog or
vector store. Stated here rather than left to be discovered.

⚠ Ingested content joins QuickJoiner's **single communal memory** — see
`is_communal_memory_warning()`. A file shared privately with you becomes answerable,
and citable, for every user of the workspace.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.live_tools import bounded, clamp, make_resolver, read_tool
from quickjoiner.connectors.msgraph import (
    GraphAuthError,
    GraphClient,
    GraphError,
    delete_token,
    scopes_for,
)
from quickjoiner.connectors.registry import register
from quickjoiner.ingest.extract import (
    ExtractionError,
    document_kind,
    extract_text,
    is_image,
    supported_extension,
)
from quickjoiner.llm.base import AgentTool

#: What this connector is permitted to reach. These map to delegated Graph scopes, so
#: they are a *consent* choice, not a crawl scope: `Files.Read` is usually
#: self-service in a tenant, `Sites.Read.All` usually needs an admin, and a user who
#: only ever learns from their own drive should not be blocked behind the latter.
ACCESS_SCOPES = ("my_drive", "shared_with_me", "sharepoint")
DEFAULT_ACCESS = ("my_drive", "shared_with_me")

#: Same ceiling the files connector uses for office/PDF documents.
MAX_ITEM_BYTES = 30_000_000

#: A single "learn this folder" is bounded so one mis-aimed URL at the drive root
#: can't become the bulk crawl this connector exists to avoid. Hitting it is reported.
MAX_LEARN_ITEMS = 250

WARNING_COMMUNAL_MEMORY = (
    "Anything you learn through this connector joins QuickJoiner's single communal "
    "memory: every user of this workspace can retrieve and cite it, including files "
    "that were shared privately with you. Per-user knowledge scopes are on the roadmap "
    "but are not built yet."
)


def is_communal_memory_warning() -> str:
    """The warning surfaced at connect time and before an on-demand learn. A function,
    not a bare constant, so there is one obvious place to change the wording when
    per-user knowledge scopes land and it stops being true."""
    return WARNING_COMMUNAL_MEMORY


# ------------------------------------------------------------------ pure converters
# Module-level and side-effect free so tests exercise them without mocking HTTP —
# the house pattern for every connector.

def access_scopes(options: dict[str, Any]) -> list[str]:
    raw = options.get("access") or list(DEFAULT_ACCESS)
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    wanted = {str(r).strip() for r in raw}
    selected = [a for a in ACCESS_SCOPES if a in wanted]
    return selected or list(DEFAULT_ACCESS)


def is_url(target: str) -> bool:
    return target.strip().lower().startswith(("http://", "https://"))


def sharing_token(url: str) -> str:
    """Encode a OneDrive/SharePoint URL as a Graph *sharing token* (`u!…`).

    This is what makes "paste any link you can open" work. Graph's `/shares/{token}`
    endpoint resolves a sharing URL — a `:w:/r/sites/...` Office link, a "copy link"
    share, a plain file URL — to the underlying driveItem, honouring the caller's own
    permissions. Encoding is defined by Microsoft: base64 of the UTF-8 URL, made
    URL-safe, with `=` padding stripped and a `u!` prefix.
    """
    encoded = base64.b64encode(url.strip().encode("utf-8")).decode("ascii")
    return "u!" + encoded.rstrip("=").replace("/", "_").replace("+", "-")


def skip_reason(item: dict[str, Any]) -> Optional[str]:
    """Why this item cannot be ingested, or None if it can.

    Returns the *reason* rather than a bool so a learn call can report "skipped: 3
    unsupported type (png)" instead of quietly ingesting less than was asked for.
    """
    if item.get("deleted") is not None:
        return "deleted"
    if item.get("folder") is not None:
        return "folder"
    name = item.get("name") or ""
    if not name:
        return "unnamed"
    if is_image(name):
        # Distinguished from "unsupported" on purpose: this one is a *not yet*, and
        # saying so is the difference between a user thinking the format will never
        # work and knowing it arrives with vision support.
        return "image — QuickJoiner can't read images yet (vision support is on the roadmap)"
    if not supported_extension(name):
        ext = name.rsplit(".", 1)[-1] if "." in name else "no extension"
        return f"unsupported type ({ext})"
    size = item.get("size")
    if isinstance(size, int) and size > MAX_ITEM_BYTES:
        return f"larger than {MAX_ITEM_BYTES // 1_000_000} MB"
    return None


def item_folder_path(item: dict[str, Any]) -> str:
    """Human folder path, from Graph's `parentReference.path`
    (`/drive/root:/Projects/Alpha`). '' at a drive root."""
    raw = ((item.get("parentReference") or {}).get("path") or "")
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    return raw.strip("/")


def item_ref(item: dict[str, Any]) -> tuple[str, str]:
    """(drive_id, item_id) for an item, following `remoteItem` when present.

    Shared items and search results are *shortcuts*: the addressable identity lives
    under `remoteItem`, pointing into the owner's drive. Using the shortcut's own ids
    yields 404s on download — the classic first bug with these endpoints.
    """
    remote = item.get("remoteItem") or item
    parent = remote.get("parentReference") or {}
    drive_id = parent.get("driveId") or remote.get("driveId") or ""
    return str(drive_id or ""), str(remote.get("id") or "")


def item_document(item: dict[str, Any], text: str, label: str = "") -> Document:
    """Graph driveItem + extracted text → a QuickJoiner Document.

    The uri is the item's **webUrl**, so citations in chat become clickable links
    straight back to the file in OneDrive/SharePoint (the markdown renderer links a
    citation whose ref is a URL). Falls back to a stable `onedrive://drive/item`
    identity when Graph omits webUrl, because the uri keys the document id and must
    never be empty or unstable.
    """
    drive_id, item_id = item_ref(item)
    uri = item.get("webUrl") or f"onedrive://{drive_id}/{item_id}"
    folder = item_folder_path(item)
    name = item.get("name") or item_id
    where = " · ".join(part for part in (label, folder) if part)
    author = (((item.get("lastModifiedBy") or {}).get("user") or {}).get("displayName") or "")

    header = [f"# {name}"]
    if where:
        header.append(f"Location: {where}")
    if author:
        header.append(f"Last modified by: {author}")

    return Document(
        uri=uri,
        title=f"{name} ({where})" if where else name,
        text="\n".join(header) + "\n\n" + text,
        kind=document_kind(name),
        updated_at=item.get("lastModifiedDateTime"),
        metadata={
            "drive_id": drive_id, "item_id": item_id, "folder": folder,
            "size": item.get("size"), "author": author,
            "web_url": item.get("webUrl") or "",
        },
    )


def search_body(query: str, size: int = 10) -> dict[str, Any]:
    """Microsoft Search request for driveItems (pure — the shape is fiddly enough to
    be worth pinning in a test)."""
    return {"requests": [{
        "entityTypes": ["driveItem"],
        "query": {"queryString": query},
        "from": 0,
        "size": max(1, min(size, 25)),
    }]}


def search_hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Microsoft Search's deeply nested response into a list of resources."""
    hits: list[dict[str, Any]] = []
    for response in payload.get("value") or []:
        for container in response.get("hitsContainers") or []:
            for hit in container.get("hits") or []:
                resource = hit.get("resource") or {}
                if resource:
                    hits.append(resource)
    return hits


def notification_refs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(driveId, itemId) pairs from a Graph change-notification body. Notifications are
    content-free by design — they name a resource, not a change."""
    refs: list[tuple[str, str]] = []
    for note in payload.get("value") or []:
        resource = note.get("resource") or ""
        drive_id = resource.split("drives/", 1)[1].split("/", 1)[0] if "drives/" in resource else ""
        item_id = (note.get("resourceData") or {}).get("id") or ""
        if drive_id and item_id:
            refs.append((drive_id, item_id))
    return refs


# --------------------------------------------------------------- learned-item manifest
# The connector must remember which items were taught to it so a later `sync` can
# refresh exactly those. It cannot use the catalog (create_connector hands a connector
# only its name/options/workspace), so this is a small file beside the token — the
# same reason browser_profile/ and uploads/ are workspace state.

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def manifest_path(workspace: Path, source_id: str) -> Path:
    return workspace / "onedrive" / f"{_SAFE.sub('_', source_id)}.json"


def load_manifest(workspace: Path, source_id: str) -> list[dict[str, Any]]:
    path = manifest_path(workspace, source_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return list(data.get("items") or []) if isinstance(data, dict) else []


def save_manifest(workspace: Path, source_id: str, items: list[dict[str, Any]]) -> None:
    path = manifest_path(workspace, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_manifest(
    existing: list[dict[str, Any]], learned: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add newly-learned items, keyed by (drive_id, item_id), newest metadata winning.
    Pure so the de-duplication rule is testable without touching disk."""
    by_key = {(i.get("drive_id"), i.get("item_id")): dict(i) for i in existing}
    for item in learned:
        by_key[(item.get("drive_id"), item.get("item_id"))] = dict(item)
    return list(by_key.values())


# ------------------------------------------------------------------- the connector

@register
class OneDriveConnector(Connector):
    type_name = "onedrive"
    # PULL here means "refresh what was taught", not "crawl". PUSH is real but
    # deployment-gated: Graph change notifications need a publicly reachable HTTPS
    # callback, so a laptop install only ever uses PULL + LIVE.
    modes = Mode.PULL | Mode.LIVE | Mode.PUSH

    def __init__(self, name: str, options: dict[str, Any], workspace, transport: Any = None):
        super().__init__(name, options, workspace)
        self._transport = transport  # httpx MockTransport in tests
        self._client_cache: Optional[GraphClient] = None

    # -- configuration -----------------------------------------------------
    @property
    def client_id(self) -> str:
        return str(self.options.get("client_id") or "").strip()

    @property
    def tenant(self) -> str:
        return str(self.options.get("tenant") or "").strip() or "organizations"

    @property
    def scopes(self) -> list[str]:
        return scopes_for(access_scopes(self.options))

    def graph(self) -> GraphClient:
        if self._client_cache is None:
            self._client_cache = GraphClient(
                self.workspace, self.source_id, self.scopes, transport=self._transport
            )
        return self._client_cache

    # -- contract ----------------------------------------------------------
    def test(self) -> ConnectionStatus:
        """Validate configuration and, once signed in, the credentials themselves.

        A connector that has never been signed in reports **ok** with an explicit "not
        signed in yet" message rather than failing. That is deliberate: the token is
        keyed by source_id, so it cannot be obtained until the connector exists, and a
        failing test blocks creation — the two would deadlock. The message says exactly
        what to do next, and every operation without a token fails loudly.
        """
        if not self.client_id:
            return ConnectionStatus(
                False, "Missing Application (client) ID from your Azure app registration")
        client = self.graph()
        if not client.signed_in():
            return ConnectionStatus(
                True,
                f"Configured, but not signed in yet — run `qj onedrive login {self.name}` or "
                f"use Sign in with Microsoft in Settings. {WARNING_COMMUNAL_MEMORY}",
            )
        try:
            me = client.whoami()
        except GraphError as exc:  # GraphAuthError included
            return ConnectionStatus(False, str(exc))
        account = me.get("userPrincipalName") or me.get("displayName") or "unknown account"
        learned = len(load_manifest(self.workspace, self.source_id))
        return ConnectionStatus(
            True,
            f"Signed in as {account} · may reach: {', '.join(access_scopes(self.options))} · "
            f"{learned} document(s) learned on demand",
        )

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        """Refresh the documents this connector was explicitly taught. Discovers nothing.

        Every item in the manifest is re-read; the pipeline's sha256 dedupe means
        unchanged files cost one download and no re-embed. An item that has since been
        deleted or had its permission revoked is skipped with a reason, not fatal —
        losing access to one file must not stop the rest from refreshing.
        """
        items = load_manifest(self.workspace, self.source_id)
        if not items:
            self._stage("nothing learned yet — use `/qj learn from this onedrive document <url>`")
            return
        client = self.graph()
        skipped: dict[str, int] = {}
        for index, entry in enumerate(items, start=1):
            self._checkpoint()
            self._stage("refreshing learned documents", index, len(items))
            drive_id = str(entry.get("drive_id") or "")
            item_id = str(entry.get("item_id") or "")
            if not drive_id or not item_id:
                continue
            try:
                item = client.get(f"/drives/{drive_id}/items/{item_id}")
            except GraphAuthError:
                raise  # credentials died — the whole run must stop and say so
            except GraphError as exc:
                skipped[f"unreadable now ({exc})"] = skipped.get(f"unreadable now ({exc})", 0) + 1
                continue
            doc = self._fetch_document(client, item, entry.get("label") or "", skipped)
            if doc is not None:
                yield doc
        if skipped:
            summary = ", ".join(f"{count}× {reason}" for reason, count in sorted(skipped.items()))
            self._stage(f"refreshed — skipped {summary}")

    def on_deleted(self) -> None:
        """Remove this connector's stored refresh token and learned-item manifest.

        Deleting a connector must not leave a live Microsoft 365 refresh token sitting
        in the workspace: it would still be redeemable by anyone who can read the file,
        long after the connector it belonged to is gone.
        """
        delete_token(self.workspace, self.source_id)
        try:
            manifest_path(self.workspace, self.source_id).unlink()
        except OSError:
            pass

    # -- on-demand learning ------------------------------------------------
    def learn(self, targets: list[str]) -> tuple[list[Document], list[str]]:
        """Resolve URLs/paths through the signed-in user's access and return
        (documents, notes). This is the connector's real entry point — the thing
        `/qj learn from this onedrive document <url>` ends up calling.

        A folder target expands to the supported files beneath it, bounded by
        MAX_LEARN_ITEMS; `notes` carries every skip and truncation so the caller can
        tell the user what was NOT learned instead of implying success.
        """
        client = self.graph()
        documents: list[Document] = []
        notes: list[str] = []
        learned_refs: list[dict[str, Any]] = []

        for target in targets:
            target = (target or "").strip()
            if not target:
                continue
            try:
                item = self._resolve(client, target)
            except GraphAuthError:
                raise
            except GraphError as exc:
                notes.append(f"{target}: {exc}")
                continue
            if item is None:
                notes.append(f"{target}: not found, or you do not have access to it")
                continue

            expanded = self._expand(client, item, notes)
            for one in expanded:
                reason = skip_reason(one)
                if reason:
                    notes.append(f"{one.get('name') or target}: skipped — {reason}")
                    continue
                skipped: dict[str, int] = {}
                doc = self._fetch_document(client, one, "", skipped)
                if doc is None:
                    for why in skipped:
                        notes.append(f"{one.get('name') or target}: skipped — {why}")
                    continue
                documents.append(doc)
                drive_id, item_id = item_ref(one)
                learned_refs.append({
                    "drive_id": drive_id, "item_id": item_id,
                    "name": one.get("name") or "", "web_url": one.get("webUrl") or "",
                    "learned_at": datetime.now(timezone.utc).isoformat(),
                })

        if learned_refs:
            merged = merge_manifest(load_manifest(self.workspace, self.source_id), learned_refs)
            save_manifest(self.workspace, self.source_id, merged)
        return documents, notes

    def _resolve(self, client: GraphClient, target: str) -> Optional[dict[str, Any]]:
        """A pasted link, or a path inside the signed-in user's own drive."""
        if is_url(target):
            return client.get(f"/shares/{sharing_token(target)}/driveItem")
        path = target.lstrip("/")
        return client.get(f"/me/drive/root:/{path}")

    def _expand(
        self, client: GraphClient, item: dict[str, Any], notes: list[str]
    ) -> list[dict[str, Any]]:
        """A file expands to itself; a folder to the supported files beneath it."""
        if item.get("folder") is None:
            return [item]
        drive_id, item_id = item_ref(item)
        if not drive_id or not item_id:
            return []
        collected: list[dict[str, Any]] = []
        stack = [item_id]
        while stack and len(collected) < MAX_LEARN_ITEMS:
            current = stack.pop()
            try:
                payload = client.get(f"/drives/{drive_id}/items/{current}/children?$top=200")
            except GraphError as exc:
                notes.append(f"could not list a folder: {exc}")
                continue
            for child in payload.get("value") or []:
                if child.get("folder") is not None:
                    stack.append(child.get("id") or "")
                elif len(collected) < MAX_LEARN_ITEMS:
                    collected.append(child)
        if len(collected) >= MAX_LEARN_ITEMS:
            notes.append(
                f"stopped at {MAX_LEARN_ITEMS} files — learn a narrower folder to get the rest")
        return collected

    def _fetch_document(
        self, client: GraphClient, item: dict[str, Any], label: str, skipped: dict[str, int]
    ) -> Optional[Document]:
        """Download + extract one item. Never raises except on dead credentials: one
        unreadable file is a skipped file, not a failed operation."""
        drive_id, item_id = item_ref(item)
        name = item.get("name") or item_id
        if not drive_id or not item_id:
            skipped["missing drive/item id"] = skipped.get("missing drive/item id", 0) + 1
            return None
        try:
            data = client.download(drive_id, item_id, MAX_ITEM_BYTES)
        except GraphAuthError:
            raise
        except GraphError as exc:
            skipped[f"download failed ({exc})"] = skipped.get(f"download failed ({exc})", 0) + 1
            return None
        if data is None:
            key = f"larger than {MAX_ITEM_BYTES // 1_000_000} MB"
            skipped[key] = skipped.get(key, 0) + 1
            return None
        try:
            text = extract_text(data, name)
        except ExtractionError as exc:
            skipped[f"unreadable ({exc})"] = skipped.get(f"unreadable ({exc})", 0) + 1
            return None
        if not text.strip():
            skipped["no extractable text"] = skipped.get("no extractable text", 0) + 1
            return None
        return item_document(item, text, label)

    # -- PUSH --------------------------------------------------------------
    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        """Graph change notification → refreshed documents.

        Only items already in the manifest are re-read. A webhook is a freshness
        signal for what you taught, never a way for the connector to start ingesting
        files you never asked for.
        """
        known = {(i.get("drive_id"), i.get("item_id")) for i in load_manifest(self.workspace, self.source_id)}
        if not known:
            return
        client = self.graph()
        skipped: dict[str, int] = {}
        for drive_id, item_id in notification_refs(payload):
            if (drive_id, item_id) not in known:
                continue
            try:
                item = client.get(f"/drives/{drive_id}/items/{item_id}")
            except GraphError:
                continue
            if skip_reason(item):
                continue
            doc = self._fetch_document(client, item, "", skipped)
            if doc is not None:
                yield doc

    # -- LIVE --------------------------------------------------------------
    @classmethod
    def type_tools(cls, connectors: list["OneDriveConnector"]) -> list[AgentTool]:
        """One READ-ONLY tool set for every configured OneDrive connector, with a
        `connector` selector when more than one exists — the consolidated pattern
        GitLab/GitHub use, so three people's OneDrives don't become three copies.

        These **find and read**; they never ingest. Learning is an explicit, confirmed
        action through the API (`/qj learn from this onedrive document <url>`), which
        keeps the plan-09 rule that live tools are GET-only intact — and keeps a model
        from deciding on its own to put someone's OneDrive into communal memory.
        """
        if not connectors:
            return []
        resolve = make_resolver(connectors, lambda c: c.name)
        selector: dict[str, tuple[str, bool, str]] = (
            {} if len(connectors) == 1
            else {"connector": ("string", False, "Which OneDrive connector to use")}
        )

        def onedrive_search(query: str, connector: str = "", limit: Any = 10) -> str:
            target, error = resolve(connector)
            if error:
                return error
            payload = target.graph().post("/search/query", search_body(query, clamp(limit, 10, 25)))
            hits = search_hits(payload)
            if not hits:
                return f"No files found in OneDrive/SharePoint matching {query!r}."
            lines = [
                f"- {h.get('name') or h.get('id') or '?'} · modified "
                f"{h.get('lastModifiedDateTime') or '?'} · {h.get('webUrl') or ''}"
                for h in hits
            ]
            lines.append(
                "\nThese files are NOT in learned memory. To ingest one permanently, the user "
                "must ask to learn it (e.g. `/qj learn from this onedrive document <url>`)."
            )
            return bounded("\n".join(lines))

        def onedrive_read_file(path_or_url: str, connector: str = "") -> str:
            target, error = resolve(connector)
            if error:
                return error
            client = target.graph()
            try:
                item = target._resolve(client, path_or_url)
            except GraphError as exc:
                return f"Could not read {path_or_url!r}: {exc}"
            reason = skip_reason(item or {})
            if item is None or reason:
                return f"Cannot read {path_or_url!r}: {reason or 'not found'}"
            drive_id, item_id = item_ref(item)
            try:
                data = client.download(drive_id, item_id, MAX_ITEM_BYTES)
            except GraphError as exc:
                return f"Could not download {path_or_url!r}: {exc}"
            if data is None:
                return f"{path_or_url!r} is too large to read inline."
            try:
                return bounded(extract_text(data, item.get("name") or path_or_url))
            except ExtractionError as exc:
                return f"Could not extract text from {path_or_url!r}: {exc}"

        return [
            read_tool(
                "onedrive_search",
                "Search the signed-in user's OneDrive and SharePoint for files by name or "
                "content, RIGHT NOW. Use for enumeration and current state ('what documents "
                "exist about X', 'find the latest deck on Y') — learned memory only returns "
                "the most similar chunks of what has already been ingested. Read-only: "
                "finding a file does NOT add it to memory.",
                onedrive_search,
                {
                    "query": ("string", True, "What to search for"),
                    **selector,
                    "limit": ("integer", False, "Maximum results (default 10)"),
                },
            ),
            read_tool(
                "onedrive_read_file",
                "Read one file from the signed-in user's OneDrive/SharePoint by path or "
                "shared URL and return its extracted text, without ingesting it into memory.",
                onedrive_read_file,
                {
                    "path_or_url": ("string", True, "Path in the user's OneDrive, or a shared link"),
                    **selector,
                },
            ),
        ]

    def tools(self) -> list[AgentTool]:
        return self.type_tools([self])
