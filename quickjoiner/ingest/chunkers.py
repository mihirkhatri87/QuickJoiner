"""Content-aware chunking: markdown by headings, code by line blocks, prose by size."""

from __future__ import annotations

import re

MAX_CHARS = 1600
OVERLAP_CHARS = 200
CODE_LINES = 60
CODE_OVERLAP_LINES = 8


def chunk_document(text: str, kind: str = "doc") -> list[str]:
    text = text.strip()
    if not text:
        return []
    if kind == "code":
        return _chunk_code(text)
    if _looks_like_markdown(text):
        return _chunk_markdown(text)
    return _chunk_plain(text)


def _looks_like_markdown(text: str) -> bool:
    return bool(re.search(r"^#{1,4} ", text, flags=re.MULTILINE))


def _chunk_markdown(text: str) -> list[str]:
    # Split at headings, then size-limit each section.
    sections = re.split(r"(?=^#{1,4} )", text, flags=re.MULTILINE)
    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if section:
            chunks.extend(_chunk_plain(section))
    return chunks


def _chunk_plain(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + MAX_CHARS, len(text))
        if end < len(text):
            # Prefer to break at a paragraph or sentence boundary.
            window = text[start:end]
            break_at = max(window.rfind("\n\n"), window.rfind(". "))
            if break_at > MAX_CHARS // 2:
                end = start + break_at + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP_CHARS, start + 1)
    return [c for c in chunks if c]


def _chunk_code(text: str) -> list[str]:
    lines = text.splitlines()
    if len(lines) <= CODE_LINES:
        return ["\n".join(lines)]
    chunks = []
    start = 0
    while start < len(lines):
        end = min(start + CODE_LINES, len(lines))
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(lines):
            break
        start = end - CODE_OVERLAP_LINES
    return chunks
