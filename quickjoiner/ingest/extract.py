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

import re
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

# Archives are expanded in place: one .zip becomes the concatenated text of the members
# we can read, each under a header naming it. Bounded hard (see _MAX_ARCHIVE_*) because
# an archive is the classic decompression-bomb vector, and nested archives are listed
# but never opened — depth is where bombs live, and a zip inside a zip is rare enough
# that skipping it honestly beats recursing carefully.
ARCHIVE_EXTENSIONS = {".zip"}

# Known image formats. QuickJoiner cannot read these yet: with no vision `image_handler`
# wired in, `extract_text` raises a *specific* ExtractionError naming the limitation
# rather than pretending the file was empty. When the vision layer lands
# (docs/plans/07-multimodal-derive-to-text.md, AI_ROADMAP #23) it supplies a handler and
# these start working with no change here — that is the whole point of the seam.
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".svg",
}

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
    # More of the plain-text world a real document store contains: docs in other markup,
    # transcripts (the text half of a meeting recording), notebooks (JSON), and the
    # config/scripting formats that turn up beside them.
    ".adoc", ".asciidoc", ".org", ".tex", ".bib", ".vtt", ".srt", ".ipynb",
    ".conf", ".env", ".editorconfig", ".lock", ".tfvars", ".hcl", ".proto", ".graphql",
    ".bat", ".cmd", ".bash", ".zsh", ".fish", ".make", ".cmake",
    ".vue", ".svelte", ".scala", ".kt", ".swift", ".r", ".jl", ".pl", ".lua",
    ".groovy", ".dart", ".ex", ".exs", ".erl", ".clj", ".fs", ".vb", ".m", ".mm",
}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go", ".rb", ".php", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".sql", ".sh", ".ps1", ".psm1",
    ".vue", ".svelte", ".scala", ".kt", ".swift", ".r", ".jl", ".pl", ".lua",
    ".groovy", ".dart", ".ex", ".exs", ".erl", ".clj", ".fs", ".vb", ".m", ".mm",
    ".bat", ".cmd", ".bash", ".zsh", ".fish", ".proto", ".graphql",
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
# Archive rails. The uncompressed cap is the decompression-bomb guard: a few-KB zip can
# claim to hold petabytes, so members are read with an explicit budget and the moment it
# is spent we stop and SAY we stopped, rather than expanding until the process dies.
_MAX_ARCHIVE_MEMBERS = 300
_MAX_ARCHIVE_BYTES = 60_000_000
_MAX_ARCHIVE_MEMBER_BYTES = 20_000_000


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


_WORDML_T = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
_WORDML_P = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
_WORDML_TXBX = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}txbxContent"


