"""Tests for src.utils.pdf_creator_multipage — ReportLab PDF report builder."""

from __future__ import annotations

import pytest
from reportlab.lib import colors

from app.agent_service.src.utils.pdf_creator_multipage import (
    _esc,
    _format_reference,
    _rating_label,
    build_security_report,
    rating_colors,
)


# ---------------------------------------------------------------------------
# rating_colors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected_bg",
    [
        ("Green", "#D1FAE5"),
        ("Amber", "#FEF3C7"),
        ("Red", "#FEE2E2"),
        ("N/A", "#E5E7EB"),
    ],
)
def test_rating_colors_known_values(value: str, expected_bg: str) -> None:
    bg, fg = rating_colors(value)
    assert bg == colors.HexColor(expected_bg)
    assert isinstance(fg, colors.Color)


def test_rating_colors_is_case_insensitive() -> None:
    assert rating_colors("green") == rating_colors("GREEN") == rating_colors("Green")


def test_rating_colors_unknown_defaults_to_white_on_black() -> None:
    assert rating_colors("something-else") == (colors.white, colors.black)
    assert rating_colors("") == (colors.white, colors.black)


# ---------------------------------------------------------------------------
# _rating_label
# ---------------------------------------------------------------------------


def test_rating_label_maps_na_to_not_applicable() -> None:
    assert _rating_label("N/A") == "Not Applicable"
    assert _rating_label("n/a") == "Not Applicable"  # case-insensitive


def test_rating_label_passes_through_other_values() -> None:
    assert _rating_label("Green") == "Green"
    assert _rating_label("") == ""
    assert _rating_label(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _esc — XML-escaping of untrusted LLM output
# ---------------------------------------------------------------------------


def test_esc_escapes_markup_metacharacters() -> None:
    assert _esc('<link href="x">click</link>') == (
        "&lt;link href=\"x\"&gt;click&lt;/link&gt;"
    )
    assert _esc("a & b") == "a &amp; b"


def test_esc_handles_none_and_non_strings() -> None:
    assert _esc(None) == ""
    assert _esc(123) == "123"


# ---------------------------------------------------------------------------
# _format_reference
# ---------------------------------------------------------------------------


def test_format_reference_with_url_builds_escaped_link() -> None:
    out = _format_reference({"text": "P.6 <ref>", "url": 'http://x?a=1"b'})
    assert out.startswith("<link href=")
    assert "</link>" in out
    # text is escaped
    assert "P.6 &lt;ref&gt;" in out
    # the quote in the URL is escaped so it can't break out of the href attribute
    assert '"b"' not in out.split("href=")[1].split(">")[0].rstrip('"')
    assert "&quot;" in out


def test_format_reference_without_url_returns_escaped_text() -> None:
    assert _format_reference({"text": "A & B"}) == "A &amp; B"


def test_format_reference_non_dict_returns_empty() -> None:
    assert _format_reference("just a string") == ""
    assert _format_reference(None) == ""


# ---------------------------------------------------------------------------
# build_security_report — end-to-end PDF generation
# ---------------------------------------------------------------------------


def _dataset(top_key: str = "Security") -> dict[str, object]:
    return {
        top_key: {
            "Final_Summary": {
                "Interpretation": "Minor gaps - needs remediation",
                "Overall_Comments": "Two items <need> review & sign-off.",
            },
            "Assessments": [
                {
                    "Question": "Is MFA enforced?",
                    "Rating": "Green",
                    "Comments": "Section 3.1 confirms MFA.",
                    "Reference": {"text": "P.3", "url": "http://example/p3"},
                },
                {
                    "Question": "Is retention defined?",
                    "Rating": "Red",
                    "Comments": "Inject: <link href='http://evil'>x</link>",
                    "Reference": {"text": "P.4"},  # no url
                },
                {
                    "Question": "Cloud provider question",
                    "Rating": "N/A",
                    "Comments": "Not Applicable - on-prem only.",
                    "Reference": "not-a-dict",  # exercises non-dict branch
                },
            ],
        }
    }


def test_build_security_report_writes_valid_pdf(tmp_path) -> None:
    out_path = str(tmp_path / "report.pdf")
    result = build_security_report([_dataset()], output_path=out_path)

    assert result == out_path
    data = (tmp_path / "report.pdf").read_bytes()
    assert data.startswith(b"%PDF"), "output should be a PDF"
    assert len(data) > 1000


def test_build_security_report_multiple_sections_paginates(tmp_path) -> None:
    out_path = str(tmp_path / "multi.pdf")
    # Two datasets -> exercises the PageBreak branch between sections.
    result = build_security_report(
        [_dataset("Security"), _dataset("Technical")], output_path=out_path
    )
    assert result == out_path
    assert (tmp_path / "multi.pdf").read_bytes().startswith(b"%PDF")


def test_build_security_report_rejects_malformed_dataset(tmp_path) -> None:
    out_path = str(tmp_path / "bad.pdf")
    # Not a single-top-level-key dict -> ValueError.
    with pytest.raises(ValueError, match="exactly one top-level key"):
        build_security_report([{"a": {}, "b": {}}], output_path=out_path)
    with pytest.raises(ValueError, match="exactly one top-level key"):
        build_security_report(["not-a-dict"], output_path=out_path)  # type: ignore[list-item]
