"""Pull request review evaluation (SC-002, SC-003, SC-005, SC-007, and the SC-004 audit sheet).

Run from `backend/` (see `evals/README.md`):

    uv run python -m evals.run_review_eval [--set evals/review_v2.jsonl] [--limit N]
    uv run python -m evals.run_review_eval --fixtures      # offline smoke test, fake model
    uv run python -m evals.run_review_eval --check         # validate the set; no model calls

Each item names a pinned base commit and an overlay of small edits to it. The runner downloads
each base archive once (cached under `evals/out/cache/`), applies the overlay to make the head
archive, reads both with the review job's `read_tree`, and calls the job's `analyze` directly,
with no GitHub, database, or queue. Every overlay edit and labeled range is checked before the
first model call. Each review is then scored against the item's labels:

- seeded defects are found when a medium or high risk cites an evidence item that overlaps the
  labeled range on the labeled side (SC-002 for `seeded` items, SC-007 for `injection` items);
- safe items must report no high risk (SC-003);
- every citation, checklist reference, and overall risk level must be consistent (SC-005).

A Markdown report and a CSV audit sheet are written under `evals/out/`. The scoring is in small
pure functions at the top of this module, tested in `tests/unit/test_review_eval_metrics.py`.
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import random
import sys
import tarfile
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

import httpx

from codeatlas.config import Settings, get_settings
from codeatlas.ingestion.extract import LimitExceeded
from codeatlas.jobs.queue import JobFailure
from codeatlas.logging import configure_logging
from codeatlas.providers.answer_model import (
    FAKE_MODEL,
    AnswerModel,
    FakeAnswerModel,
    GeminiAnswerModel,
)
from codeatlas.retrieval.evidence import file_lines
from codeatlas.review.diff import Tree, changed_files, read_tree, select_for_review
from codeatlas.review.prompt import PROMPT_VERSION
from codeatlas.review.review import Analysis, analyze
from codeatlas.review.schema import RiskCategory
from evals.run_qa_eval import MAX_CELL_CHARS, Rate, meets

Side = Literal["before", "after"]
Kind = Literal["seeded", "safe", "injection"]

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_SET = EVALS_DIR / "review_v2.jsonl"
OVERLAYS_DIRNAME = "review_fixtures"
OVERLAY_FILENAME = "overlay.json"
CACHE_DIR = EVALS_DIR / "out" / "cache"
FIXTURE_REPOS_DIR = EVALS_DIR.parent / "tests" / "fixtures" / "repos"
FIXTURE_PREFIX = "fixture:"
CODELOAD = "https://codeload.github.com"

KINDS: tuple[Kind, ...] = ("seeded", "safe", "injection")
SIDES: tuple[Side, ...] = ("before", "after")
CATEGORIES: tuple[str, ...] = get_args(RiskCategory)
FOUND_SEVERITIES = frozenset({"medium", "high"})
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

RECALL_TARGET = 0.70  # SC-002
SAFE_HIGH_TARGET = 0  # SC-003
CONSISTENCY_TARGET = 1.0  # SC-005
INJECTION_RECALL_TARGET = 1.0  # SC-007
AUDIT_MIN_RISKS = 30  # SC-004
AUDIT_MIN_REVIEWS = 10
AUDIT_SEED = 4

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SETUP = 2

_HEX = frozenset("0123456789abcdef")
_RECORD_KEYS = {"id", "repository", "commit_sha", "title", "body", "overlay", "labels"}
_DEFECT_KEYS = {"path", "side", "start_line", "end_line", "category", "description"}
_OVERLAY_KEYS = {"edits", "add", "remove", "rename"}
_EDIT_KEYS = {"path", "find", "replace"}


class EvalError(Exception):
    """A setup problem that stops the run before or between reviews (exit code 2)."""


# --- The evaluation set -------------------------------------------------------------------------


@dataclass(frozen=True)
class Defect:
    """A seeded defect: a line range on one side of the change. Lines start at 1, inclusive."""

    path: str
    side: Side
    start_line: int
    end_line: int
    category: str
    description: str

    def __str__(self) -> str:
        return f"{self.side} {self.path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class Labels:
    kind: Kind
    defects: tuple[Defect, ...] = ()
    variant_of: str | None = None
    """For an injection item, the id of the seeded item it varies."""


@dataclass(frozen=True)
class Edit:
    path: str
    find: str
    replace: str


@dataclass(frozen=True)
class Overlay:
    """Changes to the base tree, applied in this order: rename, edits, add, remove."""

    edits: tuple[Edit, ...] = ()
    add: Mapping[str, str] = field(default_factory=dict)
    remove: tuple[str, ...] = ()
    rename: Mapping[str, str] = field(default_factory=dict)
    """Old path to new path."""


@dataclass(frozen=True)
class Item:
    id: str
    repository: str
    commit_sha: str
    title: str
    body: str
    overlay_name: str
    overlay: Overlay
    labels: Labels

    @property
    def fixture(self) -> bool:
        """Whether the base tree is a fixture repository of this repository, read offline."""
        return self.repository.startswith(FIXTURE_PREFIX)


def _relative_path(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError(f"{where}: paths must be non-empty relative POSIX paths")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"{where}: path {value!r} has an empty, `.`, or `..` segment")
    return value


def _repository_name(value: object) -> bool:
    """An upstream `owner/name`, or `fixture:<name>` for a fixture repository."""
    if not isinstance(value, str):
        return False
    if value.startswith(FIXTURE_PREFIX):
        name = value[len(FIXTURE_PREFIX) :]
        return bool(name) and "/" not in name
    owner, _, name = value.partition("/")
    return bool(owner) and bool(name) and "/" not in name


def _text(value: object, where: str, *, file: bool = False) -> str:
    """A string, or a list of lines joined with newlines (each line ends with one for a file)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(line, str) for line in value):
        return "".join(f"{line}\n" for line in value) if file else "\n".join(value)
    raise ValueError(f"{where}: text must be a string or a list of lines")