def _docx_textbox_texts(document) -> list[str]:
    """Text inside Word text boxes / shapes, which `document.paragraphs` never reaches."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(_element_bytes(document.element.body))
    except Exception:  # noqa: BLE001
        return []
    texts: list[str] = []
    for box in root.iter(_WORDML_TXBX):
        joined = "".join(el.text or "" for el in box.iter(_WORDML_T)).strip()
        if joined:
            texts.append(joined)
    return texts


def _salvage_docx(data: bytes) -> str:
    """Text from a damaged Word package, straight out of the XML — same rescue as
    `_salvage_pptx`, for the same reason: one corrupt image must not cost the document."""
    import xml.etree.ElementTree as ET

    def is_text_part(name: str) -> bool:
        return name.startswith(("word/document", "word/header", "word/footer")) and name.endswith(".xml")

    lines: list[str] = []
    seen: set[str] = set()
    for _name, blob in sorted(_zip_text_members(data, is_text_part)):
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            continue
        for para in root.iter(_WORDML_P):
            text = "".join(t.text or "" for t in para.iter(_WORDML_T)).strip()
            if text and text not in seen:
                seen.add(text)
                lines.append(text)
    return "\n".join(lines)


def _extract_docx(data: bytes, image_handler: ImageHandler | None = None) -> str:
    _lazy_import("docx", "python-docx")
    import docx

    try:
        document = docx.Document(BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        salvaged = _salvage_docx(data)
        if salvaged.strip():
            return salvaged
        raise ExtractionError(f"could not read Word document: {exc}") from exc
    seen: set[str] = set()
    parts: list[str] = []

    def add(text: str) -> None:
        text = (text or "").strip()
        if text and text not in seen:
            seen.add(text)
            parts.append(text)

    for paragraph in document.paragraphs:
        add(paragraph.text)
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                add(" | ".join(cells))
    # `document.paragraphs` walks the body's top level only, so text inside **text boxes**
    # and shapes is invisible to it — the same class of silent loss that made PowerPoint
    # architecture decks extract as nothing. Headers/footers live outside the body entirely
    # and carry document titles, classification markings and version stamps.
    for text in _docx_textbox_texts(document):
        add(text)
    for section in document.sections:
        for container in (section.header, section.footer):
            try:
                for paragraph in container.paragraphs:
                    add(paragraph.text)
            except Exception:  # noqa: BLE001 — an odd section must not fail the document
                continue
    # Vision seam: inline images live in the package part store (plan 07 / roadmap #23).
    if image_handler is not None:
        imgs = ((part.blob, getattr(part, "content_type", "image/png"))
                for part in document.part.package.iter_parts()
                if "image" in getattr(part, "content_type", ""))
        parts.extend(_derive_images(imgs, image_handler))
    return "\n".join(parts)


#: DrawingML text runs — the one namespace every Office diagram format puts its text in.
_DRAWINGML = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_DRAWINGML_T = _DRAWINGML + "t"
_A_P = _DRAWINGML + "p"
_A_TBL = _DRAWINGML + "tbl"
_A_TR = _DRAWINGML + "tr"
_A_TC = _DRAWINGML + "tc"
#: Markup-compatibility wrapper. PowerPoint wraps a shape in this whenever it uses a feature
#: that needs a legacy fallback (SmartArt, icons, 3D, ink, newer effects) — and python-pptx's
#: shape iteration whitelists only p:sp/p:grpSp/p:graphicFrame/p:cxnSp/p:pic/p:contentPart, so
#: EVERY shape inside one is invisible to the object model. Verified by reading
#: pptx/oxml/shapes/groupshape.py and by reproduction. This is the single biggest source of
#: "the deck clearly has text but nothing was extracted".
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_MC_ALT = _MC + "AlternateContent"
_MC_CHOICE = _MC + "Choice"
_MC_FALLBACK = _MC + "Fallback"


def _drawingml_lines(element) -> list[str]:
    """Every DrawingML paragraph under `element`, in document order.

    The safety net beneath the python-pptx object model. Two subtleties that matter:

    * **AlternateContent** holds the SAME content twice (a modern `mc:Choice` and a legacy
      `mc:Fallback`). We descend into exactly one — Choice when present — or every such
      shape's text would be duplicated.
    * **Tables** are emitted as joined `a | b | c` rows, matching the structured pass's
      format, so the two agree and dedupe cleanly instead of producing a joined row *and*
      its individual cells.
    """
    out: list[str] = []

    def para_text(node) -> str:
        return "".join(t.text or "" for t in node.iter(_DRAWINGML_T)).strip()

    def walk(node) -> None:
        tag = node.tag
        if not isinstance(tag, str):  # comments / processing instructions
            return
        if tag == _MC_ALT:
            chosen = next((c for c in node if c.tag == _MC_CHOICE), None)
            if chosen is None:
                chosen = next((c for c in node if c.tag == _MC_FALLBACK), None)
            if chosen is not None:
                walk(chosen)
            return
        if tag == _A_TBL:
            for row in node.iter(_A_TR):
                cells = [
                    " ".join(filter(None, (para_text(p) for p in cell.iter(_A_P)))).strip()
                    for cell in row.iter(_A_TC)
                ]
                if any(cells):
                    out.append(" | ".join(cells))
            return
        if tag == _A_P:
            text = para_text(node)
            if text:
                out.append(text)
            return
        for child in node:
            walk(child)

    walk(element)
    return out
#: Nested groups are rare beyond 2-3 deep; the cap is a cycle/pathology guard, not a limit.
_MAX_GROUP_DEPTH = 12


def _xml_run_texts(blob: bytes) -> list[str]:
    """Every DrawingML text run in a raw XML part, in document order.

    Used for content whose text python-pptx does not model — chiefly **SmartArt**, whose
    text lives in a separate `diagramData` part referenced from the slide, not in the
    slide XML at all.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return []
    return [t.strip() for t in (el.text for el in root.iter(_DRAWINGML_T)) if t and t.strip()]


