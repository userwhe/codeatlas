"""The `index_repository` job: resolve, fetch, filter, parse, index, embed, publish (US1, T054)."""

import gzip
import logging
import tarfile
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, func, insert, literal_column, select, update
from sqlalchemy.orm import Session

from codeatlas.config import Settings, get_settings
from codeatlas.db import session_scope
from codeatlas.github.gateway import (
    BranchNotFound,
    GitHubGateway,
    GitHubRepository,
    RepositoryEmpty,
    get_gateway,
    verify_access,
)
from codeatlas.ingestion.chunking import code_chunks, index_version, markdown_chunks
from codeatlas.ingestion.extract import LimitExceeded, iter_archive
from codeatlas.ingestion.filters import EligibleFile, FilterResult, filter_members
from codeatlas.ingestion.parse import Declaration, parse_declarations
from codeatlas.jobs.github_access import (
    ACCESS_LOST_MESSAGE,
    DISCONNECTED_MESSAGE,
    cancel_run,
    disclosure_not_accepted,
    github_answers,
    owner_token,
    publishable_repository,
)
from codeatlas.jobs.queue import (
    JobFailure,
    LeaseLost,
    complete,
    fenced,
    index_dedupe_key,
)
from codeatlas.jobs.worker import JobContext, register
from codeatlas.logging import log_context
from codeatlas.models import (
    CodeChunk,
    CoverageEntry,
    DocChunk,
    File,
    Job,
    JobEvent,
    Repository,
    Snapshot,
    Symbol,
)
from codeatlas.providers.embeddings import get_embedder
from codeatlas.providers.errors import ProviderUnavailable
from codeatlas.workspace.access import pause, restore_access

logger = logging.getLogger(__name__)

JOB_KIND = "index_repository"
INSERT_BATCH = 500
PARSED_LANGUAGES = ("python", "typescript", "tsx")
# Keeps each tsvector well under PostgreSQL's 1 MB limit for pathological long-line files.
MAX_SEARCH_TEXT_CHARS = 100_000
UP_TO_DATE_MESSAGE = "Already up to date"
# Failures caused by access to the repository, not by its content: once access passes again, a
# check indexes the commit instead of treating it as one that cannot be indexed.
ACCESS_FAILURES = ("access_denied", "external_processing_not_accepted")


@dataclass(frozen=True)
class Target:
    """What this attempt indexes, resolved in the `resolving_commit` stage."""

    repository_id: uuid.UUID
    workspace_id: uuid.UUID
    full_name: str
    installation_id: int
    branch: str
    commit_sha: str


@dataclass
class BuildStats:
    symbols: int = 0
    code_chunks: int = 0
    doc_chunks: int = 0
    skipped_entries: int = 0
    embeddings_available: bool = True
    unsupported_syntax: list[str] = field(default_factory=list)


def _record_access(
    ctx: JobContext,
    github_repo: GitHubRepository,
    installation_id: int,
    *,
    checked_from: datetime,
    branch: str,
    commit_sha: str | None,
) -> str | JobFailure | None:
    """Record a passed access check with the refreshed metadata, in one transaction.

    Sets `access_checked_at` and restores a lost repository (research R6, R7). A loss detected
    after this check started is newer than its answer, so it is kept, and the run stops. A
    repository that turned private without an accepted disclosure pauses its automatic updates,
    and the run fails before fetching anything (FR-017, research R8). Otherwise records the
    resolved commit on the job, fenced, so a push that arrives later is covered by this job only
    if the attempt that owns the job resolved its commit (research R3).

    Returns a cancellation message or a failure when the run must stop, else None. The caller
    raises the failure after this transaction commits.
    """
    with session_scope() as db:
        job = fenced(db, ctx.job_id, ctx.fencing_token)
        repository = db.get(Repository, ctx.repository_id, with_for_update=True)
        if repository is None or repository.deleted_at is not None:
            return DISCONNECTED_MESSAGE
        lost_at = repository.access_lost_at
        if lost_at is not None and lost_at >= checked_from:
            return ACCESS_LOST_MESSAGE
        # Renamed, transferred, or reinstalled repositories keep working (001 research R5).
        repository.full_name = github_repo.full_name
        repository.default_branch = github_repo.default_branch
        repository.github_installation_id = installation_id
        repository.is_private = github_repo.private
        restore_access(db, repository, trigger=ctx.trigger)
        if repository.is_private and repository.external_processing_accepted_at is None:
            pause(db, repository, "external_processing_not_accepted")
            return disclosure_not_accepted()
        if commit_sha is not None:
            job.payload = {**job.payload, "branch": branch, "commit_sha": commit_sha}
    return None


