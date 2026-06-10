from typing import Literal

from pydantic import BaseModel


class AssessmentRow(BaseModel):
    Question: str
    Rating: Literal["Green", "Amber", "Red", "Grey"]
    Comments: str
    Reference: str


class Summary(BaseModel):
    Interpretation: str
    Overall_Comments: str


class PolicyDocResult(BaseModel):
    policy_doc_filename: str
    policy_doc_url: str
    assessments: list[AssessmentRow]
    summary: Summary


class AgentResult(BaseModel):
    agent_type: str
    docs: list[PolicyDocResult]
