"""Tests for src.utils.document_parser — PDF and DOCX parsing functions."""

from __future__ import annotations

import io

import fitz
import pytest
from docx import Document

from app.agent_service.src.utils.doc_parser import (
    _parse_bytes,
    clean_and_chunk,
    extract_text_blocks,
    get_pdf_strategy,
    parse_docx,
    sanitize_text,
)
from app.agent_service.src.utils.exceptions import ScannedPdfError

# ---------------------------------------------------------------------------
# Helpers — build minimal in-memory PDF and DOCX fixtures
# ---------------------------------------------------------------------------


def _make_text_pdf(pages: list[str]) -> bytes:
    """Create a minimal PDF with extractable text on each page."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    buf: bytes = doc.tobytes()
    doc.close()
    return buf


def _make_blank_pdf(num_pages: int = 3) -> bytes:
    """Create a PDF with no text layer (simulates a scanned document)."""
    doc = fitz.open()
    for _ in range(num_pages):
        doc.new_page()
    buf: bytes = doc.tobytes()
    doc.close()
    return buf


def _make_docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """Create a minimal DOCX from (style, text) pairs."""
    doc = Document()
    for style, text in paragraphs:
        doc.add_paragraph(text, style=style)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# get_pdf_strategy
# ---------------------------------------------------------------------------


class TestGetPdfStrategy:
    """Tests for get_pdf_strategy()."""

    def test_text_pdf_returns_text(self) -> None:
        pdf_bytes: bytes = _make_text_pdf(["Hello world. " * 20])
        assert get_pdf_strategy(pdf_bytes) == "text"

    def test_blank_pdf_returns_vision(self) -> None:
        pdf_bytes: bytes = _make_blank_pdf()
        assert get_pdf_strategy(pdf_bytes) == "vision"

    def test_samples_at_most_three_pages(self) -> None:
        pages: list[str] = [""] * 3 + ["Lots of text here. " * 50]
        pdf_bytes: bytes = _make_text_pdf(pages)
        # Text only on page 4 — first 3 pages are empty, so strategy is "vision"
        assert get_pdf_strategy(pdf_bytes) == "vision"


# ---------------------------------------------------------------------------
# extract_text_blocks
# ---------------------------------------------------------------------------


class TestExtractTextBlocks:
    """Tests for extract_text_blocks()."""

    def test_returns_list_of_dicts_with_expected_keys(self) -> None:
        pdf_bytes: bytes = _make_text_pdf(["Security controls overview."])
        blocks: list[dict] = extract_text_blocks(pdf_bytes)  # type: ignore[type-arg]
        assert len(blocks) >= 1
        first = blocks[0]
        assert "page" in first
        assert "block_no" in first
        assert "bbox" in first
        assert "font_sizes" in first
        assert "font_names" in first
        assert "text" in first

    def test_page_numbers_start_at_one(self) -> None:
        pdf_bytes: bytes = _make_text_pdf(["Page one.", "Page two."])
        blocks: list[dict] = extract_text_blocks(pdf_bytes)  # type: ignore[type-arg]
        pages: set[int] = {b["page"] for b in blocks}
        assert 1 in pages
        assert 2 in pages

    def test_empty_pdf_returns_empty_list(self) -> None:
        pdf_bytes: bytes = _make_blank_pdf(1)
        blocks: list[dict] = extract_text_blocks(pdf_bytes)  # type: ignore[type-arg]
        assert blocks == []


# ---------------------------------------------------------------------------
# clean_and_chunk
# ---------------------------------------------------------------------------


class TestCleanAndChunk:
    """Tests for clean_and_chunk()."""

    def test_returns_chunk_schema(self) -> None:
        blocks: list[dict] = [  # type: ignore[type-arg]
            {
                "page": 1,
                "block_no": 0,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [12.0],
                "font_names": ["Helvetica"],
                "text": "Hello world",
            }
        ]
        chunks: list[dict] = clean_and_chunk(blocks)  # type: ignore[type-arg]
        assert len(chunks) == 1
        c = chunks[0]
        assert "chunk_index" in c
        assert "page" in c
        assert "is_heading" in c
        assert "char_count" in c
        assert "text" in c

    def test_merges_small_blocks(self) -> None:
        blocks: list[dict] = [  # type: ignore[type-arg]
            {
                "page": 1,
                "block_no": i,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [12.0],
                "font_names": ["Helvetica"],
                "text": f"Short block {i}",
            }
            for i in range(3)
        ]
        chunks: list[dict] = clean_and_chunk(blocks)  # type: ignore[type-arg]
        # 3 small blocks should merge into 1 chunk
        assert len(chunks) == 1

    def test_splits_at_max_chars(self) -> None:
        long_text: str = "A" * 800
        blocks: list[dict] = [  # type: ignore[type-arg]
            {
                "page": 1,
                "block_no": i,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [12.0],
                "font_names": ["Helvetica"],
                "text": long_text,
            }
            for i in range(3)
        ]
        chunks: list[dict] = clean_and_chunk(blocks, max_chars=1500)  # type: ignore[type-arg]
        assert len(chunks) >= 2

    def test_heading_forces_chunk_split(self) -> None:
        """A heading block flushes the preceding body text into its own chunk."""
        blocks: list[dict] = [  # type: ignore[type-arg]
            {
                "page": 1,
                "block_no": 0,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [12.0],
                "font_names": ["Helvetica"],
                "text": "Preceding body text.",
            },
            {
                "page": 1,
                "block_no": 1,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [20.0],
                "font_names": ["Helvetica-Bold"],
                "text": "Section Title",
            },
            {
                "page": 1,
                "block_no": 2,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [12.0],
                "font_names": ["Helvetica"],
                "text": "Body text after heading.",
            },
        ]
        chunks: list[dict] = clean_and_chunk(blocks)  # type: ignore[type-arg]
        # Heading forces a split — preceding body text is in chunk 0
        assert len(chunks) == 2
        assert "Preceding body text" in chunks[0]["text"]
        assert "Section Title" in chunks[1]["text"]

    def test_heading_only_block_is_heading(self) -> None:
        """A heading that is the last block in its chunk retains is_heading=True."""
        blocks: list[dict] = [  # type: ignore[type-arg]
            {
                "page": 1,
                "block_no": 0,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [12.0],
                "font_names": ["Helvetica"],
                "text": "Body text.",
            },
            {
                "page": 1,
                "block_no": 1,
                "bbox": [0, 0, 100, 100],
                "font_sizes": [20.0],
                "font_names": ["Helvetica-Bold"],
                "text": "Standalone Heading",
            },
        ]
        chunks: list[dict] = clean_and_chunk(blocks)  # type: ignore[type-arg]
        assert len(chunks) == 2
        assert chunks[1]["is_heading"] is True

    def test_empty_blocks_returns_empty(self) -> None:
        assert clean_and_chunk([]) == []


# ---------------------------------------------------------------------------
# parse_docx
# ---------------------------------------------------------------------------


class TestParseDocx:
    """Tests for parse_docx()."""

    def test_returns_chunk_schema(self) -> None:
        docx_bytes: bytes = _make_docx([("Normal", "Hello world.")])
        chunks: list[dict] = parse_docx(docx_bytes)  # type: ignore[type-arg]
        assert len(chunks) == 1
        c = chunks[0]
        assert "chunk_index" in c
        assert "page" in c
        assert "is_heading" in c
        assert "char_count" in c
        assert "text" in c

    def test_heading_style_detected(self) -> None:
        docx_bytes: bytes = _make_docx(
            [
                ("Heading 1", "My Title"),
                ("Normal", "Body paragraph."),
            ]
        )
        chunks: list[dict] = parse_docx(docx_bytes)  # type: ignore[type-arg]
        assert chunks[0]["is_heading"] is True
        assert chunks[1]["is_heading"] is False

    def test_empty_paragraphs_skipped(self) -> None:
        docx_bytes: bytes = _make_docx(
            [
                ("Normal", "Text"),
                ("Normal", ""),
                ("Normal", "More text"),
            ]
        )
        chunks: list[dict] = parse_docx(docx_bytes)  # type: ignore[type-arg]
        texts: list[str] = [c["text"] for c in chunks]
        assert "" not in texts

    def test_chunk_index_sequential(self) -> None:
        docx_bytes: bytes = _make_docx(
            [
                ("Normal", "First"),
                ("Normal", "Second"),
                ("Normal", "Third"),
            ]
        )
        chunks: list[dict] = parse_docx(docx_bytes)  # type: ignore[type-arg]
        indices: list[int] = [c["chunk_index"] for c in chunks]
        assert indices == list(range(len(indices)))


# ---------------------------------------------------------------------------
# sanitize_text
# ---------------------------------------------------------------------------


class TestSanitizeText:
    """Tests for sanitize_text()."""

    def test_empty_text_is_returned_as_is(self) -> None:
        assert sanitize_text("") == ""

    def test_removes_invisible_controls_and_normalizes_nfkc(self) -> None:
        dirty = (
            "A\u200b"  # zero-width space
            "B\ufeff"  # BOM / ZWNBSP
            "C\u202e"  # bidi control
            "\U000E0061"  # tag block character
            "\x01"  # control character
            "\t\n\r"  # preserved control whitespace
            "\u200d\u200c"  # preserve ZWJ and ZWNJ
            "Ａ"  # full-width A should normalize to A
        )
        assert sanitize_text(dirty) == "ABC\t\n\r\u200d\u200cA"


# ---------------------------------------------------------------------------
# _parse_bytes
# ---------------------------------------------------------------------------


class TestParseBytes:
    """Tests for _parse_bytes()."""

    def test_pdf_with_text_returns_chunks(self) -> None:
        pdf_bytes: bytes = _make_text_pdf(["Policy content section. " * 12])
        chunks: list[dict] = _parse_bytes(pdf_bytes, "security-policy.pdf", "doc-1")  # type: ignore[type-arg]

        assert len(chunks) >= 1
        first = chunks[0]
        assert first["chunk_index"] == 0
        assert first["page"] == 1
        assert isinstance(first["is_heading"], bool)
        assert first["char_count"] > 0
        assert "Policy content section." in first["text"]

    def test_scanned_pdf_raises_scanned_pdf_error(self) -> None:
        pdf_bytes: bytes = _make_blank_pdf()
        with pytest.raises(ScannedPdfError):
            _parse_bytes(pdf_bytes, "scan.pdf", "doc-2")

    def test_docx_extension_parses_docx(self) -> None:
        docx_bytes: bytes = _make_docx([("Normal", "DOCX body")])
        chunks: list[dict] = _parse_bytes(docx_bytes, "input.docx", "doc-3")  # type: ignore[type-arg]
        assert len(chunks) == 1
        assert chunks[0]["text"] == "DOCX body"

    def test_missing_extension_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unsupported file extension"):
            _parse_bytes(b"irrelevant", "no_extension", "doc-4")
