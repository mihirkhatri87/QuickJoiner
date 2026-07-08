"""Files/URL connector: ingest local files, folders, or web pages."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register

TEXT_EXTENSIONS = {
    ".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go",
    ".rb", ".php", ".rs", ".c", ".h", ".cpp", ".hpp", ".sql", ".sh", ".ps1", ".psm1",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".xml", ".html", ".css",
    ".tf", ".dockerfile", ".gradle", ".properties", ".csv",
}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go", ".rb", ".php", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".sql", ".sh", ".ps1", ".psm1",
}
NAMED_TEXT_FILES = {
    "readme", "license", "notice", "changelog", "contributing", "authors", "owners",
    "codeowners", "dockerfile", "makefile", "jenkinsfile", "vagrantfile", "gemfile",
    "rakefile", "procfile", ".gitignore", ".gitattributes", ".editorconfig", ".env.example",
}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".idea", ".vs"}
MAX_FILE_BYTES = 2_000_000


def read_file_document(path: Path, root: Path) -> Document | None:
    """Read one file into a Document if it looks like text; None otherwise.

    Shared by the files and git_repo connectors.
    """
    suffix = path.suffix.lower()
    name = path.name.lower()
    stem = path.stem.lower()
    if suffix not in TEXT_EXTENSIONS and name not in NAMED_TEXT_FILES and stem not in NAMED_TEXT_FILES:
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    kind = "code" if suffix in CODE_EXTENSIONS else "doc"
    rel = path.relative_to(root) if path != root else Path(path.name)
    return Document(
        uri=path.as_uri(),
        title=str(rel),
        text=text,
        kind=kind,
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
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            doc = self._read_file(path, root)
            if doc:
                yield doc

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
