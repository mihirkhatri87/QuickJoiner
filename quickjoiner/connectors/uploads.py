"""The rolling Uploads connector — one permanent, continuously-growing document drop-box.

Instead of a new connector per file, every ad-hoc document a user adds (a chat drag-drop, a
`/qj` "ingest this file" request, or the CLI/API upload endpoints) lands in ONE managed folder
`<workspace>/uploads/` and is ingested into this single source. It accepts anything the shared
extractor understands — Word, PowerPoint, Excel, PDF, Markdown, text, JSON, HTML, code — via the
same `read_file_document` path the files/git connectors use (see `ingest/extract.py`).

It's a seeded singleton (like the control connector): auto-created, un-deletable, un-renamable,
and never user-duplicated — but, unlike the control connector, it DOES produce documents and IS
syncable and cleanable. Re-syncing re-scans the folder and picks up everything new/changed
(idempotent via hash dedupe); a clean-up forgets the ingested memory (the files stay on disk).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from quickjoiner.connectors.base import ConnectionStatus, Mode
from quickjoiner.connectors.files import FilesConnector
from quickjoiner.connectors.registry import register

UPLOADS_TYPE = "uploads"
UPLOADS_NAME = "uploads"
UPLOADS_SOURCE_ID = f"{UPLOADS_TYPE}:{UPLOADS_NAME}"


def uploads_dir(workspace: Path | str) -> Path:
    """The managed drop-box folder for a workspace, created on demand."""
    d = Path(workspace) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(filename: str) -> str:
    """A filesystem-safe basename derived from an uploaded filename (strip any path, keep a
    conservative character set). Never empty."""
    base = Path(filename.replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ +()\-]", "_", base).strip(" .") or "upload"
    return base[:180]


def save_upload(workspace: Path | str, filename: str, data: bytes) -> Path:
    """Write uploaded `data` into the rolling uploads folder under a safe, collision-aware name
    and return its path. Re-uploading the identical bytes under the same name is idempotent
    (same path ⇒ the pipeline dedupes on content hash); a DIFFERENT file that happens to share a
    name gets a short content-hash suffix so it never silently clobbers the earlier one."""
    folder = uploads_dir(workspace)
    name = _safe_name(filename)
    target = folder / name
    if target.exists() and target.read_bytes() != data:
        stem, dot, ext = name.rpartition(".")
        digest = hashlib.sha256(data).hexdigest()[:8]
        target = folder / (f"{stem}-{digest}.{ext}" if dot else f"{name}-{digest}")
    target.write_bytes(data)
    return target


@register
class UploadsConnector(FilesConnector):
    type_name = UPLOADS_TYPE
    modes = Mode.PULL | Mode.PUSH  # PUSH = files pushed in via the upload endpoints

    def _target(self) -> str:
        return str(uploads_dir(self.workspace))

    def test(self) -> ConnectionStatus:
        folder = uploads_dir(self.workspace)
        n = sum(1 for p in folder.rglob("*") if p.is_file())
        return ConnectionStatus(True, f"Uploads folder ready ({n} file(s)): {folder}")


def is_uploads_source(name: str | None = None, type_: str | None = None) -> bool:
    """True if a source name/type refers to the reserved rolling uploads connector."""
    return name == UPLOADS_NAME or type_ == UPLOADS_TYPE
