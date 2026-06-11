"""Security assessment agent for document-based RAG evaluation."""

import json
import logging
from pathlib import Path
from typing import cast
from uuid import uuid4

import anthropic
from anthropic import APIError
from anthropic.types import Message, TextBlock

from app.agent_service.src.models.schemas import (
    AgentLLMOutput,
    LLMResponseMeta,
    QuestionItem,
    RawAssessmentRow,
    Summary,
)
from app.agent_service.src.config import SecurityAgentConfig
from app.agent_service.src.utils.helpers import parse_llm_json

logger: logging.Logger = logging.getLogger(__name__)

_PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
_SYSTEM_PROMPT: str = (_PROMPTS_DIR / "security_system.md").read_text(encoding="utf-8")
_USER_TEMPLATE: str = (_PROMPTS_DIR / "security_user.md").read_text(encoding="utf-8")


def _extract_response_meta(response: Message, model: str) -> LLMResponseMeta:
    return LLMResponseMeta(
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
    )


def _format_questions_block(questions: list[QuestionItem]) -> str:
    return json.dumps(
        [{"id": q.id, "question": q.question} for q in questions],
        indent=2,
    )


class SecurityAgent:
    """Async LLM agent that assesses a document against a security checklist."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        agent_config: SecurityAgentConfig,
    ) -> None:
        self.client: anthropic.AsyncAnthropic = client
        self.agent_config: SecurityAgentConfig = agent_config

    async def assess(
        self,
        document: str,
        questions: list[QuestionItem],
    ) -> AgentLLMOutput:
        nonce: str = uuid4().hex
        user_content: str = _USER_TEMPLATE.format(
            nonce=nonce,
            document=document,
            questions=_format_questions_block(questions),
        )

        try:
            response: Message = await self.client.messages.create(
                model=self.agent_config.model,
                max_tokens=self.agent_config.max_tokens,
                temperature=self.agent_config.temperature,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
        except APIError as exc:
            logger.error("LLM API error during assessment: %s", exc)
            raise

        meta: LLMResponseMeta = _extract_response_meta(response, self.agent_config.model)
        raw_text: str = cast(TextBlock, response.content[0]).text

        try:
            payload: dict[str, object] = parse_llm_json(raw_text)
            security_block: dict[str, object] = payload["Security"]  # type: ignore[assignment]
            raw_rows: list[RawAssessmentRow] = [
                RawAssessmentRow.model_validate(row)
                for row in cast(list[object], security_block["Assessments"])
            ]
            summary_obj: Summary = Summary.model_validate(security_block["Summary"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error(
                "Failed to parse LLM response. raw_text=%.200s error=%s",
                raw_text,
                exc,
            )
            raise ValueError(f"Could not parse assessment response: {exc}") from exc

        logger.info(
            "Assessment complete: %d questions, %d input / %d output tokens",
            len(raw_rows),
            meta.input_tokens,
            meta.output_tokens,
        )

        return AgentLLMOutput(rows=raw_rows, summary=summary_obj, llm_meta=meta)