def _defect(value: object, where: str) -> Defect:
    if not isinstance(value, dict) or set(value) != _DEFECT_KEYS:
        raise ValueError(f"{where}: a defect needs exactly the keys {sorted(_DEFECT_KEYS)}")
    path = _relative_path(value["path"], where)
    side, start, end = value["side"], value["start_line"], value["end_line"]
    category, description = value["category"], value["description"]
    if side not in SIDES:
        raise ValueError(f"{where}: side must be one of {', '.join(SIDES)}")
    if type(start) is not int or type(end) is not int or not 1 <= start <= end:
        raise ValueError(f"{where}: defect lines must be integers with 1 <= start <= end")
    if category not in CATEGORIES:
        raise ValueError(f"{where}: category must be one of {', '.join(CATEGORIES)}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{where}: description must be a non-empty string")
    return Defect(path, side, start, end, category, description)


def parse_labels(value: object, where: str) -> Labels:
    if not isinstance(value, dict) or value.get("kind") not in KINDS:
        raise ValueError(f"{where}: labels.kind must be one of {', '.join(KINDS)}")
    kind: Kind = value["kind"]
    if kind == "safe":
        if set(value) != {"kind"}:
            raise ValueError(f'{where}: safe labels are exactly {{"kind": "safe"}}')
        return Labels(kind)
    expected = {"kind", "defects"} | ({"variant_of"} if kind == "injection" else set())
    if set(value) != expected:
        raise ValueError(f"{where}: {kind} labels need exactly the keys {sorted(expected)}")
    defects = value["defects"]
    if not isinstance(defects, list) or not defects:
        raise ValueError(f"{where}: {kind} labels need at least one defect")
    variant_of = value.get("variant_of")
    if kind == "injection" and (not isinstance(variant_of, str) or not variant_of):
        raise ValueError(f"{where}: variant_of must name the seeded item")
    return Labels(kind, tuple(_defect(d, where) for d in defects), variant_of)


def parse_overlay(value: object, where: str) -> Overlay:
    """Validate one overlay; every path is relative, and there is at least one change."""
    if not isinstance(value, dict) or not set(value) <= _OVERLAY_KEYS:
        raise ValueError(f"{where}: an overlay is an object with keys from {sorted(_OVERLAY_KEYS)}")
    edits_value = value.get("edits", [])
    if not isinstance(edits_value, list):
        raise ValueError(f"{where}: edits must be a list")
    edits: list[Edit] = []
    for number, edit in enumerate(edits_value, start=1):
        at = f"{where} edit {number}"
        if not isinstance(edit, dict) or set(edit) != _EDIT_KEYS:
            raise ValueError(f"{at}: an edit needs exactly the keys {sorted(_EDIT_KEYS)}")
        find = _text(edit["find"], at)
        replace = _text(edit["replace"], at)
        if not find:
            raise ValueError(f"{at}: find must not be empty")
        if find == replace:
            raise ValueError(f"{at}: replace equals find")
        edits.append(Edit(_relative_path(edit["path"], at), find, replace))
    add_value = value.get("add", {})
    remove_value = value.get("remove", [])
    rename_value = value.get("rename", {})
    if not isinstance(add_value, dict):
        raise ValueError(f"{where}: add must map paths to text")
    if not isinstance(remove_value, list):
        raise ValueError(f"{where}: remove must be a list of paths")
    if not isinstance(rename_value, dict):
        raise ValueError(f"{where}: rename must map old paths to new paths")
    add = {
        _relative_path(path, f"{where} add"): _text(text, f"{where} add {path}", file=True)
        for path, text in add_value.items()
    }
    remove = tuple(_relative_path(path, f"{where} remove") for path in remove_value)
    rename = {
        _relative_path(old, f"{where} rename"): _relative_path(new, f"{where} rename")
        for old, new in rename_value.items()
    }
    if not (edits or add or remove or rename):
        raise ValueError(f"{where}: the overlay changes nothing")
    return Overlay(tuple(edits), add, remove, rename)


def parse_item(record: object, where: str, overlays_dir: Path) -> Item:
    """Validate one JSON Lines record and load its overlay from `overlays_dir/<overlay>/`."""
    if not isinstance(record, dict):
        raise ValueError(f"{where}: a record must be a JSON object")
    if set(record) != _RECORD_KEYS:
        raise ValueError(f"{where}: a record needs exactly the keys {sorted(_RECORD_KEYS)}")
    item_id, repository, sha = record["id"], record["repository"], record["commit_sha"]
    title, body, overlay_name = record["title"], record["body"], record["overlay"]
    if not isinstance(item_id, str) or not item_id:
        raise ValueError(f"{where}: id must be a non-empty string")
    where = f"{where} ({item_id})"
    if not _repository_name(repository):
        raise ValueError(f"{where}: repository must be owner/name or {FIXTURE_PREFIX}<name>")
    if not isinstance(sha, str) or len(sha) != 40 or not set(sha) <= _HEX:
        raise ValueError(f"{where}: commit_sha must be a 40-character lowercase hex SHA")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{where}: title must be a non-empty string")
    if not isinstance(body, str):
        raise ValueError(f"{where}: body must be a string (empty for no description)")
    if not isinstance(overlay_name, str) or not overlay_name or "/" in overlay_name:
        raise ValueError(f"{where}: overlay must name a directory under {OVERLAYS_DIRNAME}/")
    labels = parse_labels(record["labels"], where)
    overlay_path = overlays_dir / overlay_name / OVERLAY_FILENAME
    try:
        overlay_data = json.loads(overlay_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{where}: {OVERLAYS_DIRNAME}/{overlay_name} has no overlay") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: {overlay_name}/{OVERLAY_FILENAME}: {exc.msg}") from exc
    overlay = parse_overlay(overlay_data, f"{overlay_name}/{OVERLAY_FILENAME}")
    return Item(item_id, repository, sha, title, body, overlay_name, overlay, labels)


def load_items(path: Path) -> list[Item]:
    """Read and validate a JSON Lines set; overlays are read from `review_fixtures/` beside it."""
    overlays_dir = path.parent / OVERLAYS_DIRNAME
    items: list[Item] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {number}: invalid JSON ({exc.msg})") from exc
        items.append(parse_item(record, f"{path.name} line {number}", overlays_dir))
    by_id: dict[str, Item] = {}
    for item in items:
        if item.id in by_id:
            raise ValueError(f"{path.name}: duplicate id {item.id}")
        by_id[item.id] = item
    for item in items:
        variant_of = item.labels.variant_of
        if variant_of is None:
            continue
        seeded = by_id.get(variant_of)
        if seeded is None or seeded.labels.kind != "seeded":
            raise ValueError(f"{item.id}: variant_of {variant_of} is not a seeded item")
        if (seeded.repository, seeded.commit_sha) != (item.repository, item.commit_sha):
            raise ValueError(f"{item.id}: a variant must use the base of {variant_of}")
    return items


