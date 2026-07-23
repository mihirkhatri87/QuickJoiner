"""Text extraction from Word/PowerPoint/Excel/PDF/HTML + the vision seam."""

from io import BytesIO

import pytest

from quickjoiner.ingest.extract import (
    ExtractionError,
    document_kind,
    extract_text,
    supported_extension,
)


def _docx_bytes(*paragraphs: str, table: list[list[str]] | None = None) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, val in enumerate(row):
                t.rows[r].cells[c].text = val
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def _pptx_bytes(title: str, notes: str = "") -> bytes:
    from pptx import Presentation

    p = Presentation()
    slide = p.slides.add_slide(p.slide_layouts[5])
    slide.shapes.title.text = title
    if notes:
        slide.notes_slide.notes_text_frame.text = notes
    b = BytesIO()
    p.save(b)
    return b.getvalue()


def _xlsx_bytes(sheet: str, rows: list[list]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    b = BytesIO()
    wb.save(b)
    return b.getvalue()


def test_docx_paragraphs_and_tables():
    text = extract_text(_docx_bytes("Deploy via Octopus.", table=[["Env", "Prod"]]), "guide.docx")
    assert "Deploy via Octopus." in text
    assert "Env | Prod" in text


def test_pptx_title_and_speaker_notes():
    text = extract_text(_pptx_bytes("Architecture", notes="remember the queue"), "deck.pptx")
    assert "Slide 1:" in text and "Architecture" in text
    assert "[speaker notes] remember the queue" in text


def test_xlsx_sheet_rows_are_tab_joined():
    text = extract_text(_xlsx_bytes("Services", [["name", "env"], ["api", "prod"]]), "s.xlsx")
    assert "Sheet: Services" in text
    assert "api\tprod" in text


def test_pdf_born_digital_text_extracted():
    # A blank/image-only PDF has no text layer → empty (honest), not an error.
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    b = BytesIO()
    w.write(b)
    assert extract_text(b.getvalue(), "scan.pdf") == ""


def test_html_keeps_image_alt_text_and_strips_chrome():
    html = b"<html><body><nav>menu</nav><h1>Docs</h1>" \
           b'<img alt="system diagram" src="a.png"><p>hello</p><script>x()</script></body></html>'
    text = extract_text(html, "page.html")
    assert "Docs" in text and "hello" in text
    assert "[image: system diagram]" in text
    assert "menu" not in text and "x()" not in text


def test_plaintext_and_json_are_decoded():
    assert extract_text(b'{"a": 1}', "x.json") == '{"a": 1}'
    assert extract_text("café".encode("utf-8"), "x.txt") == "café"


def test_corrupt_office_file_raises_extraction_error():
    with pytest.raises(ExtractionError):
        extract_text(b"not a real docx", "broken.docx")


def test_supported_extension_and_kind():
    assert supported_extension("a.docx") and supported_extension("a.pdf")
    assert supported_extension("README") and supported_extension("notes.md")
    assert not supported_extension("photo.png")
    assert document_kind("main.py") == "code"
    assert document_kind("report.pdf") == "doc"


def test_vision_seam_is_inert_without_a_handler_and_called_with_one():
    """The image_handler seam (plan 07 / roadmap #23): off by default, and when supplied it is
    handed the embedded images to derive extra text from — proving vision plugs in with no
    extractor changes."""
    calls: list[str] = []

    def handler(blob: bytes, mime: str) -> str:
        calls.append(mime)
        return "a flowchart of the deploy pipeline"

    # A PPTX with a real embedded picture.
    from pptx import Presentation
    from pptx.util import Inches

    p = Presentation()
    slide = p.slides.add_slide(p.slide_layouts[6])
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
           b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
           b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    slide.shapes.add_picture(BytesIO(png), Inches(1), Inches(1))
    b = BytesIO()
    p.save(b)
    data = b.getvalue()

    assert "flowchart" not in extract_text(data, "deck.pptx")  # inert without a handler
    with_vision = extract_text(data, "deck.pptx", image_handler=handler)
    assert "a flowchart of the deploy pipeline" in with_vision
    assert calls  # the handler was actually invoked with the embedded image
