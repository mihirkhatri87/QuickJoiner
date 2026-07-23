"""Text extraction from document formats — the single choke point that turns any
supported file into plain text for ingestion.

Every ingestion surface funnels through here: the `files`/`git` connectors, the rolling
`uploads` connector, and the upload endpoints (chat drag-drop + `/qj`). Office/PDF parsing
is lazy and defensive: a missing optional library or a corrupt/encrypted file raises
`ExtractionError` with a clear message instead of crashing a whole sync, so the caller can
skip just that one file. Plain-text/code/markdown/json/html are decoded (HTML stripped to
text) with no extra dependency.

Keep `DOC_EXTENSIONS` (needs a binary parser) and `TEXT_EXTENSIONS` (decode directly) in
sync with what `connectors/files.py` offers to ingest and what `connectors/specs.py`
advertises the uploads connector accepts.
"""

from __future__ import annotations

from io import BytesIO
from typing import Callable, Iterable

# ── Vision seam (text-first today; multimodal tomorrow) ────────────────────────────────────
# An ImageHandler turns ONE embedded image (raw bytes, mime type) into derived text — an OCR
# result or a vision-model caption. It is the single hook that lets the planned multimodal
# layer (docs/plans/07-multimodal-derive-to-text.md, AI_ROADMAP #23) read images inside PDFs,
# slide decks and web pages WITHOUT touching these extractors or any ingestion surface: the app
# builds a handler from a configured vision provider and threads it in via `extract_text(...,
# image_handler=...)`. None today ⇒ pure text extraction, images skipped honestly. The office/
# PDF/HTML extractors already enumerate their embedded images and call the handler when one is
# supplied, so switching vision on is a wiring change, not a rewrite (see `_derive_images`).
ImageHandler = Callable[[bytes, str], str]

# Office / PDF formats that need a real parser (bytes in, text out). The old binary
# Office formats (.doc/.ppt/.xls) are deliberately absent — the modern OOXML parsers below
# don't read them, so we skip rather than emit garbage; re-save as .docx/.pptx/.xlsx.
DOC_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}
HTML_EXTENSIONS = {".html", ".htm"}

# Plain-text / structured-text / code we ingest by decoding (shared with files.py). Kept here
# so the extraction contract lives in one module; files.py imports these.
TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".text", ".rst", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go",
    ".rb", ".php", ".rs", ".c", ".h", ".cpp", ".hpp", ".sql", ".sh", ".ps1", ".psm1",
    ".yaml", ".yml", ".json", ".jsonl", ".ndjson", ".toml", ".ini", ".cfg", ".xml",
    ".html", ".htm", ".css", ".tf", ".dockerfile", ".gradle", ".properties", ".csv", ".tsv",
    # dependency manifests — the raw evidence of cross-repo links (see deps.py)
    ".csproj", ".vbproj", ".fsproj", ".sln", ".props", ".targets", ".nuspec",
    ".config", ".mod", ".kts",
}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go", ".rb", ".php", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".sql", ".sh", ".ps1", ".psm1",
}
NAMED_TEXT_FILES = {
    "readme", "license", "notice", "changelog", "contributing", "authors", "owners",
    "codeowners", "dockerfile", "makefile", "jenkinsfile", "vagrantfile", "gemfile",
    "rakefile", "procfile", "pipfile", ".gitignore", ".gitattributes", ".editorconfig",
    ".env.example",
}

# Guard rails for pathological inputs (a spreadsheet with a million blank rows, a PDF whose
# text layer is enormous). These bound work, not correctness — the pipeline chunks downstream.
_MAX_SHEET_ROWS = 5000
_MAX_PDF_PAGES = 2000


class ExtractionError(Exception):
    """Raised when a file cannot be turned into text (missing parser, corrupt/encrypted
    file, unsupported format). Control flow for the caller to skip one file, not a bug."""