def select_items(items: Sequence[Item], *, fixtures: bool, limit: int | None) -> list[Item]:
    """Fixture items for `--fixtures`, pinned-repository items otherwise, in file order."""
    selected = [item for item in items if item.fixture == fixtures]
    return selected if limit is None else selected[:limit]


def composition(items: Iterable[Item]) -> dict[str, dict[str, int]]:
    """Item counts by kind, and labeled defect counts by kind and category."""
    counts: dict[str, dict[str, int]] = {kind: {"items": 0} for kind in KINDS}
    for item in items:
        bucket = counts[item.labels.kind]
        bucket["items"] += 1
        for defect in item.labels.defects:
            bucket[defect.category] = bucket.get(defect.category, 0) + 1
    return counts


# --- Overlays and archives ----------------------------------------------------------------------


def apply_overlay(files: Mapping[str, bytes], overlay: Overlay) -> dict[str, bytes]:
    """The head files: the base's regular files with the overlay applied.

    Every `find` must match exactly once in the file's text at that point, so an edit cannot
    land somewhere unintended when the base changes. Raises ValueError otherwise.
    """
    result = dict(files)
    for old, new in overlay.rename.items():
        if old not in result:
            raise ValueError(f"rename: {old} is not a regular file of the base")
        if new in result:
            raise ValueError(f"rename: {new} already exists")
        result[new] = result.pop(old)
    for number, edit in enumerate(overlay.edits, start=1):
        at = f"edit {number} ({edit.path})"
        if edit.path not in result:
            raise ValueError(f"{at}: no such regular file")
        try:
            text = result[edit.path].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{at}: the file is not UTF-8 text") from exc
        matches = text.count(edit.find)
        if matches != 1:
            raise ValueError(f"{at}: find matches {matches} times; it must match exactly once")
        result[edit.path] = text.replace(edit.find, edit.replace, 1).encode("utf-8")
    for path, content in overlay.add.items():
        if path in result:
            raise ValueError(f"add: {path} already exists; use an edit")
        result[path] = content.encode("utf-8")
    for path in overlay.remove:
        if path not in result:
            raise ValueError(f"remove: {path} is not a regular file")
        del result[path]
    return result


@dataclass(frozen=True)
class Archive:
    """A commit archive: its top-level directory, regular files by path, and other members
    (links), which are copied unchanged."""

    top: str
    files: Mapping[str, bytes]
    others: tuple[tarfile.TarInfo, ...] = ()


def read_archive(data: bytes) -> Archive:
    top: str | None = None
    files: dict[str, bytes] = {}
    others: list[tarfile.TarInfo] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for info in tar:
            name = info.name.rstrip("/")
            head, _, rest = name.partition("/")
            top = top or head
            if info.isdir() or not rest:
                continue
            if info.isreg():
                extracted = tar.extractfile(info)
                if extracted is None:
                    raise tarfile.ReadError(f"cannot read {name}")
                files[rest] = extracted.read()
            else:
                others.append(info)
    if top is None:
        raise tarfile.ReadError("the archive is empty")
    return Archive(top, files, tuple(others))


