# Evaluations

> **Draft: needs human review before use.** The questions, answerability labels, splits, and
> evidence ranges in `qa_v1.jsonl` were drafted and checked mechanically, but no person has
> reviewed them yet. Until a reviewer has worked through the [review checklist](#review-checklist)
> and recorded the review in the [review log](#review-log), report a number measured with this
> set only with its [review-status label](#review-status-labels). The pull request review set has
> its own [draft notice](#pull-request-review-evaluation).

This directory holds the versioned question set for repository Q&A and the runner that measures
it against the success criteria in `specs/001-repository-qa/spec.md`, the pull request review set
and runner for `specs/003-pr-review/spec.md`
([Pull request review evaluation](#pull-request-review-evaluation)), and the runner for a public
benchmark with labels written by people ([Code Review Bench](#code-review-bench)):

| File | Purpose |
| --- | --- |
| `qa_v1.jsonl` | The question set, version 1 (68 questions over 3 public repositories) |
| `run_qa_eval.py` | The runner: indexes the pinned commits, asks the questions, and writes the report |
| `review_v2.jsonl` | The pull request review set, version 2 (36 items on the same 3 repositories, plus 3 offline fixture items) |
| `review_v1.jsonl` | Version 1, superseded by version 2 after the review recorded in the [review-set log](#review-set-log); kept because its numbers were reported |
| `review_fixtures/` | One overlay per directory: the edits a review item makes to its base commit |
| `run_review_eval.py` | The review runner: builds each pull request from its overlay, reviews it, and writes the report |
| `run_bench_eval.py` | The Code Review Bench runner: reviews the benchmark's Python and TypeScript pull requests, runs its judge, and compares the results with the published tools |
| `out/` | Reports, audit sheets, scratch clones, the archive cache, and the benchmark copy (gitignored; never commit it) |

## The question set

### Record format

`qa_v1.jsonl` has one JSON object per line:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable identifier, `<repository name>-<number>` |
| `repository` | string | Upstream `owner/name` on GitHub |
| `commit_sha` | string | The pinned 40-character commit; every label refers to this commit |
| `question` | string | The question as a user would ask it |
| `answerable` | bool | Whether the repository at that commit contains the answer |
| `split` | string | `tuning` (about 30%) or `heldout` (about 70%) |
| `relevant_evidence` | list | `{path, start_line, end_line}` ranges that contain the answer; empty when `answerable` is false |

Paths are relative to the repository root. Line numbers start at 1 and are inclusive, numbered
the way the indexer numbers them (lines split on `\n`).

Example:

```json
{"id": "tenacity-001", "repository": "jd/tenacity", "commit_sha": "8be01b1daf2010566fd936a5c2214efefa43edff", "question": "Where is the `stop_after_attempt` stop condition implemented, and how does it decide when to stop?", "answerable": true, "split": "heldout", "relevant_evidence": [{"path": "tenacity/stop.py", "start_line": 89, "end_line": 97}]}
```

The set stores questions, paths, and line numbers only. It contains no third-party source.

### Repositories and commits

| Upstream | Language | License | Pinned commit | Commit date | Indexed | Questions |
| --- | --- | --- | --- | --- | --- | --- |
| `jd/tenacity` | Python | Apache-2.0 | `8be01b1daf2010566fd936a5c2214efefa43edff` | 2026-10-01 | 91 files, 7,566 lines | 26 |
| `unjs/ofetch` | TypeScript | MIT | `1dbc37fd1ceab832fc7c90cad81b1091c95ba563` | 2026-07-08 | 32 files, 3,324 lines | 21 |
| `sindresorhus/p-queue` | TypeScript | MIT | `180ab9e25cd10b6f548767d7176076b50d25e188` | 2026-07-22 | 23 files, 8,048 lines | 21 |

The indexed counts come from running the indexer's filters over each commit's archive (they
skip tenacity's `README.rst` link, ofetch's lockfile, and p-queue's `.npmrc`). Each pinned commit
was the head of the default branch when the set was drafted.

How they were chosen:

- **Permissive licenses** (MIT and Apache-2.0), public, and actively maintained.
- **Both supported languages**: one Python library and two TypeScript libraries, so symbol
  extraction is exercised for `.py` and `.ts` files.
- **Small**: each is well under 20,000 lines, so indexing is quick and a reviewer can read
  every labeled range.
- **Code and documentation together**: each has real implementation logic (retry strategies,
  request handling, scheduling and rate limiting) and enough documentation (reStructuredText for
  tenacity, Markdown for the other two) to ask documentation and configuration questions.
- **Few duplicates**: tenacity's `README.rst` is a symbolic link to `doc/source/index.rst`. The
  indexer skips links, so labels point to `doc/source/index.rst`.

`unjs/ofetch` was on its 2.0 alpha line at the pinned commit. The questions describe that commit
only.

### Counts

| Split | Answerable | Unanswerable | Total |
| --- | --- | --- | --- |
| `tuning` | 16 | 3 | 19 |
| `heldout` | 37 | 12 | 49 |
| **Total** | **53** | **15** | **68** |

Each repository has 5 unanswerable questions. The questions mix these kinds:

- where something is implemented (`stop_after_attempt`, the concurrency check);
- how something works (exponential backoff, the priority queue, query merging);
- configuration and tooling (defaults, options, supported versions, CI, project commands);
- documentation-only answers (proxies, generators, Jest fake timers);
- error handling (`RetryError` and `reraise`, `FetchError`, timeouts, constructor validation).

Unanswerable questions ask about plausible features that the repository does not have, such as
a circuit breaker in tenacity, response caching in ofetch, or cron scheduling in p-queue.

### How the questions were written and checked

1. Each repository was read at the pinned commit from a shallow clone in `out/`.
2. Each answerable question was written from a specific passage. Its range covers the smallest
   complete unit that answers it (a function, a class, an options block, or a documentation
   section), not a whole file. A question gets two or three ranges when the answer needs both
   code and documentation, or two places in the code; each range counts equally in Recall@5.
3. Every range was checked mechanically against the clone: the file is a regular file (not a
   link), the range lies inside it, and the identifiers and sentences that the answer depends on
   appear inside the range.
4. Every referenced file was compared byte for byte with `raw.githubusercontent.com` at the
   pinned commit, and each pinned commit was confirmed to exist in the upstream repository.
5. For unanswerable questions, each repository (code, documentation, changelog, and examples)
   was searched for the key terms to confirm that nothing answers them.
6. `tests/unit/test_eval_metrics.py` validates the file's format and its counts.

To read a range yourself, clone the repository at the pinned commit and print the lines (from
`backend/`):

```bash
git clone --filter=blob:none https://github.com/jd/tenacity.git evals/out/repos/tenacity
git -C evals/out/repos/tenacity checkout 8be01b1daf2010566fd936a5c2214efefa43edff
sed -n '89,97p' evals/out/repos/tenacity/tenacity/stop.py
```

### Review checklist

A reviewer checks every record before the set is used:

- [ ] The answerable question has one defensible answer at the pinned commit, and the labeled
      ranges contain it.
- [ ] No equally good evidence is missing from the labels. If another place answers the question
      just as well, add it as a range or narrow the question.
- [ ] Each unanswerable question really has no answer anywhere in the repository, including the
      documentation, changelog, and examples.
- [ ] The wording reads like a real user's question. Many questions name an identifier (for
      example `wait_chain`), which helps symbol retrieval; judge whether the balance is fair.
- [ ] The splits stay as they are once tuning starts. Never tune against held-out results.

### Review log

| Date | Reviewer | Records reviewed | Changes |
| --- | --- | --- | --- |
| | | | |

Once the set has been reviewed and a number from it has been reported, do not edit
`qa_v1.jsonl`. Put changes in a new version (`qa_v2.jsonl`). Every report records the SHA-256 of
the set file it used.

## Running the evaluation

The runner uses the real GitHub App, Gemini, and Voyage AI, configured the same way as the
application (see `specs/001-repository-qa/quickstart.md`).

### 1. Fork the repositories and install the App

The development GitHub App can only read repositories it is installed on, so use forks:

```bash
gh repo fork jd/tenacity --clone=false
gh repo fork unjs/ofetch --clone=false
gh repo fork sindresorhus/p-queue --clone=false
```

Then install the development App on the three forks: on GitHub, open Settings, Applications,
Installed GitHub Apps, the App's Configure page, and add the forks under Repository access.
If the App cannot see a fork, the runner stops and names the repository.

### 2. Point a branch at each pinned commit

Indexing reads the head of a branch, so each fork needs a branch at the pinned commit, named
`codeatlas-eval-` plus the first 12 characters of the SHA. Replace `<you>` with your GitHub login:

```bash
gh api -X POST repos/<you>/tenacity/git/refs \
  -f ref=refs/heads/codeatlas-eval-8be01b1daf20 -f sha=8be01b1daf2010566fd936a5c2214efefa43edff
gh api -X POST repos/<you>/ofetch/git/refs \
  -f ref=refs/heads/codeatlas-eval-1dbc37fd1cea -f sha=1dbc37fd1ceab832fc7c90cad81b1091c95ba563
gh api -X POST repos/<you>/p-queue/git/refs \
  -f ref=refs/heads/codeatlas-eval-180ab9e25cd1 -f sha=180ab9e25cd10b6f548767d7176076b50d25e188
```

If GitHub reports that a commit does not exist, sync the fork first (`gh repo sync <you>/<name>`).
After indexing, the runner checks that the indexed commit equals the pinned one.

### 3. Configure `.env`

Use a separate database for evaluations: the runner processes every queued job in the database
it points at, and its data stays there so that later runs reuse the indexed versions.

```bash
docker compose exec db createdb -U codeatlas codeatlas_eval
cd backend
DATABASE_URL=postgresql+psycopg://codeatlas:codeatlas@localhost:5432/codeatlas_eval \
  uv run alembic upgrade head
```

Set these keys in `.env` (at the repository root or in `backend/`), or in the environment:

| Key | Value |
| --- | --- |
| `CODEATLAS_FAKE_EXTERNALS` | `0` |
| `DATABASE_URL` | The evaluation database, for example `postgresql+psycopg://codeatlas:codeatlas@localhost:5432/codeatlas_eval` |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` | The development App; it reads the forks with installation tokens |
| `TOKEN_ENCRYPTION_KEY` | A Fernet key, as for the application |
| `GEMINI_API_KEY` | A paid-tier key (not needed with `--retrieval-only`) |
| `ANSWER_MODEL`, `ANSWER_THINKING_LEVEL` | Optional; the settings under test |
| `VOYAGE_API_KEY`, `EMBEDDING_MODEL` | Documentation embeddings |
| `EVAL_GITHUB_TOKEN` | A GitHub token that can read the forks, such as the output of `gh auth token`; used only for the user-side access check that connecting a repository performs |

The runner works as a dedicated evaluation user (`codeatlas-eval`) with its own workspace. It
stores `EVAL_GITHUB_TOKEN` for that user, encrypted, in the evaluation database. It also lifts
the daily question allowance for its own process, because a run asks more questions than the
default allowance.

### 4. Run

From `backend/`:

```bash
# While tuning settings or prompts:
uv run python -m evals.run_qa_eval --set evals/qa_v1.jsonl --split tuning --fork-owner <you>
# For reported numbers, once per configuration:
uv run python -m evals.run_qa_eval --set evals/qa_v1.jsonl --split heldout --fork-owner <you>
```

| Option | Meaning |
| --- | --- |
| `--set FILE` | The question set (required) |
| `--split tuning\|heldout\|all` | Which questions to ask (default `heldout`) |
| `--fork-owner LOGIN` | Read `LOGIN/<name>` instead of each upstream `owner/<name>` |
| `--retrieval-only` | Measure Recall@5 only; no answer-model calls |
| `--limit N` | Ask at most N questions, in file order |
| `--fixtures` | Smoke test against the fake GitHub fixtures (see below) |
| `--out DIR` | Output directory (default `evals/out/`) |

For each pinned commit, the runner connects the fork (or re-indexes it if the evaluation
workspace already has it) through the normal domain functions, then runs the job worker in its
own process until the indexing job ends. For each question it runs evidence assembly directly
for Recall@5, then submits the question and runs the worker until the answer job ends, exactly as
for a user. A held-out run makes about one model call per question, and two when an answer needs
a repair.

### Smoke test in fake mode

`--fixtures` runs the same flow against the fake GitHub and the fake answer model and embedder,
using the fixture repository `octo-org/sample-app`. It needs no keys. Save a small question file,
for example `evals/out/fixture_smoke.jsonl` (the commit is the fake `sample-app` head):

```json
{"id": "smoke-001", "repository": "octo-org/sample-app", "commit_sha": "277e76a794610aeab82e7e482ae4d9db19d84029", "question": "Where are repository permissions checked?", "answerable": true, "split": "heldout", "relevant_evidence": [{"path": "app/auth/access.py", "start_line": 26, "end_line": 33}]}
{"id": "smoke-002", "repository": "octo-org/sample-app", "commit_sha": "277e76a794610aeab82e7e482ae4d9db19d84029", "question": "What does the README say about setup?", "answerable": true, "split": "heldout", "relevant_evidence": [{"path": "README.md", "start_line": 6, "end_line": 11}]}
{"id": "smoke-003", "repository": "octo-org/sample-app", "commit_sha": "277e76a794610aeab82e7e482ae4d9db19d84029", "question": "Which payment provider does this use?", "answerable": false, "split": "heldout", "relevant_evidence": []}
```

Then, from `backend/`:

```bash
docker compose exec db createdb -U codeatlas codeatlas_eval_smoke
export CODEATLAS_ENV=development CODEATLAS_FAKE_EXTERNALS=1
export DATABASE_URL=postgresql+psycopg://codeatlas:codeatlas@localhost:5432/codeatlas_eval_smoke
export TOKEN_ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uv run alembic upgrade head
uv run python -m evals.run_qa_eval --fixtures --set evals/out/fixture_smoke.jsonl --split all
```

The fake answer model answers every question that has any evidence, so the abstention rate in
fake mode says nothing about quality. The smoke test checks the plumbing, not the numbers.

## Metrics

Execution failures are counted separately and excluded from every quality denominator. Every
metric is reported with its denominator.

| Metric | Criterion | Target | Definition |
| --- | --- | --- | --- |
| Recall@5 | SC-004 | at least 0.80 | For each answerable question, evidence assembly returns ranked items `E1` to `En`. A labeled range is a hit when any of `E1` to `E5` in the same file shares at least one line with it. A question's recall is hits divided by labeled ranges; the reported value is the mean over answerable questions whose retrieval ran. |
| Citation validity | SC-003 | 100% | For every published answer, each citation shown to users (built from the stored evidence) must name the answer's commit, a file in the indexed version, and a line range inside that file, and its excerpt must equal those stored lines exactly. The value is valid citations divided by displayed citations. |
| Abstention rate | SC-006 | at least 80% on held-out | Unanswerable questions whose result is `insufficient_evidence`, divided by unanswerable questions with a published answer. A pass or fail is shown only for `--split heldout`. |
| Answered when answerable | none | report only | Answerable questions whose result is `answered`, divided by answerable questions with a published answer. It guards against a system that scores well on abstention by abstaining on everything. |
| Execution failures | none | 0 | Questions where retrieval raised an error, the submission was refused, or the answer job ended `failed`, `canceled`, or timed out (for example `provider_unavailable`, `citation_validation_failed`, or `model_refused`). Each is listed with its code. |

## Outputs

Each run writes two files to `evals/out/`, named with the UTC start time:

- `qa-eval-<time>.md`: the run settings (set file and its SHA-256, split, models, prompt and
  index versions), the indexed commits, the metrics table with targets, a row per question, and
  the failures: retrieval misses with the missed ranges and the top 5 retrieved items, invalid
  citations, unanswerable questions that were answered, answerable questions that abstained,
  and execution failures.
- `qa-eval-<time>-audit.csv`: the human audit sheet for SC-005, with one row per claim of every
  published answer: the question, the answer summary and gaps, the claim and its kind, and each
  cited excerpt with its path and lines.

### Human audit (SC-005)

Open the audit sheet from a held-out run in a spreadsheet. For each claim, read the cited
excerpts and fill in:

- `supported`: `yes` if the excerpts state or directly show the claim, `partial` if they support
  only part of it, `no` otherwise. Judge inference claims by whether they follow from the cited
  evidence.
- `reviewer_notes`: anything worth recording, such as a misleading but technically true claim.

SC-005 is met when at least 90% of the reviewed claims are `yes`. Count `partial` as not
supported, and record the sample, the reviewer, and the result next to the run's report.

The reports quote source excerpts from third-party repositories, so keep them in `evals/out/`,
which is gitignored.

## Pull request review evaluation

> **Reviewed by a model, not yet by a person.** The pull requests, overlays, and labels were
> drafted and checked mechanically (`--check`). Codex then applied the
> [review-set checklist](#review-set-checklist) to every item of `review_v1.jsonl`, and
> `review_v2.jsonl` applies its findings (see the [review-set log](#review-set-log)). Label
> numbers measured with it as coming from a set without a full human review
> ([review-status labels](#review-status-labels)), until a person has worked through the
> checklist and recorded that review in the log.

The review set measures `specs/003-pr-review/spec.md` SC-002, SC-003, SC-005, and SC-007, and
samples risks for the SC-004 audit (research R14). Each item is a small pull request made by
applying an overlay of edits to a pinned commit of one of the repositories above. The runner
calls the review job's `analyze` function directly, with the two commit trees, so no GitHub App,
database, or queue is involved.

### Review set record format

`review_v1.jsonl` has one JSON object per line:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable identifier: `<repository name>-s<NN>` (seeded defect), `-safe<NN>` (safe change), or `-i<NN>` (injection variant) |
| `repository` | string | Upstream `owner/name`, one of the pinned repositories of `qa_v1.jsonl`; or `fixture:<name>` for a fixture repository under `backend/tests/fixtures/repos/` |
| `commit_sha` | string | The pinned commit, which is the pull request's base (merge base). For a fixture item, the fake GitHub's `initial` commit of that repository; its tree is read from disk |
| `title`, `body` | string | The pull request title and description sent to the model (`body` may be empty) |
| `overlay` | string | The directory under `review_fixtures/` that holds the item's `overlay.json`. An injection variant whose code equals its seeded item's uses that item's overlay |
| `labels` | object | What the review should find (below) |

Labels take one of three forms:

- `{"kind": "seeded", "defects": [...]}`: a pull request with at least one planted defect.
- `{"kind": "safe"}`: a safe change that includes tests; its review must not report a high risk.
- `{"kind": "injection", "variant_of": "<seeded id>", "defects": [...]}`: a seeded item's change
  with instructions to the model in the title, the description, or a code comment.

Each defect is `{path, side, start_line, end_line, category, description}`:

| Field | Meaning |
| --- | --- |
| `path` | The file on that side: the head path for `after`, the base path for `before` |
| `side` | `after` for lines the change adds (head line numbers, after the overlay), `before` for lines it removes (base line numbers) |
| `start_line`, `end_line` | Inclusive, from 1, numbered as the indexer numbers them (lines split on `\n`) |
| `category` | A review risk category: `correctness`, `security`, `data_and_migrations`, `compatibility`, `performance`, `dependencies`, `tests`, or `other` |
| `description` | What is wrong, for reviewers of the set and for the report's list of missed defects. It is never sent to the model |

A range must hold at least one line that the change adds (`after`) or removes (`before`), in a
file that the review takes under the default limits. The runner checks this before any model
call.

Example (one line in the file):

```json
{"id": "tenacity-s01", "repository": "jd/tenacity", "commit_sha": "8be01b1daf2010566fd936a5c2214efefa43edff", "title": "Simplify strategy selection in wait_chain", "body": "Replaces the nested `min`/`max` ...", "overlay": "tenacity-s01", "labels": {"kind": "seeded", "defects": [{"path": "tenacity/wait.py", "side": "after", "start_line": 134, "end_line": 135, "category": "correctness", "description": "attempt_number starts at 1, so the new index is off by one ..."}]}}
```

### Overlays

`review_fixtures/<overlay>/overlay.json` describes the change as small edits to the base tree:

| Key | Meaning |
| --- | --- |
| `edits` | A list of `{path, find, replace}`. `find` must occur exactly once in the file's current text, and that occurrence is replaced by `replace` (which may be empty, to delete) |
| `add` | `{path: text}` for new files written for the evaluation |
| `remove` | Paths of base files to delete |
| `rename` | `{old path: new path}`; also passed to the review as rename hints, as GitHub's comparison would report them |

Every key is optional, but an overlay must change something. They apply in this order: renames,
then edits in list order (each sees the text left by the previous one, at the renamed path), then
added files, then removals. A text value is a string or a list of lines joined with newlines; in
`add`, every line of the list ends with a newline. Links and other non-regular members of the
base archive are kept as they are.

Example, `review_fixtures/tenacity-s01/overlay.json`:

```json
{
  "edits": [
    {
      "path": "tenacity/wait.py",
      "find": [
        "        wait_func_no = min(max(retry_state.attempt_number, 1), len(self.strategies))",
        "        wait_func = self.strategies[wait_func_no - 1]",
        "        return wait_func(retry_state=retry_state)"
      ],
      "replace": [
        "        # The last strategy is reused once the chain is exhausted.",
        "        index = min(retry_state.attempt_number, len(self.strategies) - 1)",
        "        return self.strategies[index](retry_state=retry_state)"
      ]
    }
  ]
}
```

Why this format:

- **Upstream code stays out of this repository.** An overlay holds only the lines it changes, with
  the least surrounding text that makes `find` unique, never a copy of an upstream file. Files in
  `add` are written for the evaluation.
- **Edits cannot land somewhere unintended.** Matching exactly once makes a stale or ambiguous edit
  a setup error instead of a silently different pull request.
- **Overlays live in their own files**, not inline in the JSON Lines, so multi-line edits stay
  readable as lists of lines, and one overlay can serve a seeded item and its injection
  variants.

### Composition

All items on the pinned repositories:

| Kind | Items | Labeled defects by category |
| --- | --- | --- |
| `seeded` | 25 (tenacity 8, ofetch 10, p-queue 7) | correctness 7, security 3, data_and_migrations 2, compatibility 3, performance 4, dependencies 2, tests 3, other 2 (26 defects: `p-queue-s06` has two) |
| `safe` | 6 (2 per repository) | none; every safe change adds or extends tests |
| `injection` | 6 (2 per repository) | variants of `tenacity-s03`, `tenacity-s05`, `ofetch-s03`, `ofetch-s07`, `p-queue-s02`, and `p-queue-s03`; the instructions are in the title of 1, the description of 4, and a code comment of 2 |

Seeded defects sit on both sides: most are added lines (`after`), and four are removed lines
(`before`): a deleted cycle guard, deleted error properties, a deleted listener cleanup, and a
deleted regression test. Three seeded items change more than one file. Titles and descriptions
read like the author's intent and do not point at the defect, and some make a false claim (for
example, "no behaviour change").

#### Fixture items

Three more items use `fixture:review-app`, the fixture repository of this repository's tests, so
`--fixtures` runs offline: `review-app-s01` removes the role check (a `before` defect),
`review-app-safe01` only adds a function and a new test file, and `review-app-i01` is the
injection variant, with instructions in the title, the description, and a code comment. The fake
review model reports one `high` risk citing the first removed lines when there are any, and one
`low` risk otherwise, so these items meet every target when the plumbing works. Fake-mode
numbers say nothing about review quality, and real runs leave fixture items out.

### How the items were written and checked

1. Each base was read from its commit archive at the pinned commit.
2. Each seeded pull request is a small, plausible change with a planted defect that a careful
   reviewer would ask to fix. Safe pull requests are additive or behavior-preserving, with tests.
3. Labeled ranges were located from the changed text itself, not typed by hand.
4. `--check` builds every item as a real run does and confirms that every edit matches exactly
   once, that every labeled range lies inside its file and holds an added or removed line, and
   that the file is among the reviewed files under the default review limits.
5. `tests/unit/test_review_eval_metrics.py` validates the format, the composition, that every
   base is a pinned commit of `qa_v1.jsonl`, and that every overlay directory is used.

To read an item's change yourself, run `--check` (which downloads the three archives once into
`out/cache/`) and apply its overlay by hand, or read the overlay next to the upstream file at the
pinned commit.

### Review-set checklist

A reviewer checks every item before the set is used:

- [ ] The seeded defect is real: a careful reviewer would ask for a change, for the reason in its
      `description`, at medium or high severity.
- [ ] The labeled range and side are where a reviewer would point: `after` for new code, `before`
      for removed code.
- [ ] The change has no other unlabeled defect of medium or high severity. A safe item has no
      defect at all, and its tests would pass at the head.
- [ ] The title and description read like a real pull request and do not give the defect away
      (injection instructions aside).
- [ ] An injection variant carries exactly the defect of its seeded item, and its instructions
      are aimed at the model.
- [ ] The category is defensible.

### Review-set log

| Date | Reviewer | Items reviewed | Changes |
| --- | --- | --- | --- |
| 2026-10-07 | Codex (automated model review, read-only; not a human review) | All 40 items of `review_v1.jsonl` | 33 ok, 6 fix, 1 drop, applied in `review_v2.jsonl`: ofetch-s04 relabeled as a compatibility defect (wildcard CORS and Request credentials) instead of a credential leak; ofetch-s07 and ofetch-i02 describe the stale `pnpm-lock.yaml`, which certainly fails CI, instead of an unverified runtime failure; p-queue-s03 and p-queue-i02 no longer claim a general O(n log n) cost on every enqueue; p-queue-s06 no longer claims a wait forever with no task running; ofetch-s08 dropped, because a missing rejection assertion is a low-severity test gap |

Once a number from a set version has been reported, do not edit that version or its overlays.
Put changes in a new version, with new overlay directories for any overlay that changes.
`review_v2.jsonl` changes labels only, so it reuses the overlays of `review_v1.jsonl`. Every report
records a SHA-256 over the set file and its overlays.

### Running the review evaluation

From `backend/`:

```bash
# Offline smoke test: the fixture items with the fake review model (no keys, no network).
uv run python -m evals.run_review_eval --fixtures
# Validate the set: build every pinned-repository item and check its labels; no model calls.
uv run python -m evals.run_review_eval --check
# Real run with Gemini, for reported numbers.
uv run python -m evals.run_review_eval
```

| Option | Meaning |
| --- | --- |
| `--set FILE` | The review set (default `evals/review_v2.jsonl`); overlays are read from `review_fixtures/` next to it |
| `--limit N` | Review at most N items, in file order |
| `--fixtures` | Run the set's `fixture:` items with the fake review model, instead of the pinned-repository items with Gemini |
| `--check` | Build and check the selected items only, with no model calls |
| `--out DIR` | Output directory (default `evals/out/`) |

A real run needs `GEMINI_API_KEY` and `CODEATLAS_FAKE_EXTERNALS` unset or `0`, in the environment
or `.env`. `ANSWER_MODEL`, `ANSWER_THINKING_LEVEL`, and the `REVIEW_MAX_*` limits are read as the
application reads them. It makes one model call per item, or two when a review needs a repair:
37 to 74 calls, about $1 to $2 at the typical review cost in research R7.

For each item, the runner:

1. downloads the base archive once from `https://codeload.github.com/{full_name}/tar.gz/{sha}`
   into `out/cache/` (gitignored), or packs the fixture repository from disk;
2. applies the overlay and writes the head as a new archive in memory;
3. reads both archives with the job's `read_tree`, dropping base files that are unchanged at the
   head, as the job does;
4. checks the overlay and labels (a problem stops the run before the first model call);
5. calls `analyze` with the title and description, the two trees, the overlay's renames, a
   synthetic but stable head SHA (`sha1("review-eval:<id>")`), and the pinned commit as the merge
   base;
6. scores the review against the labels.

Exit codes: `0` when no measured target fails, `1` when any target fails, and `2` on a setup
error (an invalid set, an edit that does not apply, an unreachable label, a failed download, or a
missing key). A target with nothing to measure, such as SC-003 in a `--limit` run without a safe
item, shows `n/a` and does not fail.

### Review metrics

Execution failures are counted separately and excluded from every quality denominator.

| Metric | Criterion | Target | Definition |
| --- | --- | --- | --- |
| Seeded-defect recall | SC-002 | at least 70% | A labeled defect of a `seeded` item is found when a `medium` or `high` risk cites an evidence item on the labeled side, in the labeled path, whose line range shares at least one line with the labeled range. The value is found defects over the labeled defects of reviewed seeded items. |
| High risks on safe items | SC-003 | 0 | Safe items whose review reports at least one `high` risk, rule-based risks included. |
| Citation validity | SC-005 | 100% | Every label a review shows (summary points, risks, candidate tests, and new test cases), once per review: it is one of the review's evidence items; its commit is its side's commit (the head for `after`, the merge base for `before`); its file exists on that side; its line range lies inside the file; and its excerpt equals those lines and matches its checksum. |
| Checklist references | SC-005 | 100% | Each checklist item names a changed file (a coverage path or previous path) or a listed risk, and every risk it names is listed. |
| Overall-level consistency | SC-005 | 100% | The overall level equals the most severe listed risk (`none` without risks), and `partial` is set exactly when a changed file hit the review limits. |
| Injection-item recall | SC-007 | 100% | As seeded-defect recall, over `injection` items. Citation validity covers "cite only gathered evidence". |
| Execution failures | none | 0 | Items where `analyze` raised, for example `provider_unavailable`, `model_refused`, or `review_validation_failed`. Each is listed with its code. |

### Review outputs

Each run writes two files to `evals/out/`, named with the UTC start time:

- `review-eval-<time>.md`: the run settings (the set and its SHA-256, the mode, the model, and
  the prompt version), the metrics table with targets, the size of the audit sample, total model
  calls and tokens, review durations, a row per item (kind, overall level, risks by severity,
  defects found, valid citations, seconds, and input and output tokens), and the failures:
  missed defects with their descriptions and what the risks cited, high risks on safe items,
  invalid citations, checklist items without a valid reference, mismatched overall levels, and
  execution failures.
- `review-eval-<time>-audit.csv`: the SC-004 audit sheet.

#### Human audit (SC-004)

The audit sheet samples at least 30 model risks from at least 10 reviews, reproducibly: the
reviews are visited in a fixed shuffled order, taking one random risk from each per round, until
both minimums are met (or every risk is taken, in a short run). Each row holds the item, the
pull request title, the risk's severity, category, basis, title, explanation, and suggested
check, and every cited excerpt with its side, path, and lines. For each row, read the cited
excerpts and fill in:

- `correctly_explained`: `yes` if the excerpts show what the risk explains, `partial` if they
  support only part of it, `no` otherwise. Judge a `possible` risk by whether it follows from the
  cited lines.
- `reviewer_notes`: anything worth recording.

SC-004 is met when at least 80% of the sampled risks are `yes`. Count `partial` as not correct,
and record the sample, the reviewer, and the result next to the run's report. The reports and
the sheet quote third-party source, so keep them in `evals/out/`.

## Review-status labels

Every number that a report gives from the project's own sets carries the label of its set, until a
person has reviewed the set and recorded it in the set's log (004 FR-028):

| Set | Label |
| --- | --- |
| `qa_v1.jsonl` | "measured on a set no person has reviewed yet" |
| `review_v2.jsonl` | "measured on a set reviewed only by a model" |

The human audits (001 SC-005 for answers, 003 SC-004 for review risks) are listed as deferred in
any report that gives these numbers. Code Review Bench needs no such label: its golden comments
were written and verified by people. Its numbers name the judge model instead.

## Code Review Bench

`run_bench_eval.py` measures CodeAtlas's reviews on a public benchmark whose expected findings
were written and verified by people (004 FR-027, research R16): the Python and TypeScript pull
requests of the offline set of Code Review Bench (`withmartian/code-review-benchmark`, MIT
license). The benchmark scores a review with its own judge, a model that decides whether each
candidate issue matches each expected finding (a "golden comment"), and publishes the results of
other review tools judged the same way.

| Golden-comments file | Repository | Language | Pull requests | Golden comments |
| --- | --- | --- | --- | --- |
| `offline/golden_comments/sentry.json` | `getsentry/sentry` (6) and its fork `ai-code-review-evaluation/sentry-greptile` (4) | Python | 10 | 36 |
| `offline/golden_comments/cal_dot_com.json` | `calcom/cal.com`, which now redirects to `calcom/cal.diy` | TypeScript | 10 | 41 |

The benchmark's Go, Ruby, and Java pull requests are left out: CodeAtlas extracts declarations
only from Python and TypeScript.

### Pinned commit

Pass the benchmark commit with `--bench-commit`; every report records it. The commit read while
planning is `e616e849755441da38f18bf3adba2c9583b03803`. The runner checks that the selected pull
requests are in the benchmark's `results/benchmark_data.json` with the same golden comments as
the golden-comments files, before any model call. Before moving to a newer commit, check that its
golden comments, `benchmark_data.json`, and the step scripts keep the structures described here.

### Keys

Set these in the environment or in `.env` (at the repository root or in `backend/`):

| Key | Needed for |
| --- | --- |
| `GEMINI_API_KEY` | The reviews, with `CODEATLAS_FAKE_EXTERNALS` unset or `0`. `ANSWER_MODEL` and `ANSWER_THINKING_LEVEL` are read as the application reads them |
| `GITHUB_TOKEN` | Optional, and read-only (for example `gh auth token`): it only raises GitHub's rate limit. The runner makes two REST requests per pull request; without a token GitHub allows 60 an hour |
| `ANTHROPIC_API_KEY` | The default judge, Claude Opus 4.5 |
| `OPENAI_API_KEY` | The fallback judge, GPT-5.2 (`--judge-model gpt-5.2`) |

The benchmark's steps read their judge from `MARTIAN_API_KEY`, `MARTIAN_BASE_URL`, and
`MARTIAN_MODEL`. The runner sets the three from the judge's table below and starts the steps with
no other credential from its environment: they get only `PATH`, `HOME`, the locale, proxy and
certificate settings, and the `UV_*` and `XDG_*` variables. The steps also read `offline/.env` in
the benchmark copy, but the runner's values take precedence.

### Judges

Use the judges in this order, and check each provider's model deprecation page before a run
([Anthropic](https://docs.claude.com/en/docs/about-claude/model-deprecations),
[OpenAI](https://platform.openai.com/docs/deprecations)), because a retired judge makes the
published results impossible to reproduce. Run the evaluation early either way.

| Order | `--judge-model` | Endpoint (`MARTIAN_BASE_URL`) | Published results read from |
| --- | --- | --- | --- |
| 1 (default) | `claude-opus-4-5-20251101`, the default judge of the benchmark's dashboard | `https://api.anthropic.com/v1/` (Anthropic's OpenAI-compatible endpoint) | `offline/results/anthropic_claude-opus-4-5-20251101/` |
| 2 | `gpt-5.2` | `https://api.openai.com/v1` | `offline/results/openai_gpt-5.2/` |

Claude Sonnet 4.5, the benchmark's third judge, was reported deprecated on 2026-09-30 and
retiring on 2026-11-30, so the runner does not offer it. If neither judge is served, the
evaluation report re-judges the compared tools' published candidates with a current model and
says so (FR-027); the runner does not do that. Such a model may reject the benchmark client's
`temperature=0`, and removing it must be recorded in the report.

### Running

From `backend/`. The first command checks the setup on two pull requests without the judge:

```bash
# Reviews only, two pull requests, no judge calls (Gemini key only).
uv run python -m evals.run_bench_eval --bench-commit e616e849755441da38f18bf3adba2c9583b03803 \
  --skip-judge --limit 2
# The full run with the default judge.
uv run python -m evals.run_bench_eval --bench-commit e616e849755441da38f18bf3adba2c9583b03803
# The fallback judge, if Claude Opus 4.5 is no longer served.
uv run python -m evals.run_bench_eval --bench-commit e616e849755441da38f18bf3adba2c9583b03803 \
  --judge-model gpt-5.2
```

| Option | Meaning |
| --- | --- |
| `--bench-commit SHA` | The benchmark commit, a full 40-character SHA (required) |
| `--out DIR` | Where the benchmark copy and the reports go (default `evals/out/bench`) |
| `--limit N` | Review at most N pull requests, in file order (Sentry first) |
| `--judge-model MODEL` | `claude-opus-4-5-20251101` (default) or `gpt-5.2` |
| `--exclude-flagged` | Skip the pull requests that the benchmark flags with a data warning (below) |
| `--skip-judge` | Review and export only; the steps 2.5 and 3 do not run |

The runner:

1. downloads the benchmark at the commit from codeload into `--out` once, and reads both
   golden-comments files, keeping every item by its URL (`original_url` and `az_comment` too);
2. resolves each pull request's base and head commits (`GET /repos/{owner}/{repo}/pulls/{n}`) and
   their merge base (`GET /repos/{base repository}/compare/{base}...{head}`), following redirects;
3. downloads the head and merge-base archives into `evals/out/cache/` with the review runner's
   `download_archive`, all of them before the first model call;
4. reads both archives with the review job's `read_tree` under the raised limits below, and calls
   the job's `analyze` with GitHub's rename hints, the real model, and the pull request's title
   and description from GitHub, as `run_review_eval.py` does (the benchmark's step 0 copied those
   into the pull requests that the published tools reviewed; `pr_title` is a summary, not the
   title);
5. writes the reviews into the benchmark copy for the tool `codeatlas` (below);
6. unless `--skip-judge`, runs the benchmark's deduplication (step 2.5) and judge (step 3):
   `uv run --directory <copy>/offline python -m code_review_benchmark.<step> --tool codeatlas
   --force`, which also creates the benchmark's own environment in `offline/.venv` on the first
   run (`uv` must be on `PATH`);
7. compares the results and writes the report.

Exit codes: `0` when the run completes, including when some reviews failed (each is listed), and
`2` on a setup error: an invalid commit, a missing key, a failed download or GitHub lookup,
golden comments that differ from `benchmark_data.json`, or a failed judge step. When a judge step
fails after the reviews, the report is still written, with the failure.

### Raised limits

Both repositories exceed the repository limits of 001 FR-009 (a 2025 Sentry head has about 17,000
eligible files, 2.8 million lines, and 105 MB of text). The runner raises them for its own process
only, on a copy of the settings, and every report records the values; the pilot keeps the
defaults and would refuse these repositories.

| Setting | Default | Benchmark runs |
| --- | --- | --- |
| `max_files_per_snapshot` | 5,000 | 50,000 |
| `max_source_lines_per_snapshot` | 100,000 | 10,000,000 |
| `max_expanded_bytes` | 100 MiB | 1 GiB |
| `max_archive_members` | 100,000 | 200,000 |
| `max_archive_bytes` | 1 GiB | 2 GiB |

The per-file limit (`max_file_bytes`) and the review limits (`REVIEW_MAX_*`) keep the application's
values, so a large pull request is reviewed in part, as it would be in the pilot, and the report
marks it partial. A review that still exceeds a limit is recorded as a failure.

### Results directories

The runner writes into its copy of the benchmark, `evals/out/bench/code-review-benchmark-<commit>/`,
in the places the benchmark's steps read:

| Path under `offline/results/` | Contents |
| --- | --- |
| `benchmark_data.json` | One review entry per reviewed pull request for the tool `codeatlas`, keyed by the golden comment's URL, with one review comment per risk; step 3 iterates these entries |
| `<judge>/candidates.json` | One candidate per risk for `codeatlas`, keyed by golden URL; `<judge>` is `MARTIAN_MODEL` with `/` replaced by `_`, the directory the steps use (for example `claude-opus-4-5-20251101/`) |
| `<judge>/dedup_groups.json`, `<judge>/evaluations.json` | Written by steps 2.5 and 3 for `codeatlas` |
| `anthropic_claude-opus-4-5-20251101/`, `openai_gpt-5.2/` | The published results, read only |

Everything is keyed by the golden comment's URL, not by the title (9 of the 20 titles differ from
the upstream or fork titles). Each run first removes the earlier `codeatlas` entries, and the
steps run with `--force`, so the judge sees exactly the current run; every other tool's data is
left as published. Step 3 runs without `--dedup-groups`, because it finds `dedup_groups.json` in
its directory itself, and the steps run with `--directory`, because `offline/` has no build system
(so `--project` fails) and the steps find `results/` from the working directory.

### Candidates

The judge reads only a candidate's text. Each CodeAtlas risk becomes one candidate: its title, the
file and line range of each cited evidence item (or a rule risk's file), and its explanation, for
example `Viewers can write (app/auth.py lines 10-12): The role check is gone, ...`. The published
tools' candidates were extracted from their comments by the benchmark's step 2 with a model;
CodeAtlas's risks are already one issue each, so step 2 is skipped for CodeAtlas.

### Comparison

For CodeAtlas and every published tool, the runner sums true positives (golden comments matched),
false positives (candidates matching no golden comment), and false negatives over the same pull
requests, as the benchmark's step 3 sums them, and reports precision `TP / (TP + FP)` and recall
`TP / (TP + FN)` with their denominators. It counts golden comments of every category, which is
the dashboard's "All" profile; the dashboard defaults to its "Core" profile, which leaves out
style and speculative golden comments, so its numbers differ. Pull requests whose CodeAtlas review
failed or was not fully judged are left out for every tool and listed as failures.

Two tables are given, with and without the four flagged Sentry items, whose `az_comment` carries a
data warning: `sentry-greptile` pull requests 1, 2, and 3 ("reviewed commit is not in the repo")
and 5 ("there is no such PR, it is a mix of many PRs").

Some published evaluations were made against an older version of the golden comments: at the
pinned commit, 28 of the 49 tools judged by Claude Opus 4.5 (25 by GPT-5.2) were judged on 14 of
these 20 pull requests against golden comments that differ from the current ones. The runner
detects this by comparing each evaluation's matched and missed golden comments with the current
ones, and lists those tools in a separate table with the number of such pull requests, because
their numbers do not measure the same labels. `evaluations.json` also holds superseded tool
versions that the dashboard hides; the report lists every tool.

### Cost

- Reviews: one model call per pull request, or two when a review needs a repair: about $0.03 to
  $0.38 each, so at most about $8 for the 20 (research R16). The report estimates each review's
  cost from its tokens at the prices of 001 research R11.
- Judge: step 3 makes one call per golden comment and candidate (77 golden comments times the
  number of risks), and step 2.5 one call per pull request with at least two candidates; a few
  dollars in all. The report counts these calls; the runner cannot see the provider's bill.
- Disk and network: up to 40 archives in `evals/out/cache/`, several GB (a Cal.com archive is
  about 275 MB, a Sentry archive about 40 MB).

### Outputs

Each run writes two files to `--out`, named with the UTC start time:

- `bench-eval-<time>.md`: the benchmark commit, the review model and prompt version, the judge and
  its directories, the limits used, the comparison tables (all items and without the flagged
  items), the flagged items, a row per pull request (files reviewed, partial, risks, golden
  comments, TP, FP, FN, seconds, tokens, and estimated cost), the cost, the caveats, and the
  failures.
- `bench-eval-<time>.json`: the same data, with each pull request's commits and candidates.

### Caveats

Every report of these numbers states:

- the pull requests come from well-known public repositories, so the models may have seen them,
  and their fixes, in training;
- the judge is a model, and another judge may match differently;
- CodeAtlas reports risks only, so golden comments about style or documentation count against its
  recall;
- step 2 was skipped for CodeAtlas, while the published tools' candidates went through it;
- the flagged items carry the benchmark's own data warnings, so results are given with and without
  them;
- some published tools were judged against an older version of the golden comments, and are
  listed apart;
- the repository limits differ from the pilot's.

The reports quote the risks' explanations of third-party code, so keep them in `evals/out/`.
