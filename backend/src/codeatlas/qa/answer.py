"""The `answer_question` job: retrieve evidence, generate, validate citations, publish (T073)."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from codeatlas.db import session_scope
from codeatlas.jobs.queue import JobFailure, cancel
from codeatlas.jobs.worker import JobContext, register
from codeatlas.logging import log_context
from codeatlas.models import AnalysisRun, EvidenceItem, Repository, Snapshot
from codeatlas.providers.answer_model import AnswerResult, get_answer_model
from codeatlas.providers.errors import ProviderRefused, ProviderUnavailable
from codeatlas.qa.prompt import SYSTEM_PROMPT, build_repair_content, build_user_content
from codeatlas.qa.schema import AnswerOutput
from codeatlas.qa.validate import validate
from codeatlas.retrieval.evidence import Evidence, assemble_evidence

logger = logging.getLogger(__name__)

JOB_KIND = "answer_question"
NO_EVIDENCE_GAP = "No code or documentation in this version matched the question."


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0

    def add(self, result: AnswerResult) -> None:
        self.calls += 1
        self.input_tokens += result.usage.input_tokens
        self.output_tokens += result.usage.output_tokens
        self.thinking_tokens += result.usage.thinking_tokens
        self.cached_tokens += result.usage.cached_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cached_tokens": self.cached_tokens,
        }


def _call(system: str, content: str) -> AnswerResult:
    try:
        return get_answer_model().answer(system=system, user_content=content)
    except ProviderUnavailable as exc:
        raise JobFailure(
            "provider_unavailable",
            "The answer service is unavailable. Try again later.",
            permanent=False,
            retryable=True,
        ) from exc
    except ProviderRefused as exc:
        raise JobFailure(
            "model_refused",
            "The answer service declined this question.",
            permanent=True,
            retryable=False,
        ) from exc


def _generate(
    ctx: JobContext, question: str, evidence: list[Evidence], usage: Usage
) -> AnswerOutput:
    """At most two model calls: the answer and one repair (research R11)."""
    labels = [item.label for item in evidence]
    content = build_user_content(question, evidence)
    ctx.event("generating_answer", "Generating the answer")
    result = _call(SYSTEM_PROMPT, content)
    usage.add(result)

    ctx.event("validating_citations", "Checking every citation against the evidence")
    errors = validate(result.output, labels, result.parse_error)
    if not errors and result.output is not None:
        return result.output

    ctx.check()
    repair = _call(SYSTEM_PROMPT, build_repair_content(content, result.output, errors))
    usage.add(repair)
    errors = validate(repair.output, labels, repair.parse_error)
    if errors or repair.output is None:
        raise JobFailure(
            "citation_validation_failed",
            "The answer could not be verified against the repository evidence.",
            permanent=True,
            retryable=True,
        )
    return repair.output


def _insufficient() -> AnswerOutput:
    return AnswerOutput(
        status="insufficient_evidence",
        summary="There is not enough evidence in this version to answer the question.",
        claims=[],
        gaps=[NO_EVIDENCE_GAP],
    )


def handle_answer(ctx: JobContext) -> None:
    with session_scope() as db:
        run = db.get(AnalysisRun, ctx.analysis_run_id) if ctx.analysis_run_id else None
        repository = db.get(Repository, ctx.repository_id)
        snapshot = db.get(Snapshot, run.snapshot_id) if run is not None else None
        disconnected = repository is None or repository.deleted_at is not None
        question = run.question if run is not None else ""
    if run is None or snapshot is None or disconnected:
        with ctx.publish() as (db, job):
            cancel(db, job, message="The repository was disconnected.")
        return

    with log_context(run_id=str(run.id), snapshot_id=str(snapshot.id)):
        ctx.event("retrieving_evidence", "Searching the repository for evidence")
        with session_scope() as db:
            evidence, docs_degraded = assemble_evidence(db, snapshot, question)
        usage = Usage()
        output = _generate(ctx, question, evidence, usage) if evidence else _insufficient()

        ctx.event("publishing", "Saving the answer")
        with ctx.publish() as (db, job):
            locked = db.get(Repository, ctx.repository_id, with_for_update=True)
            if locked is None or locked.deleted_at is not None:
                cancel(db, job, message="The repository was disconnected.")
                return
            stored = db.get(AnalysisRun, run.id)
            if stored is None:
                cancel(db, job, message="The question no longer exists.")
                return
            db.add_all(
                EvidenceItem(
                    analysis_run_id=stored.id,
                    label=item.label,
                    source_type=item.source_type,
                    path=item.path,
                    commit_sha=item.commit_sha,
                    start_line=item.start_line,
                    end_line=item.end_line,
                    excerpt=item.excerpt,
                    excerpt_sha256=item.excerpt_sha256,
                    rank=item.rank,
                )
                for item in evidence
            )
            stored.result = output.model_dump(mode="json")
            stored.quality_state = output.status
            stored.usage = {**usage.as_dict(), "docs_degraded": docs_degraded}
            stored.completed_at = datetime.now(UTC)


register(JOB_KIND, handle_answer)