def _pptx_chart_lines(shape) -> list[str]:
    """Title, series names and category labels of an embedded chart. Chart text lives in a
    chart part, so neither the shape's text frame nor its XML contains it."""
    lines: list[str] = []
    try:
        chart = shape.chart
        if chart.has_title and chart.chart_title.text_frame.text.strip():
            lines.append(chart.chart_title.text_frame.text.strip())
        for series in chart.series:
            if getattr(series, "name", "").strip():
                lines.append(f"series: {series.name.strip()}")
        for plot in chart.plots:
            cats = [str(c).strip() for c in plot.categories if str(c).strip()]
            if cats:
                lines.append("categories: " + " | ".join(cats))
    except Exception:  # noqa: BLE001 — chart parts vary wildly; never fail an extraction on one
        pass
    return lines


def _pptx_walk(shapes, lines, images, image_handler, seen, depth=0) -> None:
    """Collect text from a shape tree, **recursing into groups**.

    Recursion is the whole point: `slide.shapes` yields only top-level shapes, so every
    label inside a grouped diagram used to be silently dropped — and a PowerPoint
    architecture diagram is precisely a group of labelled boxes. A deck could extract to
    almost nothing and report success (found live: an attached architecture deck produced
    no context at all, and the agent correctly said it had no content to work with).
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def add(text: str) -> None:
        text = text.strip()
        if text and text not in seen:
            seen.add(text)
            lines.append(text)

    for shape in shapes:
        try:
            stype = shape.shape_type
        except Exception:  # noqa: BLE001 — python-pptx raises on a few exotic shape types
            stype = None

        if stype == MSO_SHAPE_TYPE.GROUP:
            if depth < _MAX_GROUP_DEPTH:
                _pptx_walk(shape.shapes, lines, images, image_handler, seen, depth + 1)
            continue

        if shape.has_text_frame:
            add(shape.text_frame.text)
        if shape.has_table:
            for row in shape.table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    add(" | ".join(cells))
        if getattr(shape, "has_chart", False):
            for line in _pptx_chart_lines(shape):
                add(line)
        # Vision seam: slide diagrams/screenshots are pictures (plan 07 / roadmap #23).
        if image_handler is not None and stype == MSO_SHAPE_TYPE.PICTURE:
            try:
                images.append((shape.image.blob, shape.image.content_type))
            except Exception:  # noqa: BLE001 — some pictures have no retrievable blob
                pass
        # Anything else holding DrawingML text inline (embedded objects, ink annotations,
        # exotic frames). SmartArt is handled per-slide instead: its text is in another part.
        if not shape.has_text_frame and shape._element.tag.endswith("}graphicFrame"):
            for text in _xml_run_texts(_element_bytes(shape._element)):
                add(text)


def _element_bytes(element) -> bytes:
    import xml.etree.ElementTree as ET

    try:
        return ET.tostring(element)
    except Exception:  # noqa: BLE001 — lxml elements serialise, but never fail extraction on it
        return b""


#: Embedded Office documents inside a deck (a pasted-in workbook or document). Their text is
#: in a whole separate package, so it is extracted by recursing into the right parser.
_EMBEDDED_PARSERS = {
    "spreadsheetml.sheet": ".xlsx",
    "wordprocessingml.document": ".docx",
}


def _pptx_related_texts(slide) -> list[str]:
    """Text that lives in parts the slide only *references*, not in the slide XML.

    Three cases, all common in architecture decks and all invisible without following the
    relationship:

    * **SmartArt** — the slide holds a `dgm:relIds` pointer; the labels are in a
      `diagramData` part.
    * **Charts** — titles, axis and cached series/category names live in a chart part.
    * **Embedded workbooks/documents** — a pasted Excel table is an entire xlsx package
      stored as an embedding; it is parsed with the same extractor a loose file would get.
    """
    import xml.etree.ElementTree as ET

    texts: list[str] = []
    try:
        rels = list(slide.part.rels.values())
    except Exception:  # noqa: BLE001
        return texts

    for rel in rels:
        try:
            if rel.is_external:
                continue
            part = rel.target_part
            ctype = getattr(part, "content_type", "") or ""
            blob = part.blob
        except Exception:  # noqa: BLE001 — a broken relationship must not fail the deck
            continue
        try:
            if "diagramData" in ctype:
                texts.extend(_xml_run_texts(blob))
            elif "chart" in ctype and ctype.endswith("xml"):
                texts.extend(_xml_run_texts(blob))
                # Cached series names/categories are `c:v` values, not DrawingML runs.
                root = ET.fromstring(blob)
                for value in root.iter():
                    if value.tag.endswith("}v") and (value.text or "").strip():
                        candidate = value.text.strip()
                        # Numbers are plot data, not labels — they add noise, not meaning.
                        if not _looks_numeric(candidate):
                            texts.append(candidate)
            else:
                suffix = next((s for key, s in _EMBEDDED_PARSERS.items() if key in ctype), None)
                if suffix and len(blob) <= _MAX_ARCHIVE_MEMBER_BYTES:
                    texts.extend(extract_text(blob, "embedded" + suffix).splitlines())
        except (ExtractionError, ET.ParseError, Exception):  # noqa: BLE001
            continue
    return [t.strip() for t in texts if t and t.strip()]


def _looks_numeric(text: str) -> bool:
    try:
        float(text.replace(",", ""))
        return True
    except ValueError:
        return False


def _zip_text_members(data: bytes, matches) -> list[tuple[str, bytes]]:
    """Read the members of a possibly-damaged package that `matches(name)` accepts.

    Office files are zips, and a single corrupt member makes the *whole* package
    unreadable to python-pptx/python-docx: reported live as `Bad CRC-32 for file
    'ppt/media/image7.png'` — one damaged image, and every slide's text was lost, even
    though text and images share nothing but the container. (PowerPoint itself opens such
    files happily, so the deck looks perfectly fine to the person sending it.)

    So: read members individually, skip the ones that fail, and — for a member we actually
    need — fall back to reading it without CRC verification, because a checksum mismatch
    does not mean the bytes are useless.
    """
    import zipfile

    out: list[tuple[str, bytes]] = []
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExtractionError(f"not a readable Office package: {exc}") from exc
    for info in archive.infolist():
        if info.is_dir() or not matches(info.filename):
            continue
        try:
            out.append((info.filename, archive.read(info)))
        except Exception:  # noqa: BLE001 — corrupt member: try once more without the CRC check
            try:
                handle = archive.open(info)
                handle._expected_crc = None  # type: ignore[attr-defined]
                out.append((info.filename, handle.read()))
            except Exception:  # noqa: BLE001 — genuinely unrecoverable; skip this member
                continue
    return out


def _slide_sort_key(name: str) -> tuple[int, str]:
    digits = "".join(c for c in name.rsplit("/", 1)[-1] if c.isdigit())
    return (int(digits) if digits else 0, name)


def _salvage_pptx(data: bytes) -> str:
    """Extract text straight from a damaged deck's XML, bypassing the object model entirely.

    Reached only when `Presentation()` cannot open the file. Text lives in the slide,
    notes, diagram and chart parts — never in `ppt/media/*` — so a corrupt image costs
    nothing here.
    """
    def is_text_part(name: str) -> bool:
        return (
            (name.startswith("ppt/slides/slide") and name.endswith(".xml"))
            or (name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml"))
            or (name.startswith("ppt/diagrams/data") and name.endswith(".xml"))
            or (name.startswith("ppt/charts/chart") and name.endswith(".xml"))
        )

    import xml.etree.ElementTree as ET

    members = sorted(_zip_text_members(data, is_text_part), key=lambda m: _slide_sort_key(m[0]))
    slides: list[str] = []
    extras: list[str] = []
    for name, blob in members:
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            continue
        seen: set[str] = set()
        lines = [ln for ln in _drawingml_lines(root) if not (ln in seen or seen.add(ln))]
        if not lines:
            continue
        if name.startswith("ppt/slides/"):
            slides.append(f"Slide {_slide_sort_key(name)[0]}:\n" + "\n".join(lines))
        elif name.startswith("ppt/notesSlides/"):
            extras.append("[speaker notes] " + " ".join(lines))
        else:
            extras.append("\n".join(lines))
    return "\n\n".join(slides + extras)


def _extract_pptx(data: bytes, image_handler: ImageHandler | None = None) -> str:
    _lazy_import("pptx", "python-pptx")
    from pptx import Presentation

    try:
        prs = Presentation(BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        # The package is damaged (a corrupt media part is the common case). Salvage the
        # text from the XML rather than losing an entire deck to one bad image.
        salvaged = _salvage_pptx(data)
        if salvaged.strip():
            return salvaged
        raise ExtractionError(f"could not read PowerPoint file: {exc}") from exc
    slides: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        lines: list[str] = []
        images: list[tuple[bytes, str]] = []
        seen: set[str] = set()

        # 1. Structured pass — the object model, where it is richest: it knows a table from a
        #    text box, reads charts, and gives shapes in visual order.
        _pptx_walk(slide.shapes, lines, images, image_handler, seen)

        # 2. Safety net — sweep the slide XML directly. python-pptx's shape iteration
        #    whitelists six tags, so anything else (above all mc:AlternateContent-wrapped
        #    shapes, which modern PowerPoint emits constantly) is invisible to pass 1 and
        #    would be lost SILENTLY. Deduped against what pass 1 already found, so this only
        #    ever adds. Appended rather than interleaved: exact visual position is
        #    unrecoverable here, and completeness beats ordering for retrieval.
        try:
            for text in _drawingml_lines(slide.part.element if hasattr(slide.part, "element")
                                         else slide.shapes._spTree):
                if text not in seen:
                    seen.add(text)
                    lines.append(text)
        except Exception:  # noqa: BLE001 — the safety net must never be what breaks a deck
            pass

        # 3. Parts the slide only references: SmartArt, charts, embedded workbooks.
        for text in _pptx_related_texts(slide):
            if text not in seen:
                seen.add(text)
                lines.append(text)

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


# ── HTML tables ───────────────────────────────────────────────────────────────────────────
# `soup.get_text()` puts every cell on its own line, which destroys a table outright: the
# column each value belonged to is gone, and — worse — a BLANK cell simply vanishes, so every
# later value in that row shifts left into the wrong column. Measured on a real internal
# service catalogue: a member row missing only its email rendered its Location where Phone
# belonged, and nothing in the stored text said so. Rows are therefore rendered as markdown
# pipe rows instead, which keeps the header, keeps the alignment, keeps empty cells as empty,
# and makes each row a self-contained line that chunks and embeds far better than a vertical
# stream of orphaned cells. `ingest/tables.py` then mines these rows for graph edges.
_MAX_TABLE_ROWS = 500        # a rendered table past this is truncated, and says so
_MAX_CELL_CHARS = 300        # one runaway cell can't dominate the document
_HEADER_BLANK_TOLERANCE = 2  # header labels may trail off into spacer/actions columns


def _cell_text(cell) -> str:
    """One cell's text, whitespace-collapsed, `|` escaped so it can't break the row."""
    text = " ".join((cell.get_text(" ", strip=True) or "").split())
    if len(text) > _MAX_CELL_CHARS:
        text = text[:_MAX_CELL_CHARS] + "…"
    return text.replace("|", "\\|")


def _table_rows(table) -> list[list[str]]:
    """Cells per row, with colspan expanded to empty padding so columns stay aligned."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr", recursive=True):
        # Only cells belonging to THIS table — a nested table has already been rendered
        # into a string by the caller (innermost-first), so nothing is double-counted.
        cells = [c for c in tr.find_all(["th", "td"], recursive=False)]
        if not cells:
            continue
        row: list[str] = []
        for c in cells:
            row.append(_cell_text(c))
            try:
                span = int(c.get("colspan") or 1)
            except (TypeError, ValueError):
                span = 1
            row.extend([""] * max(0, min(span, 20) - 1))
        rows.append(row)
    return rows


