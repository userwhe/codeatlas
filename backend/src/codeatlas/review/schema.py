"""Structured review output (specs/003-pr-review research R7). Bounds are enforced here,
server-side; validation of the cited labels comes after parsing."""

from typing import Literal

from pydantic import BaseModel, Field

MAX_OVERVIEW_CHARS = 600
MAX_SUMMARY_POINTS = 15
MAX_SUMMARY_POINT_CHARS = 300
MAX_RISKS = 12
MAX_RISK_TITLE_CHARS = 120
MAX_RISK_EXPLANATION_CHARS = 600
MAX_SUGGESTED_CHECK_CHARS = 300
MAX_CHECKLIST_ITEMS = 12
MAX_CHECKLIST_TEXT_CHARS = 200
MAX_NEW_TEST_CASES = 8
MAX_TEST_BEHAVIOR_CHARS = 300
MAX_LOCATION_HINT_CHARS = 200

ChangeKind = Literal["added", "modified", "renamed", "removed"]
Severity = Literal["high", "medium", "low"]
RiskCategory = Literal[
    "correctness",
    "security",
    "data_and_migrations",
    "compatibility",
    "performance",
    "dependencies",
    "tests",
    "other",
]
RiskBasis = Literal["observed", "possible"]


class SummaryPoint(BaseModel):
    change: ChangeKind
    text: str = Field(min_length=1, max_length=MAX_SUMMARY_POINT_CHARS)
    evidence_ids: list[str] = Field(default_factory=list)


class RiskOut(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_RISK_TITLE_CHARS)
    severity: Severity
    category: RiskCategory
    basis: RiskBasis
    explanation: str = Field(min_length=1, max_length=MAX_RISK_EXPLANATION_CHARS)
    suggested_check: str = Field(min_length=1, max_length=MAX_SUGGESTED_CHECK_CHARS)
    evidence_ids: list[str] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    """What the reader should verify. `risk_indexes` are 0-based positions in the same output's
    `risks`, before the server drops or sorts any risk."""

    text: str = Field(min_length=1, max_length=MAX_CHECKLIST_TEXT_CHARS)
    paths: list[str] = Field(default_factory=list)
    risk_indexes: list[int] = Field(default_factory=list)


class NewTestCase(BaseModel):
    behavior: str = Field(min_length=1, max_length=MAX_TEST_BEHAVIOR_CHARS)
    location_hint: str = Field(default="", max_length=MAX_LOCATION_HINT_CHARS)
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    overview: str = Field(min_length=1, max_length=MAX_OVERVIEW_CHARS)
    summary_points: list[SummaryPoint] = Field(default_factory=list, max_length=MAX_SUMMARY_POINTS)
    risks: list[RiskOut] = Field(default_factory=list, max_length=MAX_RISKS)
    checklist: list[ChecklistItem] = Field(default_factory=list, max_length=MAX_CHECKLIST_ITEMS)
    new_test_cases: list[NewTestCase] = Field(default_factory=list, max_length=MAX_NEW_TEST_CASES)