def _branch_head(
    ctx: JobContext, gateway: GitHubGateway, installation_id: int, full_name: str, branch: str
) -> str | JobFailure:
    """The branch head, or the failure for a missing branch or an empty repository.

    The branch is read with the installation token, so a denial here means the installation can
    no longer read the repository (research R4).
    """
    with github_answers(ctx, stage="resolving_commit", denied="installation_cannot_read"):
        try:
            return gateway.resolve_commit(installation_id, full_name, branch)
        except BranchNotFound:
            return JobFailure(
                "branch_not_found", f"The branch '{branch}' does not exist.", permanent=True
            )
        except RepositoryEmpty:
            return JobFailure(
                "repository_empty",
                "The repository has no commits, so there is nothing to index.",
                permanent=True,
            )


def _resolve(ctx: JobContext, gateway: GitHubGateway) -> Target | None:
    """Re-check access, refresh repository metadata, and resolve the branch (FR-003, FR-011).

    GitHub is called outside any transaction, and its answers are classified as in research R4.
    Returns None when the run was canceled instead.
    """
    ctx.event("resolving_commit", "Checking access and resolving the branch")
    owner = owner_token(ctx, gateway, stage="resolving_commit")
    if owner is None:
        cancel_run(ctx, DISCONNECTED_MESSAGE)
        return None
    checked_from = datetime.now(UTC)
    with github_answers(ctx, stage="resolving_commit", denied=None, credential=owner.credential):
        github_repo, installation_id = verify_access(
            gateway, owner.token, owner.github_repository_id
        )
    branch = str(ctx.payload.get("branch") or github_repo.default_branch)
    head = _branch_head(ctx, gateway, installation_id, github_repo.full_name, branch)

    # A missing branch or an empty repository is still an answer for the installation, so access
    # is verified either way.
    stop = _record_access(
        ctx,
        github_repo,
        installation_id,
        checked_from=checked_from,
        branch=branch,
        commit_sha=head if isinstance(head, str) else None,
    )
    if isinstance(stop, JobFailure):
        raise stop
    if stop is not None:
        cancel_run(ctx, stop)
        return None
    if isinstance(head, JobFailure):
        raise head
    return Target(
        repository_id=ctx.repository_id,
        workspace_id=ctx.workspace_id,
        full_name=github_repo.full_name,
        installation_id=installation_id,
        branch=branch,
        commit_sha=head,
    )


def _existing_ready(db: Session, target: Target, version: str) -> Snapshot | None:
    return db.scalar(
        select(Snapshot).where(
            Snapshot.repository_id == target.repository_id,
            Snapshot.commit_sha == target.commit_sha,
            Snapshot.index_version == version,
            Snapshot.status == "ready",
        )
    )


def _reuse(ctx: JobContext, snapshot_id: uuid.UUID) -> None:
    ctx.event("publishing", "This commit is already indexed; reusing it")
    with ctx.publish() as (db, job):
        # When the run is canceled, the reused version is left as it is: an earlier run built it.
        repository = publishable_repository(db, ctx, job)
        if repository is not None:
            repository.active_snapshot_id = snapshot_id


