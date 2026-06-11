"""Document parsing utilities for PDF and DOCX files.

Accepts raw ``bytes`` (Lambda downloads from S3 into memory) and produces
chunk dicts compatible with the tagging agent input schema.
"""

from __future__ import annotations

import io
import logging
import unicodedata
from collections import Counter
from typing import Any

import fitz
from docx import Document

from app.agent_service.src.config import ParserConfig
from app.agent_service.src.utils.exceptions import ScannedPdfError

logger: logging.Logger = logging.getLogger(__name__)

# Invisible characters used to smuggle hidden prompt-injection instructions.
# NB: ZWJ (U+200D) and ZWNJ (U+200C) are intentionally NOT stripped — they are
# linguistically significant in scripts such as Devanagari, Persian and Arabic,
# and removing them would corrupt legitimate non-English content.
_ZERO_WIDTH: frozenset[str] = frozenset({"​", "﻿"})  # ZWSP, BOM/ZWNBSP

# Bidirectional override/isolate controls — used to make visible text differ
# from the logical text the model actually reads.
_BIDI_CONTROLS: frozenset[str] = frozenset(
    {
        "‪", "‫", "‬", "‭", "‮",  # LRE RLE PDF LRO RLO
        "⁦", "⁧", "⁨", "⁩",            # LRI RLI FSI PDI
    }
)

# Unicode Tags block (U+E0000–U+E007F): invisible code points that can carry an
# entire hidden instruction the model still reads.
_TAGS_BLOCK_START: int = 0xE0000
_TAGS_BLOCK_END: int = 0xE007F


def sanitize_text(text: str) -> str:
    """Neutralise hidden/obfuscated content used for prompt injection.

    Language-agnostic defence against non-English and invisible injection that
    does not rely on keyword blocklists:

    - NFKC-normalises compatibility/confusable forms (homoglyph folding).
    - Strips zero-width space and BOM (but keeps linguistic ZWJ/ZWNJ).
    - Removes bidirectional override/isolate controls.
    - Removes the invisible Unicode Tags block.
    - Drops other control characters except tab/newline/carriage-return.
    """
    if not text:
        return text

    normalized: str = unicodedata.normalize("NFKC", text)
    cleaned: list[str] = []
    for ch in normalized:
        if ch in _ZERO_WIDTH or ch in _BIDI_CONTROLS:
            continue
        if _TAGS_BLOCK_START <= ord(ch) <= _TAGS_BLOCK_END:
            continue
        if unicodedata.category(ch) == "Cc" and ch not in ("\t", "\n", "\r"):
            continue
        cleaned.append(ch)
    return "".join(cleaned)

_parser_config: ParserConfig | None = None


def _get_parser_config() -> ParserConfig:
    """Return the module-level ``ParserConfig`` singleton, creating on first call."""
    global _parser_config  # noqa: PLW0603
    if _parser_config is None:
        _parser_config = ParserConfig()
    return _parser_config


def get_pdf_strategy(file_bytes: bytes, min_text_chars: int | None = None) -> str:
    """Return ``"text"`` if the PDF has an extractable text layer, else ``"vision"``."""
    threshold: int = (
        min_text_chars if min_text_chars is not None else _get_parser_config().min_text_chars
    )
    doc: fitz.Document = fitz.open(stream=file_bytes, filetype="pdf")
    sample: str = "".join(doc[i].get_text() for i in range(min(3, len(doc))))
    doc.close()
    return "text" if len(sample.strip()) > threshold else "vision"


def extract_text_blocks(file_bytes: bytes) -> list[dict[str, Any]]:
    """Extract raw text blocks from a PDF with spatial metadata."""
    doc: fitz.Document = fitz.open(stream=file_bytes, filetype="pdf")
    blocks: list[dict[str, Any]] = []

    for page_num, page in enumerate(doc, start=1):
        raw_blocks: list[dict[str, Any]] = page.get_text("dict")["blocks"]

        for block in raw_blocks:
            if block["type"] != 0:
                continue

            spans: list[dict[str, Any]] = [
                span for line in block["lines"] for span in line["spans"]
            ]

            if not spans:
                continue

            text: str = sanitize_text(
                " ".join(s["text"].strip() for s in spans if s["text"].strip())
            )
            if not text:
                continue

            font_sizes: list[float] = list({round(s["size"], 1) for s in spans})
            font_names: list[str] = list({s["font"] for s in spans})

            blocks.append(
                {
                    "page": page_num,
                    "block_no": block["number"],
                    "bbox": [round(v, 1) for v in block["bbox"]],
                    "font_sizes": font_sizes,
                    "font_names": font_names,
                    "text": text,
                }
            )

    doc.close()
    return blocks