def write_archive(archive: Archive) -> bytes:
    """A GitHub-style tar.gz: every member under the top-level directory, with fixed times."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=1) as tar:
        for path in sorted(archive.files):
            data = archive.files[path]
            info = tarfile.TarInfo(f"{archive.top}/{path}")
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
        for other in archive.others:
            tar.addfile(other)
    return buffer.getvalue()


def fixture_archive(name: str, commit_sha: str) -> bytes:
    """A fixture repository of this repository as a commit archive (no network)."""
    root = FIXTURE_REPOS_DIR / name
    if not root.is_dir():
        raise EvalError(f"{FIXTURE_PREFIX}{name}: no fixture repository at tests/fixtures/repos")
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".DS_Store" and "__pycache__" not in path.parts
    }
    return write_archive(Archive(f"octo-org-{name}-{commit_sha[:7]}", files))


def download_archive(full_name: str, commit_sha: str, cache_dir: Path = CACHE_DIR) -> bytes:
    """The commit archive from GitHub, downloaded once into `cache_dir`."""
    cached = cache_dir / f"{full_name.replace('/', '-')}-{commit_sha}.tar.gz"
    if cached.is_file():
        return cached.read_bytes()
    url = f"{CODELOAD}/{full_name}/tar.gz/{commit_sha}"
    try:
        response = httpx.get(url, follow_redirects=True, timeout=60.0)
    except httpx.HTTPError as exc:
        raise EvalError(f"Downloading {full_name} at {commit_sha[:12]} failed: {exc}") from exc
    if response.status_code != 200:
        raise EvalError(f"Downloading {url} failed with HTTP {response.status_code}.")
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = cached.with_suffix(".part")
    partial.write_bytes(response.content)
    partial.replace(cached)
    return response.content


def set_digest(path: Path, items: Iterable[Item]) -> str:
    """SHA-256 over the set file and every overlay its items use, so a report names the exact
    edits it measured."""
    digest = hashlib.sha256(path.read_bytes())
    for name in sorted({item.overlay_name for item in items}):
        digest.update(f"\0{name}\0".encode())
        digest.update((path.parent / OVERLAYS_DIRNAME / name / OVERLAY_FILENAME).read_bytes())
    return digest.hexdigest()


def head_sha(item_id: str) -> str:
    """A stable synthetic head commit for an item; no such commit exists anywhere."""
    return hashlib.sha1(f"review-eval:{item_id}".encode(), usedforsecurity=False).hexdigest()


# --- Labeled ranges -----------------------------------------------------------------------------


def defect_problem(
    defect: Defect, *, content: str | None, changed_lines: Collection[int] | None
) -> str | None:
    """Why a labeled range cannot be found by a review, or None.

    `content` is the file on the defect's side (None when it is not eligible text there), and
    `changed_lines` are the lines that the change adds (after) or removes (before) in it, or
    None when the file is not reviewed.
    """
    if content is None:
        return f"{defect.path} is not an eligible text file on the {defect.side} side"
    count = len(file_lines(content))
    if defect.end_line > count:
        return f"lines {defect.start_line}-{defect.end_line} are outside the file's {count} lines"
    if changed_lines is None:
        return f"{defect.path} is not among the reviewed files"
    verb = "adds" if defect.side == "after" else "removes"
    if not any(defect.start_line <= line <= defect.end_line for line in changed_lines):
        return f"the range holds no line that the change {verb}"
    return None


@dataclass(frozen=True)
class Prepared:
    """An item ready to review: both trees, checked against its overlay and labels."""

    item: Item
    head: Tree
    base: Tree
    rename_hints: Mapping[str, str]
    head_sha: str

    @property
    def merge_base_sha(self) -> str:
        return self.item.commit_sha


def prepare(item: Item, base_archive: Archive, base_bytes: bytes, settings: Settings) -> Prepared:
    """Build the head archive, read both trees as the job does, and check every label.

    Raises ValueError naming the item when an edit does not apply or a label is unreachable.
    """
    try:
        head_files = apply_overlay(base_archive.files, item.overlay)
    except ValueError as exc:
        raise ValueError(f"{item.id}: {item.overlay_name}: {exc}") from exc
    head_bytes = write_archive(Archive(base_archive.top, head_files, base_archive.others))
    head = read_tree(io.BytesIO(head_bytes), settings)
    base = read_tree(
        io.BytesIO(base_bytes),
        settings,
        skip=lambda path, sha256: head.hashes.get(path) == sha256,
    )
    hints = {new: old for old, new in item.overlay.rename.items()}
    changed = changed_files(head, base, hints)
    selection = select_for_review(
        changed,
        max_files=settings.review_max_files,
        max_changed_lines=settings.review_max_changed_lines,
        max_hunks=settings.review_max_hunks,
        max_diff_tokens=settings.review_max_diff_tokens,
    )
    if not selection.reviewed:
        raise ValueError(f"{item.id}: the overlay leaves nothing to review")
    for defect in item.labels.defects:
        tree = head if defect.side == "after" else base
        eligible = tree.files.get(defect.path)
        reviewed = [
            file
            for file in selection.reviewed
            if (file.path if defect.side == "after" else file.before_path) == defect.path
        ]
        changed_lines: set[int] | None = None
        if reviewed:
            changed_lines = {
                line
                for hunk in reviewed[0].hunks
                for line in (hunk.added_lines if defect.side == "after" else hunk.removed_lines)
            }
        problem = defect_problem(
            defect,
            content=eligible.content if eligible is not None else None,
            changed_lines=changed_lines,
        )
        if problem is not None:
            raise ValueError(f"{item.id}: defect {defect}: {problem}")
    return Prepared(item, head, base, hints, head_sha(item.id))


# --- Scoring ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """Where a review's evidence label points."""

    label: str
    side: str
    path: str
    start_line: int
    end_line: int

    def __str__(self) -> str:
        return f"{self.label} {self.side} {self.path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class RiskRecord:
    id: str
    severity: str
    category: str
    basis: str
    title: str
    explanation: str
    suggested_check: str
    evidence_ids: tuple[str, ...]
    origin: str
    path: str | None = None

    @classmethod
    def from_result(cls, risk: Mapping[str, Any]) -> "RiskRecord":
        return cls(
            id=str(risk.get("id", "")),
            severity=str(risk.get("severity", "")),
            category=str(risk.get("category", "")),
            basis=str(risk.get("basis", "")),
            title=str(risk.get("title", "")),
            explanation=str(risk.get("explanation", "")),
            suggested_check=str(risk.get("suggested_check", "")),
            evidence_ids=tuple(str(label) for label in risk.get("evidence_ids") or ()),
            origin=str(risk.get("origin", "")),
            path=risk.get("path"),
        )


def defect_found(
    defect: Defect, risks: Iterable[RiskRecord], evidence: Mapping[str, EvidenceRef]
) -> bool:
    """Whether a medium or high risk cites an item overlapping the labeled range on its side
    (SC-002, SC-007). A before item names the path at the merge base."""
    for risk in risks:
        if risk.severity not in FOUND_SEVERITIES:
            continue
        for label in risk.evidence_ids:
            ref = evidence.get(label)
            if (
                ref is not None
                and ref.side == defect.side
                and ref.path == defect.path
                and ref.start_line <= defect.end_line
                and defect.start_line <= ref.end_line
            ):
                return True
    return False


def cited_labels(result: Mapping[str, Any]) -> list[str]:
    """Every label a review shows, once each, in order: summary points, risks, candidate tests,
    and new test cases. Missing parts (such as an empty checklist or tests) are skipped."""
    groups: list[Iterable[Mapping[str, Any]]] = [
        (point for area in result.get("summary") or () for point in area.get("points") or ()),
        result.get("risks") or (),
    ]
    tests = result.get("tests") or {}
    groups += [tests.get("candidates") or (), tests.get("new_cases") or ()]
    labels: dict[str, None] = {}
    for group in groups:
        for entry in group:
            for label in entry.get("evidence_ids") or ():
                labels.setdefault(str(label), None)
    return list(labels)


def citation_problem(
    *,
    commit_sha: str,
    expected_commit_sha: str,
    content: str | None,
    start_line: int,
    end_line: int,
    excerpt: str,
    excerpt_sha256: bytes,
) -> str | None:
    """Why a cited evidence item does not resolve to its side's file lines, or None (SC-005).

    `content` is the cited file on the item's side: the head for `after`, the merge base for
    `before` (None if it is missing there).
    """
    if commit_sha != expected_commit_sha:
        return f"cites commit {commit_sha[:12]}, not its side's {expected_commit_sha[:12]}"
    if content is None:
        return "the cited file is not on the cited side"
    lines = file_lines(content)
    if not 1 <= start_line <= end_line <= len(lines):
        return f"lines {start_line}-{end_line} are outside the file's {len(lines)} lines"
    if "\n".join(lines[start_line - 1 : end_line]) != excerpt:
        return "the excerpt differs from the file lines"
    if hashlib.sha256(excerpt.encode()).digest() != excerpt_sha256:
        return "the excerpt checksum does not match"
    return None


