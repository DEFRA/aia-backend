"""Tests for the technical assessment prompt template (loaded from .md files)."""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "app"
    / "agent_service"
    / "src"
    / "agents"
    / "prompts"
)


def _load(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def test_technical_prompt_renders_json_questions() -> None:
    """Formatting the user template should produce the expected JSON questions
    block inside <questions> delimiters — no category_url line.
    """
    import json

    template = _load("technical_user.md")
    questions_block: str = json.dumps(
        [
            {
                "id": "aaaa-0001",
                "question": "Is a Record of Processing Activity maintained?",
            },
            {"id": "bbbb-0002", "question": "Are retention schedules documented?"},
        ],
        indent=2,
    )
    document: str = "Sample policy text."
    nonce: str = "test-nonce-123"

    rendered: str = template.format(
        nonce=nonce, document=document, questions=questions_block
    )

    # Document is wrapped in the nonce-delimited untrusted-document boundary.
    assert f"<<<BEGIN_UNTRUSTED_DOCUMENT {nonce}>>>" in rendered
    assert f"<<<END_UNTRUSTED_DOCUMENT {nonce}>>>" in rendered
    assert "Sample policy text." in rendered
    assert "<category_url>" not in rendered
    assert "aaaa-0001" in rendered
    assert '"Technical"' in rendered


def test_technical_system_prompt_specifies_technical_top_level_key() -> None:
    """The system prompt must instruct the model to use ``Technical`` as the
    top-level JSON key, otherwise the agent's parser will fail to extract.
    """
    system_prompt = _load("technical_system.md")
    assert '"Technical"' in system_prompt


def test_technical_system_prompt_covers_required_uk_ig_topics() -> None:
    """The system prompt should explicitly name the key UK information-governance
    domains the agent is expected to evaluate (DPA 2018 / UK GDPR / records mgmt).
    """
    system_prompt = _load("technical_system.md")
    required_terms: list[str] = [
        "UK GDPR",
        "DPA 2018",
        "Article 6",
        "Article 30",
        "DPIA",
        "DPO",
        "IAO",
        "SIRO",
        "OFFICIAL",
    ]
    for term in required_terms:
        assert term in system_prompt, f"Expected '{term}' in technical system prompt"
