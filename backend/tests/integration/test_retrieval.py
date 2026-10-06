"""Question retrieval on indexed fixture repositories: code candidates, docs search, evidence."""

import hashlib
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import NO_CODE_ID, SAMPLE_APP_ID, SAMPLE_APP_PRIVATE_ID
from codeatlas.models import File, Repository, Snapshot, Symbol
from codeatlas.providers.errors import ProviderUnavailable
from codeatlas.retrieval.code import code_fts_candidates, symbol_and_path_candidates
from codeatlas.retrieval.docs import search_docs
from codeatlas.retrieval.evidence import (
    MAX_EVIDENCE_ITEMS,
    assemble_evidence,
    file_lines,
)

pytestmark = pytest.mark.integration

PERMISSIONS_QUESTION = "Where are repository permissions checked?"

Indexer = Callable[..., Snapshot]


class UnavailableEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("documents are not embedded at query time")

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        raise ProviderUnavailable("stub embedder is down")


@pytest.fixture
def index(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> Indexer:
    """Connect a fake GitHub repository, run the indexing job, and return its snapshot."""
    client = signed_in("octocat")

    def run(github_id: int, *, private: bool = False) -> Snapshot:
        payload: dict[str, object] = {"github_repository_id": github_id}
        if private:
            payload["accept_external_processing"] = True
        response = client.post("/v1/repositories", json=payload)
        assert response.status_code == 202, response.text
        while run_worker_once():
            pass
        repository = db.get(Repository, response.json()["repository"]["id"])
        assert repository is not None and repository.active_snapshot_id is not None
        snapshot = db.get(Snapshot, repository.active_snapshot_id)
        assert snapshot is not None and snapshot.status == "ready"
        return snapshot

    return run


@pytest.fixture
def sample_app(index: Indexer) -> Snapshot:
    return index(SAMPLE_APP_ID)


def stored_lines(db: Session, snapshot_id: uuid.UUID, path: str) -> list[str]:
    content = db.scalar(
        select(File.content).where(File.snapshot_id == snapshot_id, File.path == path)
    )
    assert content is not None
    return file_lines(content)


def file_ids(db: Session, snapshot_id: uuid.UUID) -> set[uuid.UUID]:
    return set(db.scalars(select(File.id).where(File.snapshot_id == snapshot_id)))


# Code candidates


def test_exact_symbol_match_spans_the_declaration(db: Session, sample_app: Snapshot) -> None:
    candidates = symbol_and_path_candidates(
        db, sample_app.id, "What does `check_repository_access` return?"
    )

    declaration = db.scalars(
        select(Symbol).where(
            Symbol.snapshot_id == sample_app.id, Symbol.name == "check_repository_access"
        )
    ).one()
    first = candidates[0]
    assert (first.source_type, first.path, first.label_hint) == (
        "symbol",
        "app/auth/access.py",
        "check_repository_access",
    )
    assert (first.start_line, first.end_line) == (declaration.start_line, declaration.end_line)


def test_qualified_names_paths_and_case_insensitive_matches(
    db: Session, sample_app: Snapshot
) -> None:
    candidates = symbol_and_path_candidates(
        db, sample_app.id, "Compare AccessPolicy.can_read, usercard, and app/main.py"
    )

    found = {(c.source_type, c.path, c.label_hint): c for c in candidates}
    can_read = found[("symbol", "app/auth/access.py", "AccessPolicy.can_read")]
    user_card = found[("symbol", "src/components/UserCard.tsx", "UserCard")]
    main = found[("code", "app/main.py", "app/main.py")]
    assert can_read.score > user_card.score  # exact case beats case-insensitive
    assert (main.start_line, main.end_line) == (
        1,
        len(stored_lines(db, sample_app.id, "app/main.py")),
    )


def test_basename_without_extension_matches_files(db: Session, sample_app: Snapshot) -> None:
    candidates = symbol_and_path_candidates(db, sample_app.id, "What is in the readme?")
    readme = [c for c in candidates if c.path == "README.md"]
    assert readme and readme[0].source_type == "doc" and readme[0].start_line == 1


def test_no_exact_matches_for_unrelated_words(db: Session, sample_app: Snapshot) -> None:
    assert symbol_and_path_candidates(db, sample_app.id, "Which payment provider is used?") == []
    assert symbol_and_path_candidates(db, sample_app.id, "?! the and of") == []


def test_code_full_text_ranks_the_access_checks(db: Session, sample_app: Snapshot) -> None:
    candidates = code_fts_candidates(db, sample_app.id, PERMISSIONS_QUESTION)

    # The definition and its only caller; `checked` matches `check` through its stem.
    assert {c.path for c in candidates[:2]} == {"app/auth/access.py", "app/main.py"}
    access = next(c for c in candidates if c.path == "app/auth/access.py")
    assert (access.source_type, access.start_line) == ("code", 1)
    assert [c.score for c in candidates] == sorted((c.score for c in candidates), reverse=True)
    assert len(code_fts_candidates(db, sample_app.id, PERMISSIONS_QUESTION, limit=1)) == 1


def test_code_full_text_tolerates_query_syntax(db: Session, sample_app: Snapshot) -> None:
    hostile = "check' | !access & (repository:*) <-> \\ '; DROP TABLE files; --"
    candidates = code_fts_candidates(db, sample_app.id, hostile)
    assert "app/auth/access.py" in {c.path for c in candidates}
    assert code_fts_candidates(db, sample_app.id, "&|!() ' :* <->") == []
    assert file_ids(db, sample_app.id)


# Documentation search


def test_docs_search_returns_the_setup_section(db: Session, sample_app: Snapshot) -> None:
    result = search_docs(db, sample_app, "setup")

    assert result.degraded is False
    first = result.hits[0]
    assert (first.path, first.heading_path) == ("README.md", "Sample App > Setup")
    assert first.text.startswith("## Setup")
    lines = stored_lines(db, sample_app.id, "README.md")
    assert first.text == "\n".join(lines[first.start_line - 1 : first.end_line])
    assert len(search_docs(db, sample_app, "setup", limit=2).hits) <= 2


def test_docs_search_degrades_when_the_query_cannot_be_embedded(
    db: Session, sample_app: Snapshot, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = UnavailableEmbedder()
    stubbed = search_docs(db, sample_app, "setup", embedder=stub)
    monkeypatch.setattr(settings, "fake_embedder_mode", "unavailable")
    configured = search_docs(db, sample_app, "setup")

    for result in (stubbed, configured):
        assert result.degraded is True
        assert [(hit.path, hit.heading_path) for hit in result.hits] == [
            ("README.md", "Sample App > Setup")
        ]
    assert stub.calls == 1


def test_docs_search_degrades_for_snapshots_without_embeddings(
    db: Session, index: Indexer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "fake_embedder_mode", "unavailable")
    snapshot = index(NO_CODE_ID)
    assert snapshot.coverage["embeddings_available"] is False
    stub = UnavailableEmbedder()

    result = search_docs(db, snapshot, "release process", embedder=stub)

    assert result.degraded is True
    assert stub.calls == 0  # no embedding call for a snapshot indexed without vectors
    assert result.hits[0].path == "docs/release-process.md"


# Evidence


def test_evidence_is_labeled_with_exact_excerpts(db: Session, sample_app: Snapshot) -> None:
    evidence, degraded = assemble_evidence(db, sample_app, PERMISSIONS_QUESTION)

    assert degraded is False
    assert 0 < len(evidence) <= MAX_EVIDENCE_ITEMS
    assert [item.label for item in evidence] == [f"E{n}" for n in range(1, len(evidence) + 1)]
    assert [item.rank for item in evidence] == list(range(1, len(evidence) + 1))
    assert "app/auth/access.py" in {item.path for item in evidence}
    for item in evidence:
        lines = stored_lines(db, sample_app.id, item.path)
        assert 1 <= item.start_line <= item.end_line <= len(lines)
        assert item.excerpt == "\n".join(lines[item.start_line - 1 : item.end_line])
        assert item.excerpt_sha256 == hashlib.sha256(item.excerpt.encode()).digest()
        assert item.commit_sha == sample_app.commit_sha
    spans: dict[str, list[tuple[int, int]]] = {}
    for item in evidence:
        for start, end in spans.get(item.path, []):
            assert item.end_line + 1 < start or end + 1 < item.start_line  # merged ranges
        spans.setdefault(item.path, []).append((item.start_line, item.end_line))


def test_named_symbol_is_the_first_evidence(db: Session, sample_app: Snapshot) -> None:
    evidence, _ = assemble_evidence(
        db, sample_app, "How does AccessPolicy decide who can read?", embedder=UnavailableEmbedder()
    )

    declaration = db.scalars(
        select(Symbol).where(Symbol.snapshot_id == sample_app.id, Symbol.name == "AccessPolicy")
    ).one()
    first = evidence[0]
    assert (first.label, first.source_type, first.path) == ("E1", "symbol", "app/auth/access.py")
    # The declaration overlaps the file's full-text chunk, so the two merge at the symbol's rank.
    assert first.start_line <= declaration.start_line <= declaration.end_line <= first.end_line
    assert "class AccessPolicy:" in first.excerpt
    assert [item.path for item in evidence].count("app/auth/access.py") == 1


def test_evidence_reports_degraded_docs(db: Session, sample_app: Snapshot) -> None:
    evidence, degraded = assemble_evidence(
        db, sample_app, "How do I set up the project?", embedder=UnavailableEmbedder()
    )
    assert degraded is True
    assert ("README.md", "doc") in {(item.path, item.source_type) for item in evidence}


def test_retrieval_never_crosses_snapshots(db: Session, index: Indexer) -> None:
    public = index(SAMPLE_APP_ID)
    private = index(SAMPLE_APP_PRIVATE_ID, private=True)  # the same files in another repository
    handbook = index(NO_CODE_ID)
    public_files = file_ids(db, public.id)
    assert public_files.isdisjoint(file_ids(db, private.id))

    question = "Where is check_repository_access in app/auth/access.py, and what is the setup?"
    exact = symbol_and_path_candidates(db, public.id, question)
    full_text = code_fts_candidates(db, public.id, question)
    docs = search_docs(db, public, question)
    assert exact and full_text and docs.hits
    assert {c.file_id for c in exact + full_text} <= public_files
    assert {hit.file_id for hit in docs.hits} <= public_files

    handbook_question = "What happens during onboarding and the release process?"
    handbook_paths = set(db.scalars(select(File.path).where(File.snapshot_id == handbook.id)))
    evidence, _ = assemble_evidence(db, public, handbook_question)
    public_paths = set(db.scalars(select(File.path).where(File.snapshot_id == public.id)))
    assert {item.path for item in evidence} <= public_paths
    assert not {item.path for item in evidence} & (handbook_paths - public_paths)
    assert {hit.file_id for hit in search_docs(db, public, handbook_question).hits} <= public_files
