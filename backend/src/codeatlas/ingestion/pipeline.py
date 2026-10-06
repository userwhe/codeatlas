"""The `index_repository` job: resolve, fetch, filter, parse, index, embed, publish (US1, T054)."""

import gzip
import logging
import tarfile
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, literal_column, select, update
from sqlalchemy.orm import Session

from codeatlas.auth.github_login import get_user_token
from codeatlas.config import Settings, get_settings
from codeatlas.db import session_scope
from codeatlas.github.gateway import (
    BranchNotFound,
    GitHubAccessDenied,
    GitHubGateway,
    GitHubNotFound,
    GitHubUnavailable,
    RepositoryEmpty,
    get_gateway,
    verify_access,
)
from codeatlas.ingestion.chunking import code_chunks, index_version, markdown_chunks
from codeatlas.ingestion.extract import LimitExceeded, iter_archive
from codeatlas.ingestion.filters import EligibleFile, FilterResult, filter_members
from codeatlas.ingestion.parse import Declaration, parse_declarations
from codeatlas.jobs.queue import JobFailure, LeaseLost, cancel
from codeatlas.jobs.worker import JobContext, register
from codeatlas.logging import log_context
from codeatlas.models import (
    CodeChunk,
    CoverageEntry,
    DocChunk,
    File,
    Job,
    Repository,
    Snapshot,
    Symbol,
    User,
)
from codeatlas.providers.embeddings import get_embedder
from codeatlas.providers.errors import ProviderUnavailable
from codeatlas.workspace.audit import record

logger = logging.getLogger(__name__)

JOB_KIND = "index_repository"
INSERT_BATCH = 500
PARSED_LANGUAGES = ("python", "typescript", "tsx")
# Keeps each tsvector well under PostgreSQL's 1 MB limit for pathological long-line files.
MAX_SEARCH_TEXT_CHARS = 100_000


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


def _record_denied(ctx: JobContext, detail: str) -> None:
    with session_scope() as db:
        record(
            db,
            action="access_denied",
            outcome="denied",
            workspace_id=ctx.workspace_id,
            actor_user_id=ctx.created_by,
            resource_type="repository",
            resource_id=str(ctx.repository_id),
            detail={"stage": "resolving_commit", "reason": detail},
        )


def _access_denied(ctx: JobContext, detail: str) -> JobFailure:
    _record_denied(ctx, detail)
    return JobFailure(
        "access_denied",
        "CodeAtlas can no longer read this repository on GitHub.",
        permanent=True,
    )


def _cancel_disconnected(ctx: JobContext) -> None:
    with ctx.publish() as (db, job):
        cancel(db, job, message="The repository was disconnected.")


def _resolve(ctx: JobContext, gateway: GitHubGateway) -> Target | None:
    """Re-check access, refresh repository metadata, and resolve the branch (FR-003)."""
    ctx.event("resolving_commit", "Checking access and resolving the branch")
    with session_scope() as db:
        repository = db.get(Repository, ctx.repository_id)
        if repository is None or repository.deleted_at is not None:
            return None
        user = db.get(User, ctx.created_by) if ctx.created_by else None
        if user is None:
            raise _access_denied(ctx, "requesting user no longer exists")
        try:
            token = get_user_token(db, user, gateway)
            github_repo, installation_id = verify_access(
                gateway, token, repository.github_repository_id
            )
        except (GitHubNotFound, GitHubAccessDenied) as exc:
            raise _access_denied(ctx, type(exc).__name__) from exc
        except GitHubUnavailable as exc:
            raise JobFailure(
                "github_unavailable", "GitHub is unavailable.", permanent=False
            ) from exc

        # Renamed, transferred, or reinstalled repositories keep working (research R5).
        repository.full_name = github_repo.full_name
        repository.default_branch = github_repo.default_branch
        repository.github_installation_id = installation_id
        branch = str(ctx.payload.get("branch") or github_repo.default_branch)
        try:
            commit_sha = gateway.resolve_commit(installation_id, github_repo.full_name, branch)
        except BranchNotFound as exc:
            raise JobFailure(
                "branch_not_found", f"The branch '{branch}' does not exist.", permanent=True
            ) from exc
        except RepositoryEmpty as exc:
            raise JobFailure(
                "repository_empty",
                "The repository has no commits, so there is nothing to index.",
                permanent=True,
            ) from exc
        except GitHubAccessDenied as exc:
            raise _access_denied(ctx, "installation") from exc
        except GitHubUnavailable as exc:
            raise JobFailure(
                "github_unavailable", "GitHub is unavailable.", permanent=False
            ) from exc

        job = db.get(Job, ctx.job_id)
        if job is not None:
            job.payload = {**job.payload, "branch": branch, "commit_sha": commit_sha}
        return Target(
            repository_id=repository.id,
            workspace_id=repository.workspace_id,
            full_name=github_repo.full_name,
            installation_id=installation_id,
            branch=branch,
            commit_sha=commit_sha,
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
        repository = db.get(Repository, ctx.repository_id, with_for_update=True)
        if repository is None or repository.deleted_at is not None:
            cancel(db, job, message="The repository was disconnected.")
            return
        repository.active_snapshot_id = snapshot_id


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
        repository = db.get(Repository, ctx.repository_id, with_for_update=True)
        snapshot = db.get(Snapshot, snapshot_id)
        if snapshot is None:
            raise RuntimeError(f"snapshot {snapshot_id} disappeared before publishing")
        if repository is None or repository.deleted_at is not None:
            snapshot.status = "discarded"
            cancel(db, job, message="The repository was disconnected.")
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
        _cancel_disconnected(ctx)
        return

    version = index_version(settings)
    with session_scope() as db:
        existing = _existing_ready(db, target, version)
        existing_id = existing.id if existing is not None else None
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
            with gateway.open_tarball(
                target.installation_id, target.full_name, target.commit_sha
            ) as stream:
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
        except GitHubUnavailable as exc:
            _mark_snapshot(snapshot_id, "failed", "github_unavailable", "GitHub is unavailable.")
            raise JobFailure(
                "github_unavailable", "GitHub is unavailable.", permanent=False
            ) from exc
        except (GitHubNotFound, GitHubAccessDenied) as exc:
            _mark_snapshot(snapshot_id, "failed", "access_denied", "Access was lost.")
            raise _access_denied(ctx, type(exc).__name__) from exc
        except BaseException as exc:
            _mark_snapshot(snapshot_id, "failed", "internal_error", type(exc).__name__)
            raise


register(JOB_KIND, handle_index)