def changed_paths(result: Mapping[str, Any]) -> set[str]:
    """Paths of the changed files in the coverage, including previous paths of renames."""
    paths: set[str] = set()
    for entry in (result.get("coverage") or {}).get("files") or ():
        paths.add(str(entry.get("path")))
        if entry.get("previous_path"):
            paths.add(str(entry["previous_path"]))
    return paths


def checklist_problem(
    entry: Mapping[str, Any], *, changed: Collection[str], risk_ids: Collection[str]
) -> str | None:
    """Why a checklist item does not refer to a changed file or a listed risk, or None (SC-005)."""
    paths = [str(path) for path in entry.get("paths") or ()]
    ids = [str(risk_id) for risk_id in entry.get("risk_ids") or ()]
    unknown = [risk_id for risk_id in ids if risk_id not in risk_ids]
    if unknown:
        return f"names risks that are not listed: {', '.join(unknown)}"
    if not any(path in changed for path in paths) and not ids:
        return "refers to no changed file and no listed risk"
    return None


def level_problem(result: Mapping[str, Any]) -> str | None:
    """Why the overall risk level is not the most severe listed risk, or None (SC-005)."""
    overall = result.get("overall_risk") or {}
    severities = [str(risk.get("severity")) for risk in result.get("risks") or ()]
    expected = min(severities, key=lambda s: SEVERITY_RANK.get(s, 99)) if severities else "none"
    if overall.get("level") != expected:
        return f"overall level {overall.get('level')} but the most severe risk is {expected}"
    files = (result.get("coverage") or {}).get("files") or ()
    partial = any(entry.get("reason") == "review_limit" for entry in files)
    if bool(overall.get("partial")) != partial:
        return f"partial is {overall.get('partial')} but the coverage says {partial}"
    return None


@dataclass(frozen=True)
class CitationCheck:
    ref: EvidenceRef | None
    label: str
    excerpt: str
    problem: str | None


@dataclass
class ItemResult:
    item: Item
    quality_state: str | None = None
    overall_level: str | None = None
    risks: list[RiskRecord] = field(default_factory=list)
    found: list[bool] = field(default_factory=list)
    """Per labeled defect, in label order."""
    citations: list[CitationCheck] = field(default_factory=list)
    checklist_items: int = 0
    checklist_problems: list[str] = field(default_factory=list)
    level_problem: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)
    """Execution failures, kept apart from review quality."""

    @property
    def reviewed(self) -> bool:
        return self.quality_state is not None and not self.failures

    @property
    def high_risks(self) -> int:
        return sum(1 for risk in self.risks if risk.severity == "high")


def score(result: ItemResult, prepared: Prepared, analysis: Analysis) -> None:
    """Fill `result` from a finished review."""
    review = analysis.result
    evidence = {item.label: item for item in analysis.evidence}
    refs = {
        label: EvidenceRef(label, item.side, item.path, item.start_line, item.end_line)
        for label, item in evidence.items()
    }
    result.quality_state = analysis.quality_state
    result.overall_level = str((review.get("overall_risk") or {}).get("level"))
    result.usage = dict(analysis.usage.as_dict())
    result.risks = [RiskRecord.from_result(risk) for risk in review.get("risks") or ()]
    result.found = [defect_found(d, result.risks, refs) for d in prepared.item.labels.defects]
    for label in cited_labels(review):
        stored = evidence.get(label)
        if stored is None:
            result.citations.append(
                CitationCheck(None, label, "", "the label is not in the review's evidence")
            )
            continue
        tree, expected = (
            (prepared.head, prepared.head_sha)
            if stored.side == "after"
            else (prepared.base, prepared.merge_base_sha)
        )
        eligible = tree.files.get(stored.path)
        problem = citation_problem(
            commit_sha=stored.commit_sha,
            expected_commit_sha=expected,
            content=eligible.content if eligible is not None else None,
            start_line=stored.start_line,
            end_line=stored.end_line,
            excerpt=stored.excerpt,
            excerpt_sha256=stored.excerpt_sha256,
        )
        result.citations.append(CitationCheck(refs[label], label, stored.excerpt, problem))
    checklist = review.get("checklist") or []
    changed = changed_paths(review)
    risk_ids = {risk.id for risk in result.risks}
    result.checklist_items = len(checklist)
    for number, entry in enumerate(checklist, start=1):
        problem = checklist_problem(entry, changed=changed, risk_ids=risk_ids)
        if problem is not None:
            result.checklist_problems.append(f"item {number}: {problem}")
    result.level_problem = level_problem(review)


@dataclass(frozen=True)
class Summary:
    items: int
    recall: Rate
    """Seeded defects found, over the defects of seeded items that were reviewed (SC-002)."""
    safe_with_high: int
    """Safe items whose review reports a high risk (SC-003)."""
    safe_reviews: int
    citation_validity: Rate
    checklist_validity: Rate
    level_consistency: Rate
    injection_recall: Rate
    """Defects found on injection items that were reviewed (SC-007)."""
    execution_failures: int


def summarize(results: Sequence[ItemResult]) -> Summary:
    reviewed = [r for r in results if r.reviewed]

    def recall(kind: str) -> Rate:
        found = [hit for r in reviewed if r.item.labels.kind == kind for hit in r.found]
        return Rate(sum(found), len(found))

    safe = [r for r in reviewed if r.item.labels.kind == "safe"]
    checks = [c for r in reviewed for c in r.citations]
    checklist_total = sum(r.checklist_items for r in reviewed)
    return Summary(
        items=len(results),
        recall=recall("seeded"),
        safe_with_high=sum(1 for r in safe if r.high_risks),
        safe_reviews=len(safe),
        citation_validity=Rate(sum(1 for c in checks if c.problem is None), len(checks)),
        checklist_validity=Rate(
            checklist_total - sum(len(r.checklist_problems) for r in reviewed), checklist_total
        ),
        level_consistency=Rate(sum(1 for r in reviewed if r.level_problem is None), len(reviewed)),
        injection_recall=recall("injection"),
        execution_failures=sum(1 for r in results if r.failures),
    )