def _failed_permanently(db: Session, target: Target) -> bool:
    """Whether the latest finished run on the branch failed at this commit permanently, and not
    retryably, for example because the repository exceeds a limit (research R6).

    Checks that were already up to date say nothing new about the commit, so they are passed
    over; otherwise every other check would index the commit again. Access failures do not
    count: they are about the owner's access, which this run has just verified.
    """
    up_to_date = exists().where(
        JobEvent.job_id == Job.id,
        JobEvent.event_type == "succeeded",
        JobEvent.message == UP_TO_DATE_MESSAGE,
    )
    latest = db.scalar(
        select(Job)
        .where(
            Job.repository_id == target.repository_id,
            Job.kind == JOB_KIND,
            Job.dedupe_key == index_dedupe_key(target.repository_id, target.branch),
            Job.finished_at.is_not(None),
            ~up_to_date,
        )
        .order_by(Job.finished_at.desc(), Job.id.desc())
        .limit(1)
    )
    return (
        latest is not None
        and latest.error_retryable is False
        and latest.error_code not in ACCESS_FAILURES
        and latest.payload.get("commit_sha") == target.commit_sha
    )


def _up_to_date(ctx: JobContext) -> None:
    """Finish a check run with nothing to index; the active version stays as it is (R6)."""
    with ctx.publish() as (db, job):
        if publishable_repository(db, ctx, job) is not None:
            complete(db, job, message=UP_TO_DATE_MESSAGE)