def render_html_table(table) -> str:
    """One `<table>` element as a markdown pipe table (header + rows), or plain lines
    when it is a layout table rather than a data table.

    Single-column tables are old-fashioned page layout, not data; rendering those as
    pipe rows would wrap ordinary prose in table syntax for no gain, so they degrade to
    their text. Everything else keeps its shape.
    """
    rows = _table_rows(table)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    if width < 2:
        return "\n".join(c for r in rows for c in r if c)

    truncated = len(rows) > _MAX_TABLE_ROWS
    if truncated:
        rows = rows[:_MAX_TABLE_ROWS]
    rows = [r + [""] * (width - len(r)) for r in rows]

    # A header row is one made of <th>, else the first row when it is MOSTLY populated.
    # "Fully populated" was too strict: a real header ending in a spacer/actions column
    # (`Name | Role | Email | `) has one empty cell and was demoted to a data row, leaving
    # the table headerless — so every column went untyped and `ingest/tables.py` extracted
    # nothing from it. Measured on a live team-roster page: 0 edges from 7 members.
    first_tr = table.find("tr")
    has_th = bool(first_tr and first_tr.find("th"))
    filled = sum(1 for c in rows[0] if c)
    looks_like_header = filled >= 2 and filled >= width - _HEADER_BLANK_TOLERANCE
    header, body = (rows[0], rows[1:]) if (has_th or looks_like_header) else ([""] * width, rows)

    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    out.extend("| " + " | ".join(r) + " |" for r in body)
    if truncated:
        out.append(f"… (table truncated at {_MAX_TABLE_ROWS} rows)")
    return "\n".join(out)


