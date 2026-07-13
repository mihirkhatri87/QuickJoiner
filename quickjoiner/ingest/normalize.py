"""Text normalization applied at ingestion (and, lightly, to queries).

Connectors pull text from wikis, editors and PDFs full of typographic quotes,
non-breaking spaces, zero-width characters and inconsistent whitespace. Both
retrieval legs benefit from folding those away before anything is hashed,
chunked, embedded or lexically indexed:

- the sha256 dedupe hash stops treating cosmetic variants as new content,
- embeddings see the same token stream regardless of which editor produced it,
- the FTS index matches "don't" whether it was typed with ' or '.

Normalization is idempotent; content hashes change once for pre-existing docs
(they re-ingest on their next sync, then settle).
"""

from __future__ import annotations

import re
import unicodedata

# Typographic characters NFKC leaves alone, folded to ASCII equivalents.
_CHAR_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",  # single quotes
    "“": '"', "”": '"', "„": '"', "‟": '"',  # double quotes
    "–": "-", "—": " - ", "―": " - ",  # en/em/horizontal-bar dashes
    "…": "...",  # ellipsis
    " ": " ", " ": " ", " ": " ",  # non-breaking / narrow spaces
    "​": "", "‌": "", "‍": "", "﻿": "",  # zero-width + BOM
    "­": "",  # soft hyphen
})

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RUNS = re.compile(r"[ \t]+")
_TRAILING = re.compile(r"[ \t]+$", flags=re.MULTILINE)
_BLANK_RUNS = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Full normalization for document bodies. Preserves line structure
    (chunkers split on headings/blank lines) while folding cosmetic noise."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_CHAR_FOLD)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _SPACE_RUNS.sub(" ", text)
    text = _TRAILING.sub("", text)
    text = _BLANK_RUNS.sub("\n\n", text)
    return text.strip()


def normalize_query(query: str) -> str:
    """Light normalization for search queries: same character folding,
    single-line whitespace collapse. Kept cheap — it runs on every search."""
    if not query:
        return ""
    query = unicodedata.normalize("NFKC", query)
    query = query.translate(_CHAR_FOLD)
    return " ".join(query.split())