def _lazy_import(module: str, pip_name: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ExtractionError(
            f"parsing this file needs the '{pip_name}' library (pip install {pip_name})"
        ) from exc


def _derive_images(images: Iterable[tuple[bytes, str]], handler: ImageHandler | None) -> list[str]:
    """Turn embedded images into derived text via the vision/OCR `handler`. Inert (returns [])
    when no handler is supplied — the text-first default — so enumerating images costs nothing
    today. A per-image failure is swallowed: one bad image must never fail a whole document.
    This is where the multimodal layer (plan 07 / roadmap #23) starts contributing."""
    if handler is None:
        return []
    out: list[str] = []
    for blob, mime in images:
        try:
            text = (handler(blob, mime) or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if text:
            out.append(text)
    return out


def _extract_pdf(data: bytes, image_handler: ImageHandler | None = None) -> str:
    _lazy_import("pypdf", "pypdf")
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # many "encrypted" PDFs use an empty owner password
            except Exception:  # noqa: BLE001
                raise ExtractionError("PDF is password-protected")
        parts: list[str] = []
        for page in reader.pages[:_MAX_PDF_PAGES]:
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(text)
            # Vision seam: scanned/image-only pages have no text layer, and diagrams live only
            # in images — hand each to the vision handler when one is wired (plan 07 / #23).
            if image_handler is not None:
                imgs = ((img.data, "image/" + (img.name.rsplit(".", 1)[-1].lower() if "." in img.name else "png"))
                        for img in getattr(page, "images", []))
                parts.extend(_derive_images(imgs, image_handler))
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"could not read PDF: {exc}") from exc
    return "\n\n".join(parts)


def _extract_docx(data: bytes, image_handler: ImageHandler | None = None) -> str:
    _lazy_import("docx", "python-docx")
    import docx

    try:
        document = docx.Document(BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"could not read Word document: {exc}") from exc
    parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    # Vision seam: inline images live in the package part store (plan 07 / roadmap #23).
    if image_handler is not None:
        imgs = ((part.blob, getattr(part, "content_type", "image/png"))
                for part in document.part.package.iter_parts()
                if "image" in getattr(part, "content_type", ""))
        parts.extend(_derive_images(imgs, image_handler))
    return "\n".join(parts)


def _extract_pptx(data: bytes, image_handler: ImageHandler | None = None) -> str:
    _lazy_import("pptx", "python-pptx")
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    try:
        prs = Presentation(BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"could not read PowerPoint file: {exc}") from exc
    slides: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        lines: list[str] = []
        images: list[tuple[bytes, str]] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        lines.append(" | ".join(cells))
            # Vision seam: slide diagrams/screenshots are pictures (plan 07 / roadmap #23).
            if image_handler is not None and shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    images.append((shape.image.blob, shape.image.content_type))
                except Exception:  # noqa: BLE001 — some pictures have no retrievable blob
                    pass
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"[speaker notes] {notes}")
        lines.extend(_derive_images(images, image_handler))
        if lines:
            slides.append(f"Slide {i}:\n" + "\n".join(lines))
    return "\n\n".join(slides)


def _extract_xlsx(data: bytes, image_handler: ImageHandler | None = None) -> str:
    # Spreadsheets are tabular text; embedded charts/images are out of scope even for the
    # vision seam (a chart's data is its cells, which we already read).
    _lazy_import("openpyxl", "openpyxl")
    import openpyxl

    try:
        wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"could not read Excel workbook: {exc}") from exc
    try:
        sheets: list[str] = []
        for ws in wb.worksheets:
            rows: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    rows.append("\t".join(cells))
                if len(rows) >= _MAX_SHEET_ROWS:
                    rows.append(f"… (truncated at {_MAX_SHEET_ROWS} rows)")
                    break
            if rows:
                sheets.append(f"Sheet: {ws.title}\n" + "\n".join(rows))
        return "\n\n".join(sheets)
    finally:
        wb.close()


def _extract_html(data: bytes, image_handler: ImageHandler | None = None) -> str:
    import base64

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    # Keep image alt/title captions as text — a free, dependency-less win now (many diagrams
    # ship a meaningful alt); replace the <img> with its caption so it lands inline.
    derived: list[str] = []
    for img in soup.find_all("img"):
        caption = (img.get("alt") or img.get("title") or "").strip()
        src = img.get("src") or ""
        # Vision seam: inline data-URI images can be decoded and described (plan 07 / #23).
        if image_handler is not None and src.startswith("data:image/"):
            try:
                header, b64 = src.split(",", 1)
                mime = header[5:].split(";", 1)[0]
                derived.extend(_derive_images([(base64.b64decode(b64), mime)], image_handler))
            except Exception:  # noqa: BLE001
                pass
        img.replace_with(f"[image: {caption}]" if caption else "")
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return "\n".join([text, *derived]).strip()


_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".pptx": _extract_pptx,
    ".xlsx": _extract_xlsx,
    ".html": _extract_html,
    ".htm": _extract_html,
}


def is_extractable(filename: str) -> bool:
    """True if `filename` is an office/PDF format needing a binary parser (as opposed to a
    plain-text file we can just decode). Used to decide the read size cap and byte-mode read."""
    return _ext(filename) in DOC_EXTENSIONS


def supported_extension(filename: str) -> bool:
    """True if we can ingest this file at all — a text/code format OR an office/PDF one."""
    ext = _ext(filename)
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return (
        ext in TEXT_EXTENSIONS
        or ext in DOC_EXTENSIONS
        or name in NAMED_TEXT_FILES
        or stem in NAMED_TEXT_FILES
    )


def document_kind(filename: str) -> str:
    """The chunker hint for a file: 'code' for source files, 'doc' otherwise."""
    return "code" if _ext(filename) in CODE_EXTENSIONS else "doc"


def extract_text(data: bytes, filename: str, image_handler: ImageHandler | None = None) -> str:
    """Turn raw bytes into plain text based on `filename`'s extension.

    Office/PDF/HTML route through a real parser; everything else is decoded as UTF-8
    (invalid bytes replaced). Raises `ExtractionError` when a parser is missing or the file
    is corrupt/encrypted — the caller skips that one file. The result may be empty (an empty
    or image-only document with no text layer and no vision handler); the caller decides
    whether an empty result is worth ingesting.

    `image_handler` (default None ⇒ text-first, images skipped) is the multimodal seam: when
    the planned vision layer (plan 07 / roadmap #23) supplies one, embedded images and scanned
    pages are described into text alongside the extracted text — no other code changes.
    """
    extractor = _EXTRACTORS.get(_ext(filename))
    if extractor is not None:
        return extractor(data, image_handler)
    return data.decode("utf-8", errors="replace")


def _ext(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""