def _extract_html(data: bytes, image_handler: ImageHandler | None = None) -> str:
    import base64

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    # Keep image alt/title captions as text — a free, dependency-less win now (many diagrams
    # ship a meaningful alt); replace the <img> with its caption so it lands inline. Runs
    # BEFORE table rendering so an image inside a cell still contributes its caption (and
    # still reaches the vision seam) instead of being destroyed with the table element.
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
    # Only tables that contain no other table are rendered. A table nested inside another
    # is almost always the meaningful one — old intranet apps wrap real data tables in
    # layout tables — so rendering the outer one would bury the data in a single cell.
    # The skipped wrapper still contributes its text normally.
    for table in soup.find_all("table"):
        if table.find("table") is not None:
            continue
        table.replace_with("\n" + render_html_table(table) + "\n")
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return "\n".join([text, *derived]).strip()


def _extract_zip(data: bytes, image_handler: ImageHandler | None = None) -> str:
    """Expand an archive into the concatenated text of the members we can read.

    Each member is introduced by a `--- path/inside.docx ---` header so the resulting
    document still says where every passage came from, and members route back through
    `extract_text` — so a .docx inside a .zip is parsed exactly as a loose one would be.

    Deliberate limits, all reported in the output rather than applied silently:
      * nested archives are listed, never opened (depth is where zip bombs live);
      * a member that cannot be parsed is noted and skipped, not fatal;
      * expansion stops at the member/byte budget and says so.
    """
    import io
    import zipfile

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExtractionError(f"not a readable zip archive: {exc}") from exc

    parts: list[str] = []
    notes: list[str] = []
    budget = _MAX_ARCHIVE_BYTES
    members = 0

    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if members >= _MAX_ARCHIVE_MEMBERS:
            notes.append(f"stopped after {_MAX_ARCHIVE_MEMBERS} files — archive has more")
            break
        if budget <= 0:
            notes.append("stopped at the uncompressed-size budget — archive has more")
            break
        ext = _ext(name)
        if ext in ARCHIVE_EXTENSIONS:
            notes.append(f"{name}: nested archive, not expanded")
            continue
        if ext in IMAGE_EXTENSIONS and image_handler is None:
            notes.append(f"{name}: image, not read yet (needs vision support)")
            continue
        if not supported_extension(name) and ext not in IMAGE_EXTENSIONS:
            continue  # binaries and unknown formats: silent, they are noise not loss
        if info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            notes.append(f"{name}: skipped, larger than "
                         f"{_MAX_ARCHIVE_MEMBER_BYTES // 1_000_000} MB")
            continue
        try:
            with archive.open(info) as handle:
                payload = handle.read(min(budget, _MAX_ARCHIVE_MEMBER_BYTES) + 1)
        except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
            # RuntimeError is what zipfile raises for an encrypted member.
            notes.append(f"{name}: unreadable ({exc})")
            continue
        budget -= len(payload)
        members += 1
        try:
            text = extract_text(payload, name, image_handler)
        except ExtractionError as exc:
            notes.append(f"{name}: {exc}")
            continue
        if text.strip():
            parts.append(f"--- {name} ---\n{text.strip()}")

    if not parts and not notes:
        raise ExtractionError("archive contains no readable documents")
    if notes:
        parts.append("--- archive notes ---\n" + "\n".join(f"- {n}" for n in notes))
    return "\n\n".join(parts)


