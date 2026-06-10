"""Pydantic schemas for security assessment agent I/O and EventBridge detail payloads."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class QuestionItem(BaseModel):
    id: str
    question: str
    reference: str


class AssessmentRow(BaseModel):
    Question: str
    Rating: Literal["Green", "Amber", "Red", "Grey"]
    Comments: str
    Reference: str


class Summary(BaseModel):
    Interpretation: str
    Overall_Comments: str


class LLMResponseMeta(BaseModel):
    """Metadata extracted from the raw LLM API response."""

    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None


class RawAssessmentRow(BaseModel):
    question_id: str
    Rating: Literal["Green", "Amber", "Red", "Grey"]
    Comments: str


class AgentLLMOutput(BaseModel):
    rows: list[RawAssessmentRow]
    summary: Summary
    llm_meta: LLMResponseMeta | None = None


class PolicyDocResult(BaseModel):
    """Complete result returned by a security or compliance agent for one policy document."""

    policy_doc_filename: str
    policy_doc_url: str
    assessments: list[AssessmentRow]
    summary: Summary


class AgentResult(BaseModel):
    """Consolidated result for one agent_type across all its policy documents."""

    agent_type: str
    docs: list[PolicyDocResult]


class TaggedChunk(BaseModel):
    """A document chunk enriched with security/technical tags."""

    chunk_index: int
    page: int
    is_heading: bool
    text: str
    relevant: bool
    tags: list[str]
    reason: str | None


# ---------------------------------------------------------------------------
# PayloadEnvelope — discriminated union for inline-or-S3 cross-stage handoff
# ---------------------------------------------------------------------------


class InlinePayload(BaseModel):
    """A payload carried inline as a JSON-serialised string."""

    inline: str

    model_config = {"extra": "forbid"}


class S3KeyPayload(BaseModel):
    """A payload offloaded to S3 — the receiver dereferences this key."""

    s3Key: str

    model_config = {"extra": "forbid"}


# Discriminated union: exactly one of ``inline`` or ``s3Key`` must be present.
PayloadEnvelope = Annotated[
    InlinePayload | S3KeyPayload,
    Field(union_mode="left_to_right"),
]


# ---------------------------------------------------------------------------
# EventBridge detail payload models — one per pipeline stage transition
# ---------------------------------------------------------------------------


class DocumentParsedDetail(BaseModel):
    """Detail payload for the ``DocumentParsed`` event (Stage 3 -> 4)."""

    document_id: str
    payload: PayloadEnvelope


class DocumentTaggedDetail(BaseModel):
    """Detail payload for the ``DocumentTagged`` event (Stage 4 -> 5)."""

    document_id: str
    payload: PayloadEnvelope


class SectionsReadyDetail(BaseModel):
    """Detail payload for the ``SectionsReady`` event (Stage 5 -> 6)."""

    document_id: str
    agentType: Literal["security", "technical"]


class AgentCompleteDetail(BaseModel):
    """Detail payload for the ``AgentComplete`` event (Stage 6 terminus marker)."""

    document_id: str
    agentType: str


# ---------------------------------------------------------------------------
# Stage 6 SQS Status queue terminal output
# ---------------------------------------------------------------------------


class AgentStatusMessage(BaseModel):
    """Status message published by Stage 6 agents to the SQS Status queue."""

    document_id: str
    agentType: str
    status: Literal["completed", "failed"]
    result: PolicyDocResult | None
    durationMs: float
    completedAt: str
    errorMessage: str | None = None