def _batched(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [rows[i : i + INSERT_BATCH] for i in range(0, len(rows), INSERT_BATCH)]


def _tsvector(config: str, text: str) -> Any:
    return func.to_tsvector(literal_column(f"'{config}'::regconfig"), text)


def _insert_files(
    db: Session, target: Target, snapshot_id: uuid.UUID, files: list[EligibleFile]
) -> dict[str, uuid.UUID]:
    ids = {eligible.path: uuid.uuid4() for eligible in files}
    rows = [
        {
            "id": ids[eligible.path],
            "workspace_id": target.workspace_id,
            "snapshot_id": snapshot_id,
            "path": eligible.path,
            "language": eligible.language,
            "size_bytes": eligible.size_bytes,
            "line_count": eligible.line_count,
            "content_sha256": eligible.content_sha256,
            "content": eligible.content,
        }
        for eligible in files
    ]
    for batch in _batched(rows):
        db.execute(insert(File), batch)
    return ids


def _build_index(
    ctx: JobContext,
    target: Target,
    snapshot_id: uuid.UUID,
    result: FilterResult,
    stats: BuildStats,
) -> list[tuple[int, str]]:
    """Insert files, coverage, symbols, and chunks. Returns (doc chunk id, text) to embed."""
    ctx.event(
        "parsing",
        f"Parsing {sum(1 for f in result.files if f.language in PARSED_LANGUAGES)} source files",
    )
    declarations: dict[str, list[Declaration]] = {}
    for eligible in result.files:
        if eligible.language not in PARSED_LANGUAGES:
            continue
        parsed = parse_declarations(eligible.content, eligible.language)  # type: ignore[arg-type]
        declarations[eligible.path] = parsed.declarations
        if parsed.has_errors:
            stats.unsupported_syntax.append(eligible.path)
    ctx.check()

    ctx.event("building_search_index", f"Indexing {len(result.files)} files")
    with session_scope() as db:
        file_ids = _insert_files(db, target, snapshot_id, result.files)

        coverage_rows: list[dict[str, Any]] = [
            {
                "snapshot_id": snapshot_id,
                "path": entry.path,
                "entry_type": entry.entry_type,
                "reason": entry.reason,
                "detail": entry.detail,
            }
            for entry in result.skipped
        ]
        coverage_rows += [
            {
                "snapshot_id": snapshot_id,
                "path": path,
                "entry_type": "file",
                "reason": "unsupported_syntax",
                "detail": "indexed as text",
            }
            for path in stats.unsupported_syntax
        ]
        for batch in _batched(coverage_rows):
            db.execute(insert(CoverageEntry), batch)
        stats.skipped_entries = len(result.skipped)

        symbol_rows = [
            {
                "snapshot_id": snapshot_id,
                "file_id": file_ids[path],
                "name": declaration.name,
                "qualified_name": declaration.qualified_name,
                "kind": declaration.kind,
                "start_line": declaration.start_line,
                "end_line": declaration.end_line,
            }
            for path, items in declarations.items()
            for declaration in items
        ]
        for batch in _batched(symbol_rows):
            db.execute(insert(Symbol), batch)
        stats.symbols = len(symbol_rows)

        code_rows: list[dict[str, Any]] = []
        doc_rows: list[dict[str, Any]] = []
        for eligible in result.files:
            if eligible.language == "markdown":
                for doc in markdown_chunks(eligible.content):
                    doc_rows.append(
                        {
                            "snapshot_id": snapshot_id,
                            "file_id": file_ids[eligible.path],
                            "start_line": doc.start_line,
                            "end_line": doc.end_line,
                            "heading_path": doc.heading_path,
                            "text": doc.text,
                            "search_vector": _tsvector(
                                "english",
                                f"{doc.heading_path}\n{doc.text}"[:MAX_SEARCH_TEXT_CHARS],
                            ),
                        }
                    )
            else:
                for chunk in code_chunks(eligible.content):
                    code_rows.append(
                        {
                            "snapshot_id": snapshot_id,
                            "file_id": file_ids[eligible.path],
                            "start_line": chunk.start_line,
                            "end_line": chunk.end_line,
                            "search_vector": _tsvector(
                                "simple", chunk.search_text[:MAX_SEARCH_TEXT_CHARS]
                            ),
                        }
                    )
        # Multi-row VALUES so each row can carry its own to_tsvector() expression.
        for batch in _batched(code_rows):
            db.execute(insert(CodeChunk).values(batch))
        stats.code_chunks = len(code_rows)
        doc_ids: list[tuple[int, str]] = []
        for batch in _batched(doc_rows):
            inserted = db.execute(
                insert(DocChunk).values(batch).returning(DocChunk.id, DocChunk.text)
            )
            doc_ids += [(row_id, text) for row_id, text in inserted]
        stats.doc_chunks = len(doc_rows)
    return doc_ids


def _embed_docs(
    ctx: JobContext, snapshot_id: uuid.UUID, docs: list[tuple[int, str]], stats: BuildStats
) -> None:
    ctx.event("embedding_docs", f"Embedding {len(docs)} documentation passages")
    if not docs:
        return
    try:
        vectors = get_embedder().embed_documents([text for _, text in docs])
    except ProviderUnavailable:
        logger.warning("documentation embeddings unavailable; continuing without them")
        stats.embeddings_available = False
        with session_scope() as db:
            db.add(
                CoverageEntry(
                    snapshot_id=snapshot_id,
                    path="",
                    entry_type="directory",
                    reason="embeddings_unavailable",
                    detail="Documentation search uses keywords only for this version.",
                )
            )
        return
    with session_scope() as db:
        for (doc_id, _), vector in zip(docs, vectors, strict=True):
            db.execute(update(DocChunk).where(DocChunk.id == doc_id).values(embedding=vector))


def _coverage_summary(result: FilterResult, stats: BuildStats) -> dict[str, Any]:
    return {
        "eligible_files": len(result.files),
        "indexed_files": len(result.files),
        "source_lines": result.source_lines,
        "eligible_bytes": result.eligible_bytes,
        "symbols": stats.symbols,
        "code_chunks": stats.code_chunks,
        "doc_chunks": stats.doc_chunks,
        "skipped_entries": stats.skipped_entries
        + len(stats.unsupported_syntax)
        + (0 if stats.embeddings_available else 1),
        "embeddings_available": stats.embeddings_available,
    }


def _publish(
    ctx: JobContext, snapshot_id: uuid.UUID, result: FilterResult, stats: BuildStats
) -> None:
    ctx.event("publishing", "Publishing the indexed version")
    with ctx.publish() as (db, job):
        repository = publishable_repository(db, ctx, job)
        snapshot = db.get(Snapshot, snapshot_id)
        if snapshot is None:
            raise RuntimeError(f"snapshot {snapshot_id} disappeared before publishing")
        if repository is None:
            snapshot.status = "discarded"
            return
        snapshot.status = "ready"
        snapshot.ready_at = datetime.now(UTC)
        snapshot.coverage = _coverage_summary(result, stats)
        repository.active_snapshot_id = snapshot.id


def _mark_snapshot(snapshot_id: uuid.UUID, status: str, code: str, message: str) -> None:
    with session_scope() as db:
        db.execute(
            update(Snapshot)
            .where(Snapshot.id == snapshot_id, Snapshot.status == "building")
            .values(status=status, failure_code=code, failure_message=message)
        )


def handle_index(ctx: JobContext) -> None:
    settings: Settings = get_settings()
    gateway = get_gateway(settings)
    target = _resolve(ctx, gateway)
    if target is None:
        return

    version = index_version(settings)
    with session_scope() as db:
        existing = _existing_ready(db, target, version)
        existing_id = existing.id if existing is not None else None
        # A check indexes only a head that is neither indexed nor known to fail (FR-018).
        up_to_date = ctx.trigger == "check" and (
            existing_id is not None or _failed_permanently(db, target)
        )
    if up_to_date:
        _up_to_date(ctx)
        return
    if existing_id is not None:
        _reuse(ctx, existing_id)
        return

    with session_scope() as db:
        snapshot = Snapshot(
            workspace_id=target.workspace_id,
            repository_id=target.repository_id,
            commit_sha=target.commit_sha,
            branch=target.branch,
            index_version=version,
            status="building",
            job_id=ctx.job_id,
            job_attempt=ctx.attempt,
            coverage={},
        )
        db.add(snapshot)
        db.flush()
        snapshot_id = snapshot.id

    with log_context(snapshot_id=str(snapshot_id)):
        try:
            ctx.event("fetching_source", f"Downloading {target.commit_sha[:7]}")
            # The tarball is read with the installation token (research R4).
            with (
                github_answers(ctx, stage="fetching_source", denied="installation_cannot_read"),
                gateway.open_tarball(
                    target.installation_id, target.full_name, target.commit_sha
                ) as stream,
            ):
                ctx.event("filtering", "Filtering files")
                result = filter_members(iter_archive(stream, settings), settings)
            ctx.check()
            stats = BuildStats()
            docs = _build_index(ctx, target, snapshot_id, result, stats)
            _embed_docs(ctx, snapshot_id, docs, stats)
            _publish(ctx, snapshot_id, result, stats)
        except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
            message = "The repository archive could not be read."
            _mark_snapshot(snapshot_id, "failed", "archive_unreadable", message)
            raise JobFailure("archive_unreadable", message, permanent=False) from exc
        except LimitExceeded as exc:
            message = f"The repository exceeds the limit {exc.limit_name} ({exc.limit_value})."
            _mark_snapshot(snapshot_id, "failed", "limit_exceeded", message)
            raise JobFailure("limit_exceeded", message, permanent=True) from exc
        except LeaseLost:
            _mark_snapshot(snapshot_id, "discarded", "lease_lost", "A newer attempt took over.")
            raise
        except JobFailure as exc:
            _mark_snapshot(snapshot_id, "failed", exc.code, str(exc))
            raise
        except BaseException as exc:
            _mark_snapshot(snapshot_id, "failed", "internal_error", type(exc).__name__)
            raise


register(JOB_KIND, handle_index)
