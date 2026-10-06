# Answer-quality evaluation

> **Draft: needs human review before use.** The questions, answerability labels, splits, and
> evidence ranges in `qa_v1.jsonl` were drafted and checked mechanically, but no person has
> reviewed them yet. Do not report any number measured with this set until a reviewer has worked
> through the [review checklist](#review-checklist) and recorded the review in the
> [review log](#review-log).

This directory holds the versioned question set for repository Q&A and the runner that measures
it against the success criteria in `specs/001-repository-qa/spec.md`:

| File | Purpose |
| --- | --- |
| `qa_v1.jsonl` | The question set, version 1 (68 questions over 3 public repositories) |
| `run_qa_eval.py` | The runner: indexes the pinned commits, asks the questions, and writes the report |
| `out/` | Reports, audit sheets, and scratch clones (gitignored; never commit it) |

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
