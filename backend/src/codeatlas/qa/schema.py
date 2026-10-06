"""Structured answer output (research R11). Bounds are enforced here, server-side."""

from typing import Literal

from pydantic import BaseModel, Field

MAX_CLAIMS = 10
MAX_CLAIM_CHARS = 600


class Claim(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CLAIM_CHARS)
    kind: Literal["fact", "inference"]
    evidence_ids: list[str] = Field(default_factory=list)


class AnswerOutput(BaseModel):
    status: Literal["answered", "insufficient_evidence"]
    summary: str
    claims: list[Claim] = Field(default_factory=list, max_length=MAX_CLAIMS)
    gaps: list[str] = Field(default_factory=list)