def target_checks(summary: Summary) -> list[tuple[str, bool | None]]:
    """Each target and whether it passes; None when nothing was measured (for example, a
    `--limit` run with no safe item)."""
    return [
        ("SC-002 seeded-defect recall", meets(summary.recall, RECALL_TARGET)),
        (
            "SC-003 safe items with a high risk",
            None if not summary.safe_reviews else summary.safe_with_high <= SAFE_HIGH_TARGET,
        ),
        ("SC-005 citation validity", meets(summary.citation_validity, CONSISTENCY_TARGET)),
        ("SC-005 checklist references", meets(summary.checklist_validity, CONSISTENCY_TARGET)),
        ("SC-005 overall-level consistency", meets(summary.level_consistency, CONSISTENCY_TARGET)),
        ("SC-007 injection-item recall", meets(summary.injection_recall, INJECTION_RECALL_TARGET)),
        ("Items with execution failures", summary.execution_failures == 0),
    ]


def exit_code(summary: Summary) -> int:
    """0 when no measured target fails, else 1."""
    failed = any(passed is False for _, passed in target_checks(summary))
    return EXIT_FAIL if failed else EXIT_PASS


# --- Report and audit sheet ---------------------------------------------------------------------

AUDIT_COLUMNS = (
    "item_id",
    "repository",
    "commit_sha",
    "pull_request_title",
    "risk_id",
    "severity",
    "category",
    "basis",
    "risk_title",
    "explanation",
    "suggested_check",
    "cited_evidence",
    "cited_excerpts",
    "correctly_explained",
    "reviewer_notes",
)


def audit_sample(
    results: Sequence[ItemResult],
    *,
    min_risks: int = AUDIT_MIN_RISKS,
    min_reviews: int = AUDIT_MIN_REVIEWS,
    seed: int = AUDIT_SEED,
) -> list[tuple[ItemResult, RiskRecord]]:
    """A reproducible sample of model risks for the SC-004 audit.

    Reviews are visited in a shuffled order, taking one random risk from each per round, until
    the sample has at least `min_risks` risks from at least `min_reviews` reviews, or every risk
    is taken. The sample is returned in item and risk order.
    """
    rng = random.Random(seed)
    pools: list[tuple[int, list[tuple[int, RiskRecord]]]] = []
    for index, result in enumerate(results):
        risks = [(n, risk) for n, risk in enumerate(result.risks) if risk.origin == "model"]
        if result.reviewed and risks:
            rng.shuffle(risks)
            pools.append((index, risks))
    rng.shuffle(pools)
    taken: list[tuple[int, int, RiskRecord]] = []
    reviews: set[int] = set()

    def enough() -> bool:
        return len(taken) >= min_risks and len(reviews) >= min_reviews

    while not enough() and any(risks for _, risks in pools):
        for index, risks in pools:
            if enough():
                break
            if risks:
                position, risk = risks.pop()
                taken.append((index, position, risk))
                reviews.add(index)
    taken.sort(key=lambda entry: (entry[0], entry[1]))
    return [(results[index], risk) for index, _, risk in taken]


def audit_rows(sample: Iterable[tuple[ItemResult, RiskRecord]]) -> list[dict[str, str]]:
    """One row per sampled risk, with its cited excerpts and empty reviewer columns."""
    rows: list[dict[str, str]] = []
    for result, risk in sample:
        by_label = {check.label: check for check in result.citations}
        cited = [by_label[label] for label in risk.evidence_ids if label in by_label]
        excerpts = "\n\n".join(f"[{c.ref}]\n{c.excerpt}" for c in cited if c.ref is not None)
        if len(excerpts) > MAX_CELL_CHARS:
            excerpts = excerpts[:MAX_CELL_CHARS] + "\n[truncated]"
        item = result.item
        rows.append(
            {
                "item_id": item.id,
                "repository": item.repository,
                "commit_sha": item.commit_sha,
                "pull_request_title": item.title,
                "risk_id": risk.id,
                "severity": risk.severity,
                "category": risk.category,
                "basis": risk.basis,
                "risk_title": risk.title,
                "explanation": risk.explanation,
                "suggested_check": risk.suggested_check,
                "cited_evidence": "; ".join(str(c.ref) for c in cited if c.ref is not None),
                "cited_excerpts": excerpts,
                "correctly_explained": "",
                "reviewer_notes": "",
            }
        )
    return rows


@dataclass(frozen=True)
class RunInfo:
    started_at: datetime
    set_path: str
    set_sha256: str
    limit: int | None
    mode: str
    model: str
    thinking_level: str
    skipped: int
    """Items of the other mode (fixture or pinned) in the set, not run."""


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _rate_text(rate: Rate) -> str:
    if rate.value is None:
        return "n/a"
    return f"{rate.value:.1%} ({rate.numerator:g}/{rate.denominator})"


def _verdict(passed: bool | None) -> str:
    return "n/a" if passed is None else ("PASS" if passed else "FAIL")


def _tokens(result: ItemResult, key: str) -> int:
    return int(result.usage.get(key, 0))


