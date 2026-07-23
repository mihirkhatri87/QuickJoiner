"""Files/URL connector: ingest local files, folders, or web pages."""

from __future__ import annotations

import concurrent.futures
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import httpx

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.deps import SKIP_DIRS, dependency_document  # noqa: F401 — SKIP_DIRS re-exported
from quickjoiner.connectors.registry import register
from quickjoiner.ingest.extract import (  # noqa: F401 — extension sets re-exported
    CODE_EXTENSIONS,
    DOC_EXTENSIONS,
    NAMED_TEXT_FILES,
    TEXT_EXTENSIONS,
    ExtractionError,
    document_kind,
    extract_text,
)

# Plain-text files are read whole and small; office/PDF files (parsed via extract.py) are
# routinely larger, so they get a roomier cap. Both bound work, not correctness.
MAX_FILE_BYTES = 2_000_000
MAX_DOC_BYTES = 30_000_000


def read_workers() -> int:
    """How many files to read concurrently. Disk reads (`stat`/`read_text`) release the GIL,
    so a small thread pool genuinely parallelizes the I/O — the file-reading bottleneck on a
    large repo (and on every re-sync, where unchanged files are still read to hash them).
    `QJ_READ_WORKERS` overrides; default scales to the machine, capped so a spinning disk isn't
    thrashed. 1 ⇒ fully sequential (the prior behaviour)."""
    env = os.environ.get("QJ_READ_WORKERS")
    if env and env.strip().isdigit():
        return max(1, min(int(env), 64))
    return max(2, min(8, (os.cpu_count() or 4)))


def read_documents_parallel(
    items: list,
    read_fn: Callable[[Any], Document | None],
    stage: Callable[[int, int], None] | None = None,
    workers: int | None = None,
) -> Iterator[Document]:
    """Turn `items` into Documents concurrently, yielded **in order**, with at most a bounded
    number of `read_fn` calls in flight (so a huge input doesn't balloon memory). `read_fn(item)`
    returns a Document or None (skipped) and does the slow I/O — a file read (files/git) or a
    network fetch (e.g. Octopus per-project releases). `stage(done, total)` — the connector's
    `_stage` — is called before each position for progress AND cooperative pause/stop: it may
    raise `SyncStopped`, which unwinds cleanly because `read_fn` is a side-effect-free read
    (nothing is committed here; the pipeline ingests downstream, idempotently). Integrity is
    unchanged — only the *fetching* is parallel. Best for I/O that releases the GIL (disk, httpx).
    """
    total = len(items)
    n = workers if workers is not None else read_workers()
    if n <= 1 or total <= 1:
        for i, p in enumerate(items):
            if stage:
                stage(i, total)
            doc = read_fn(p)
            if doc is not None:
                yield doc
        return
    depth = max(n * 4, 16)  # prefetch window: reads run ahead of the (slower) downstream embed
    it = iter(items)
    pending: deque[concurrent.futures.Future] = deque()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        for _ in range(depth):
            p = next(it, None)
            if p is None:
                break
            pending.append(pool.submit(read_fn, p))
        done = 0
        while pending:
            fut = pending.popleft()
            if stage:
                stage(done, total)  # checkpoint (stop/pause) + progress, in submission order
            done += 1
            nxt = next(it, None)
            if nxt is not None:
                pending.append(pool.submit(read_fn, nxt))
            doc = fut.result()  # in-order; the pool exits with wait=True if stage() raised
            if doc is not None:
                yield doc


def read_file_document(path: Path, root: Path) -> Document | None:
    """Read one file into a Document if we can extract text from it; None otherwise.

    Plain-text/code/markdown/json/html are decoded directly; Word/PowerPoint/Excel/PDF are
    parsed via `ingest.extract` (office/PDF get the roomier size cap). A file we can't parse
    (missing optional library, corrupt/encrypted, or an image-only doc with no text layer)
    is skipped, not fatal. Shared by the files, git_repo and uploads connectors.
    """
    suffix = path.suffix.lower()
    name = path.name.lower()
    stem = path.stem.lower()
    is_doc = suffix in DOC_EXTENSIONS
    is_text = suffix in TEXT_EXTENSIONS or name in NAMED_TEXT_FILES or stem in NAMED_TEXT_FILES
    if not is_doc and not is_text:
        return None
    try:
        if path.stat().st_size > (MAX_DOC_BYTES if is_doc else MAX_FILE_BYTES):
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = extract_text(raw, path.name)
    except ExtractionError:
        return None  # skip a file we can't parse — never fail the whole sync
    if not text.strip():
        return None  # empty or image-only (no text layer, no vision handler wired yet)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    rel = path.relative_to(root) if path != root else Path(path.name)
    return Document(
        uri=path.as_uri(),
        title=str(rel),
        text=text,
        kind=document_kind(path.name),
        updated_at=mtime,
    )


@register
class FilesConnector(Connector):
    type_name = "files"
    modes = Mode.PULL

    def _target(self) -> str:
        return str(self.options.get("path") or self.options.get("url") or "")

    def test(self) -> ConnectionStatus:
        target = self._target()
        if not target:
            return ConnectionStatus(False, "No 'path' or 'url' configured")
        if target.startswith(("http://", "https://")):
            return ConnectionStatus(True, f"URL target: {target}")
        p = Path(target)
        return (
            ConnectionStatus(True, f"Path exists: {p}")
            if p.exists()
            else ConnectionStatus(False, f"Path not found: {p}")
        )

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        target = self._target()
        if target.startswith(("http://", "https://")):
            yield self._fetch_url(target)
            return
        root = Path(target)
        if root.is_file():
            doc = self._read_file(root, root.parent)
            if doc:
                yield doc
            return
        # Count the ingestable files up front (a fast local walk) so the file phase can
        # report an accurate % — a local tree is one of the few sources with a real total.
        self._stage("scanning files")
        files = [
            p for p in sorted(root.rglob("*"))
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
        ]
        total = len(files)
        # Read files concurrently (bounded, in-order); disk I/O releases the GIL so this is a
        # real speedup, and it overlaps reading with the downstream embed. Stop/pause + progress
        # still land per file via the stage callback.
        yield from read_documents_parallel(
            files,
            lambda p: self._read_file(p, root),
            stage=lambda done, tot: self._stage("reading files", done, tot),
        )
        self._stage("dependency map", total, total)
        dep_doc = dependency_document(root, self.name, root.as_uri())
        if dep_doc:
            yield dep_doc

    def _read_file(self, path: Path, root: Path) -> Document | None:
        return read_file_document(path, root)

    def _fetch_url(self, url: str) -> Document:
        from bs4 import BeautifulSoup

        resp = httpx.get(url, timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type:
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            title = soup.title.string.strip() if soup.title and soup.title.string else url
            text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        else:
            title, text = url, resp.text
        return Document(uri=url, title=title, text=text, kind="doc")