_ARCHIVE_HEADER_RE = re.compile(r"^--- (.+) ---$", re.MULTILINE)
_ARCHIVE_NOTES_RE = re.compile(r"^--- archive notes ---\n((?:- .+\n?)+)", re.MULTILINE)


def parse_archive_manifest(text: str) -> dict[str, list[str]]:
    """Recover the member list `_extract_zip` produced from the document's own stored text.

    There is nowhere else to get it from: chunks are the only place a document's original
    text lives (the catalog stores metadata + a hash, not the text — see
    `KnowledgeStore.get_document_chunks`), so the document browser's "what's inside this
    archive" view re-derives it by re-reading the same `--- path ---` headers this module
    wrote at ingest time, rather than needing a new schema column. Chunking may reorder
    nothing (chunks are ordered slices of the same text) and headers are always a whole
    line, so scanning the reassembled text is reliable. Returns empty lists for text with
    no archive headers — callers decide whether that's worth surfacing.
    """
    notes: list[str] = []
    notes_match = _ARCHIVE_NOTES_RE.search(text)
    if notes_match:
        notes = [ln[2:].strip() for ln in notes_match.group(1).splitlines() if ln.startswith("- ")]
    members = [name for name in _ARCHIVE_HEADER_RE.findall(text) if name != "archive notes"]
    return {"members": members, "notes": notes}