def render_report(
    info: RunInfo,
    results: Sequence[ItemResult],
    summary: Summary,
    sample_size: tuple[int, int],
) -> str:
    counts = {kind: sum(1 for r in results if r.item.labels.kind == kind) for kind in KINDS}
    verdicts = dict(target_checks(summary))
    lines = [
        "# Pull request review evaluation",
        "",
        f"- Started: {info.started_at.isoformat(timespec='seconds')}",
        f"- Set: `{info.set_path}` (SHA-256 of the set and its overlays "
        f"`{info.set_sha256[:12]}`)" + (f", limit {info.limit}" if info.limit is not None else ""),
        f"- Items: {len(results)} ({counts['seeded']} seeded, {counts['safe']} safe, "
        f"{counts['injection']} injection); {info.skipped} items of the other mode not run",
        f"- Mode: {info.mode}",
        f"- Review model: `{info.model}` (thinking `{info.thinking_level}`), "
        f"prompt `{PROMPT_VERSION}`",
        "",
        "## Metrics",
        "",
        "Execution failures are excluded from every quality denominator and listed below.",
        "",
        "| Metric | Value | Target | Status |",
        "| --- | --- | --- | --- |",
        f"| SC-002 seeded defects found by a medium or high risk | {_rate_text(summary.recall)} "
        f"| >= {RECALL_TARGET:.0%} | {_verdict(verdicts['SC-002 seeded-defect recall'])} |",
        f"| SC-003 safe reviews with a high risk | {summary.safe_with_high} of "
        f"{summary.safe_reviews} | {SAFE_HIGH_TARGET} "
        f"| {_verdict(verdicts['SC-003 safe items with a high risk'])} |",
        f"| SC-005 citations that resolve to their side's lines "
        f"| {_rate_text(summary.citation_validity)} | {CONSISTENCY_TARGET:.0%} "
        f"| {_verdict(verdicts['SC-005 citation validity'])} |",
        f"| SC-005 checklist items that refer to a changed file or listed risk "
        f"| {_rate_text(summary.checklist_validity)} | {CONSISTENCY_TARGET:.0%} "
        f"| {_verdict(verdicts['SC-005 checklist references'])} |",
        f"| SC-005 overall levels equal to the most severe risk "
        f"| {_rate_text(summary.level_consistency)} | {CONSISTENCY_TARGET:.0%} "
        f"| {_verdict(verdicts['SC-005 overall-level consistency'])} |",
        f"| SC-007 defects found on injection items | {_rate_text(summary.injection_recall)} "
        f"| {INJECTION_RECALL_TARGET:.0%} "
        f"| {_verdict(verdicts['SC-007 injection-item recall'])} |",
        f"| Items with execution failures | {summary.execution_failures} of {summary.items} "
        f"| 0 | {_verdict(verdicts['Items with execution failures'])} |",
        "",
        f"SC-004 audit sheet: {sample_size[0]} risks from {sample_size[1]} reviews "
        f"(at least {AUDIT_MIN_RISKS} from {AUDIT_MIN_REVIEWS} are needed).",
        "",
        *_usage_lines(results),
        "## Per-item results",
        "",
        "| ID | Kind | Level | Risks (H/M/L) | Defects found | Citations valid | Seconds "
        "| Input tokens | Output tokens | Execution failure |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        by_severity = [sum(1 for risk in r.risks if risk.severity == s) for s in SEVERITY_RANK]
        valid = sum(1 for c in r.citations if c.problem is None)
        defects = f"{sum(r.found)}/{len(r.found)}" if r.reviewed and r.found else ""
        lines.append(
            f"| {r.item.id} | {r.item.labels.kind} | {r.overall_level or ''} "
            f"| {'/'.join(map(str, by_severity)) if r.reviewed else ''} | {defects} "
            f"| {f'{valid}/{len(r.citations)}' if r.reviewed else ''} | {r.seconds:.1f} "
            f"| {_tokens(r, 'input_tokens')} | {_tokens(r, 'output_tokens')} "
            f"| {_cell('; '.join(r.failures))} |"
        )
    lines += ["", "## Failures", "", *_failure_sections(results)]
    return "\n".join(lines).rstrip("\n") + "\n"


def _usage_lines(results: Sequence[ItemResult]) -> list[str]:
    timed = [r.seconds for r in results if r.reviewed]
    totals = {
        key: sum(_tokens(r, key) for r in results)
        for key in ("model_calls", "input_tokens", "output_tokens", "thinking_tokens")
    }
    mean = sum(timed) / len(timed) if timed else 0.0
    return [
        f"Model calls: {totals['model_calls']}; tokens: {totals['input_tokens']} input, "
        f"{totals['output_tokens']} output, {totals['thinking_tokens']} thinking. "
        f"Review time: mean {mean:.1f} s, max {max(timed, default=0.0):.1f} s.",
        "",
    ]


def _section(title: str, entries: Sequence[str]) -> list[str]:
    return [f"### {title}", "", *(entries or ["None."]), ""]


def _failure_sections(results: Sequence[ItemResult]) -> list[str]:
    missed: list[str] = []
    for r in results:
        if not r.reviewed:
            continue
        for defect, hit in zip(r.item.labels.defects, r.found, strict=True):
            if hit:
                continue
            cited = {c.label: c.ref for c in r.citations}
            risks = "; ".join(
                f"{risk.id} {risk.severity} {risk.category}: "
                + ", ".join(str(cited.get(label) or label) for label in risk.evidence_ids)
                for risk in r.risks
            )
            missed.append(
                f"- {r.item.id} ({r.item.labels.kind}): {defect} ({defect.category}): "
                f"{_cell(defect.description)} Risks: {_cell(risks) or 'none'}"
            )
    high_on_safe = [
        f"- {r.item.id} {risk.id} {risk.category}: {_cell(risk.title)}"
        for r in results
        if r.reviewed and r.item.labels.kind == "safe"
        for risk in r.risks
        if risk.severity == "high"
    ]
    invalid = [
        f"- {r.item.id} {c.ref or c.label}: {c.problem}"
        for r in results
        if r.reviewed
        for c in r.citations
        if c.problem
    ]
    checklist = [
        f"- {r.item.id} {problem}"
        for r in results
        if r.reviewed
        for problem in r.checklist_problems
    ]
    levels = [f"- {r.item.id}: {r.level_problem}" for r in results if r.level_problem]
    errored = [f"- {r.item.id}: {_cell('; '.join(r.failures))}" for r in results if r.failures]
    return [
        *_section("Missed defects (SC-002 and SC-007)", missed),
        *_section("High risks on safe items (SC-003)", high_on_safe),
        *_section("Invalid citations (SC-005)", invalid),
        *_section("Checklist items without a valid reference (SC-005)", checklist),
        *_section("Overall levels that do not match the risks (SC-005)", levels),
        *_section("Execution failures", errored),
    ]


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return path.name


def _write_outputs(
    out_dir: Path, info: RunInfo, results: Sequence[ItemResult], summary: Summary
) -> tuple[Path, Path]:
    sample = audit_sample(results)
    size = (len(sample), len({id(result) for result, _ in sample}))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = info.started_at.strftime("%Y%m%dT%H%M%SZ")
    report = out_dir / f"review-eval-{stamp}.md"
    sheet = out_dir / f"review-eval-{stamp}-audit.csv"
    report.write_text(render_report(info, results, summary, size), encoding="utf-8")
    with sheet.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        writer.writerows(audit_rows(sample))
    return report, sheet


# --- Running ------------------------------------------------------------------------------------


def _model(settings: Settings, *, fixtures: bool) -> tuple[AnswerModel, str]:
    if fixtures:
        # The fake follows FAKE_REVIEW_MODEL_MODE (default `ok`) and needs no key.
        return FakeAnswerModel(settings), FAKE_MODEL
    if settings.fake_externals:
        raise EvalError(
            "CODEATLAS_FAKE_EXTERNALS is on, so a real run would not reach Gemini. Pass "
            "--fixtures for a smoke test, or set CODEATLAS_FAKE_EXTERNALS=0."
        )
    if not settings.gemini_api_key:
        raise EvalError("Set GEMINI_API_KEY in the environment or .env (evals/README.md).")
    return GeminiAnswerModel(settings), settings.answer_model


def prepare_all(items: Sequence[Item], settings: Settings) -> list[Prepared]:
    """Read every base once and check every item; raises EvalError on the first problem."""
    archives: dict[tuple[str, str], tuple[Archive, bytes]] = {}
    prepared: list[Prepared] = []
    for item in items:
        key = (item.repository, item.commit_sha)
        if key not in archives:
            if item.fixture:
                data = fixture_archive(item.repository[len(FIXTURE_PREFIX) :], item.commit_sha)
            else:
                print(f"Reading {item.repository} at {item.commit_sha[:12]} ...", flush=True)
                data = download_archive(item.repository, item.commit_sha)
            try:
                archives[key] = (read_archive(data), data)
            except (tarfile.TarError, EOFError, OSError) as exc:
                raise EvalError(f"The archive of {item.repository} is unreadable: {exc}") from exc
        archive, data = archives[key]
        try:
            prepared.append(prepare(item, archive, data, settings))
        except ValueError as exc:
            raise EvalError(str(exc)) from exc
        except LimitExceeded as exc:
            raise EvalError(f"{item.id}: the head exceeds {exc.limit_name}") from exc
    return prepared


def review(prepared: Prepared, settings: Settings, model: AnswerModel) -> ItemResult:
    """Run the job's `analyze` on one item and score it; failures are recorded, not raised."""
    result = ItemResult(prepared.item)
    started = time.monotonic()
    try:
        analysis = analyze(
            {"title": prepared.item.title, "body": prepared.item.body},
            prepared.head,
            prepared.base,
            prepared.rename_hints,
            head_sha=prepared.head_sha,
            merge_base_sha=prepared.merge_base_sha,
            settings=settings,
            model=model,
        )
    except JobFailure as exc:
        result.failures.append(f"{exc.code}: {exc.message}")
    except Exception as exc:  # one broken review must not end the run
        result.failures.append(f"{type(exc).__name__}: {exc}")
    else:
        score(result, prepared, analysis)
    result.seconds = time.monotonic() - started
    return result


def _progress(index: int, total: int, result: ItemResult) -> str:
    parts = [f"[{index}/{total}] {result.item.id}"]
    if result.reviewed:
        parts.append(f"{result.quality_state}, level {result.overall_level}")
        parts.append(f"{len(result.risks)} risks")
        if result.found:
            parts.append(f"defects {sum(result.found)}/{len(result.found)}")
        invalid = sum(1 for c in result.citations if c.problem)
        if invalid:
            parts.append(f"{invalid} invalid citations")
    if result.failures:
        parts.append("FAILED: " + "; ".join(result.failures))
    parts.append(f"{result.seconds:.1f}s")
    return "  ".join(parts)


def _print_composition(items: Sequence[Item]) -> None:
    for kind, counts in composition(items).items():
        categories = ", ".join(f"{c} {counts[c]}" for c in CATEGORIES if counts.get(c))
        suffix = f"; defects: {categories}" if categories else ""
        print(f"  {kind}: {counts['items']} items{suffix}")


def run(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    set_path = Path(args.set)
    try:
        all_items = load_items(set_path)
    except (OSError, ValueError) as exc:
        raise EvalError(str(exc)) from exc
    items = select_items(all_items, fixtures=args.fixtures, limit=args.limit)
    if not items:
        which = "fixture" if args.fixtures else "pinned-repository"
        raise EvalError(f"{set_path.name} has no {which} items.")

    settings = get_settings()
    model: AnswerModel | None = None
    model_name = ""
    if not args.check:  # before any download, so a missing key fails fast
        model, model_name = _model(settings, fixtures=args.fixtures)
    prepared = prepare_all(items, settings)
    print(f"Checked {len(prepared)} items: every edit applies and every label is reviewable.")
    _print_composition(items)
    if model is None:
        return EXIT_PASS

    results: list[ItemResult] = []
    for index, ready in enumerate(prepared, start=1):
        result = review(ready, settings, model)
        results.append(result)
        print(_progress(index, len(prepared), result), flush=True)

    summary = summarize(results)
    info = RunInfo(
        started_at=started_at,
        set_path=_display(set_path),
        set_sha256=set_digest(set_path, all_items),
        limit=args.limit,
        mode="fixtures (fake review model)" if args.fixtures else "real review model",
        model=model_name,
        thinking_level=settings.answer_thinking_level,
        skipped=len(all_items) - len(select_items(all_items, fixtures=args.fixtures, limit=None)),
    )
    report, sheet = _write_outputs(Path(args.out), info, results, summary)
    print()
    for name, passed in target_checks(summary):
        print(f"{name}: {_verdict(passed)}")
    print(f"Seeded-defect recall: {_rate_text(summary.recall)}")
    print(f"Injection-item recall: {_rate_text(summary.injection_recall)}")
    print(f"Safe reviews with a high risk: {summary.safe_with_high}/{summary.safe_reviews}")
    print(f"Report: {_display(report)}")
    print(f"Audit sheet: {_display(sheet)}")
    return exit_code(summary)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_review_eval",
        description="Run the pull request review evaluation.",
    )
    parser.add_argument(
        "--set", default=str(DEFAULT_SET), help="evaluation set (default: evals/review_v2.jsonl)"
    )
    parser.add_argument("--limit", type=int, help="review at most N items, in file order")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="offline smoke test: the set's fixture items with the fake review model",
    )
    parser.add_argument(
        "--check", action="store_true", help="validate the selected items only; no model calls"
    )
    parser.add_argument(
        "--out",
        default=str(EVALS_DIR / "out"),
        help="output directory (default: evals/out)",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(logging.WARNING)
    try:
        return run(args)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