def clean_and_chunk(
    blocks: list[dict[str, Any]],
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Merge small blocks into chunks and attach heading hints."""
    if not blocks:
        return []

    effective_max_chars: int = (
        max_chars if max_chars is not None else _get_parser_config().chunk_max_chars
    )

    # Build per-page body-font lookup
    page_font_counter: dict[int, Counter[float]] = {}
    for b in blocks:
        page: int = b["page"]
        if page not in page_font_counter:
            page_font_counter[page] = Counter()
        for fs in b["font_sizes"]:
            page_font_counter[page][fs] += 1

    body_font: dict[int, float] = {
        pg: counter.most_common(1)[0][0] for pg, counter in page_font_counter.items()
    }

    chunks: list[dict[str, Any]] = []
    idx: int = 0
    current_text: str = ""
    current_page: int = blocks[0]["page"] if blocks else 1
    current_is_heading: bool = False

    def flush(text: str, pg: int, is_heading: bool) -> dict[str, Any]:
        return {
            "chunk_index": idx,
            "page": pg,
            "is_heading": is_heading,
            "char_count": len(text),
            "text": text.strip(),
        }

    for block in blocks:
        pg = block["page"]
        text: str = block["text"]
        max_font: float = max(block["font_sizes"]) if block["font_sizes"] else 0
        is_heading: bool = max_font > body_font.get(pg, 0) * 1.1

        force_flush: bool = is_heading or (len(current_text) + len(text) > effective_max_chars)

        if force_flush and current_text.strip():
            chunks.append(flush(current_text, current_page, current_is_heading))
            idx += 1
            current_text = ""

        current_text += (" " if current_text else "") + text
        current_page = pg
        current_is_heading = is_heading

    if current_text.strip():
        chunks.append(flush(current_text, current_page, current_is_heading))

    return chunks


def parse_docx(file_bytes: bytes) -> list[dict[str, Any]]:
    """Parse a DOCX file to the same chunk schema as PDF parsing."""
    doc = Document(io.BytesIO(file_bytes))
    chunks: list[dict[str, Any]] = []
    idx: int = 0

    for para_idx, para in enumerate(doc.paragraphs):
        text: str = sanitize_text(para.text.strip())
        if not text:
            continue

        style_name: str = para.style.name if para.style else ""
        is_heading: bool = style_name.startswith("Heading")

        chunks.append(
            {
                "chunk_index": idx,
                "page": para_idx + 1,
                "is_heading": is_heading,
                "char_count": len(text),
                "text": text,
            }
        )
        idx += 1

    return chunks


def _parse_bytes(file_bytes: bytes, s3_key: str, doc_id: str) -> list[dict[str, Any]]:
    """Parse a PDF or DOCX byte stream into chunks.

    Raises:
        ScannedPdfError: If the PDF has no extractable text layer.
        ValueError: If the file extension is unsupported.
    """
    extension: str = s3_key.rsplit(".", maxsplit=1)[-1].lower() if "." in s3_key else ""

    if extension == "pdf":
        strategy: str = get_pdf_strategy(file_bytes)
        if strategy == "vision":
            raise ScannedPdfError(
                f"PDF has no extractable text layer: doc_id={doc_id} s3_key={s3_key}"
            )
        blocks: list[dict[str, Any]] = extract_text_blocks(file_bytes)
        return clean_and_chunk(blocks)

    if extension == "docx":
        return parse_docx(file_bytes)

    raise ValueError(f"Unsupported file extension: '{extension}' for s3_key={s3_key}")
