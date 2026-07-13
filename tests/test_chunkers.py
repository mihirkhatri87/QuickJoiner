from quickjoiner.ingest.chunkers import MAX_CHARS, chunk_document


def test_empty_text_yields_no_chunks():
    assert chunk_document("") == []
    assert chunk_document("   \n  ") == []


def test_short_text_single_chunk():
    assert chunk_document("hello world") == ["hello world"]


def test_long_prose_is_split_with_all_content_covered():
    text = " ".join(f"sentence{i}." for i in range(2000))
    chunks = chunk_document(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHARS + 10 for c in chunks)
    assert "sentence0." in chunks[0]
    assert "sentence1999." in chunks[-1]


def test_markdown_split_at_headings():
    text = "# Alpha\ncontent a\n\n# Beta\ncontent b"
    chunks = chunk_document(text)
    assert any("Alpha" in c for c in chunks)
    assert any("Beta" in c for c in chunks)


def test_markdown_large_section_carries_heading_on_every_subchunk():
    body = " ".join(f"word{i}" for i in range(2000))  # forces the section to split
    chunks = chunk_document(f"## Deployments\n{body}")
    assert len(chunks) > 1
    assert all("## Deployments" in c for c in chunks)  # heading context on each piece


def test_code_split_by_lines():
    code = "\n".join(f"line_{i} = {i}" for i in range(200))
    chunks = chunk_document(code, kind="code")
    assert len(chunks) > 1
    assert "line_0 = 0" in chunks[0]
    assert "line_199 = 199" in chunks[-1]
