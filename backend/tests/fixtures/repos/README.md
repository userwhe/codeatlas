# Fixture repositories

The fake GitHub gateway (`src/codeatlas/github/fake.py`) serves these directories as tarballs,
plus a few archives it generates in code. Every archive has one top-level directory named like
GitHub's (`<owner>-<repo>-<sha[:7]>/`).

## Repositories served by the fake

| ID   | Full name                     | Content                                   | Access           |
|------|-------------------------------|-------------------------------------------|------------------|
| 2001 | `octo-org/sample-app`         | `sample-app/` + injected files, 2 commits | octocat, hubot   |
| 2002 | `octo-org/sample-app-private` | same as sample-app (private)              | octocat          |
| 2003 | `octo-org/no-code`            | `no-code/`                                | octocat          |
| 2004 | `octo-org/oversized`          | generated                                 | octocat          |
| 2005 | `octo-org/vendored-heavy`     | generated                                 | octocat          |
| 2006 | `octo-org/unsafe-paths`       | generated                                 | octocat          |
| 2007 | `octo-org/empty`              | no branches (`RepositoryEmpty`)           | octocat          |
| 2008 | `octocat/solo`                | `no-code/`                                | octocat          |
| 2009 | `hubot/tools`                 | `no-code/`                                | hubot            |
| 2010 | `monalisa/public-lib`         | `no-code/`                                | App not installed |
| 2011 | `octo-org/review-app`         | `review-app/` + pull requests             | octocat, hubot   |
| 2012 | `octo-org/review-app-private` | same as review-app (private), #1 only     | octocat          |

As on GitHub, every user can see a public repository; the Access column lists who reaches it
through an installation of the App. Installations: `octo-org` 5001, `octocat` 5002, `hubot` 5003.
Users: `octocat` 1001, `hubot` 1002, and `monalisa` 1003, who has no installation and reaches no
repository through the App. Commit SHAs are
`sha1("<repository id>:<commit name>")`; the module function `commit_sha` computes them.

## sample-app

Files injected by the fake rather than stored here: credential names are git-ignored and flagged
by secret scanners, `node_modules/` is git-ignored, and an unparsable `.py` file would fail the
repository's linter.

| Path                                  | Stored   | Expected coverage reason                     |
|---------------------------------------|----------|----------------------------------------------|
| `node_modules/left-pad/` (2 files)    | injected | `excluded_directory` (one directory entry)   |
| `.env`                                | injected | `credential_file`                            |
| `id_rsa`                              | injected | `credential_file`                            |
| `static/vendor.min.js`                | on disk  | `generated` (`*.min.js`)                     |
| `src/generated/apiClient.ts`          | on disk  | `generated` (`linguist-generated` in `.gitattributes`) |
| `assets/logo.png`                     | on disk  | `binary` (NUL bytes)                         |
| `docs/latin1-notes.txt`               | on disk  | `unsupported_encoding` (Latin-1 byte `0xE9`) |
| `data/large.txt`                      | injected | `too_large` (`max_file_bytes + 1` bytes)     |
| `app/legacy.py`                       | injected | `unsupported_syntax` (file is still indexed) |

Everything else is eligible, including `.env.example` (not a credential file) and
`.gitattributes`. Symbols worth asserting on:

- Python: `check_repository_access` and class `AccessPolicy` with method `can_read` in
  `app/auth/access.py`; `describe_access` in `app/main.py`.
- TypeScript: interface `User`, enum `Role`, and type alias `UserId` in `src/types.ts`; class
  `UserService` with method `getUserById` in `src/services/userService.ts`; exported const
  arrow function `formatUser` in `src/utils/format.ts`; component `UserCard` in
  `src/components/UserCard.tsx`.
- Docs: `README.md` has a `## Setup` section; `docs/architecture.md` describes access control.

Commits: `main` starts at `initial`. `FakeGitHub.advance(2001)` moves it to `second`, which
renames `app/utils/strings.py` to `app/utils/text.py` and deletes `app/reports.py`
(`SAMPLE_APP_RENAMED`, `SAMPLE_APP_DELETED`).

## review-app

A small Python and TypeScript app with tests, for pull request reviews. Its pull requests are
built from the overlays in `../pull-requests/` (see the README there); the 001 and 002 fixtures
stay unchanged. Commit `initial` is the merge base of every pull request, and both `main` and
`release` point to it.

| Path                         | What to assert on                                                   |
|------------------------------|---------------------------------------------------------------------|
| `app/auth/permissions.py`    | `User`, `WRITE_ROLES`, and `can_write` (role and repository checks) |
| `app/repositories.py`        | `rename_repository`, which calls `can_write`                       |
| `app/text.py`                | `slugify`                                                           |
| `tests/test_permissions.py`  | owner, viewer, and other-repository cases for `can_write`          |
| `tests/test_text.py`         | one `slugify` case                                                  |
| `web/src/format.ts`          | `formatCount`; `formatCount(0)` gives `"0 item"` until #2 fixes it |
| `web/src/format.test.ts`     | two `formatCount` cases (`describe`, `it`, `expect`)                |

The test files are data: the fixture conftest keeps pytest from collecting them, and nothing runs
the TypeScript test.

## no-code

Markdown and plain text only (`README.md`, `docs/onboarding.md`, `docs/release-process.md`,
`notes.txt`). It indexes with 0 symbols.

## Generated archives

- `oversized`: `max_files_per_snapshot + 1` one-line `.txt` files under `files/`, read from the
  settings when the tarball is opened. Fails with `limit_exceeded` (`max_files_per_snapshot`).
- `vendored-heavy`: 6,000 one-line `.js` files under `node_modules/` and 6 eligible files
  (`README.md`, `package.json`, `app/__init__.py`, `app/server.py`, `src/index.ts`,
  `docs/usage.md`). Reaches `ready` with `node_modules/` as one `excluded_directory` entry.
- `unsafe-paths`: safe `README.md` and `app.py`, plus `<top>/../escape.txt` and
  `/etc/codeatlas-absolute.txt` (`unsafe_path`), a symlink `docs-link` to `/etc/passwd`, and a
  hard link `readme-hardlink.md` to `README.md` (`link`).