def _extract_image(data: bytes, image_handler: ImageHandler | None = None) -> str:
    """Images are the one format QuickJoiner knows about but cannot yet read.

    With a vision `image_handler` wired in this describes the image into text like any
    other content; without one it raises a *specific* error naming the missing
    capability, so an ingested image is reported as "needs vision support" rather than
    silently landing in memory as an empty document.
    """
    if image_handler is None:
        raise ExtractionError(
            "image files can't be read yet — vision support is on the roadmap "
            "(docs/plans/07-multimodal-derive-to-text.md)"
        )
    described = image_handler(data, "image")
    if not described.strip():
        raise ExtractionError("vision returned no description for this image")
    return described


_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".pptx": _extract_pptx,
    ".xlsx": _extract_xlsx,
    ".html": _extract_html,
    ".htm": _extract_html,
    ".zip": _extract_zip,
    **{ext: _extract_image for ext in IMAGE_EXTENSIONS},
}


def is_extractable(filename: str) -> bool:
    """True if `filename` is a binary format needing a real parser (office/PDF/archive)
    as opposed to a plain-text file we can just decode. Used to decide the read size cap
    and byte-mode read."""
    return _ext(filename) in DOC_EXTENSIONS or _ext(filename) in ARCHIVE_EXTENSIONS


def is_image(filename: str) -> bool:
    """True for formats we recognise as images. Separate from `supported_extension`
    because knowing what a file *is* and being able to read it are different questions
    until the vision layer lands."""
    return _ext(filename) in IMAGE_EXTENSIONS


def supported_extension(filename: str) -> bool:
    """True if we can ingest this file at all — a text/code format OR an office/PDF one."""
    ext = _ext(filename)
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    # Images are deliberately excluded: callers use this to decide whether to spend a
    # read on a file at all, and until vision is wired in an image read can only end in
    # an ExtractionError. `is_image` is how a caller tells "unknown" from "not yet".
    return (
        ext in TEXT_EXTENSIONS
        or ext in DOC_EXTENSIONS
        or ext in ARCHIVE_EXTENSIONS
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
