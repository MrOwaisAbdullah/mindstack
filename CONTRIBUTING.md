# Contributing to BrainOutside

Thanks for looking. Ground rules first, because they shape everything
else here:

**This is a single-user, single-brain product, maintained by one
person.** That is its identity, not a missing feature — pull requests
that add multi-tenancy, user management, or team features will be
declined kindly. If you want those, the codebase is MIT; fork away.

## What helps most

- **Bug reports** — especially from real self-hosted deployments.
  A report that includes your `/ops/health/` output and the relevant
  container log lines is usually fixable same-day.
- **Docs fixes** — anything that was wrong or confusing on your first
  install.
- **Small, focused fixes** — one problem per PR.
- **Anything larger: open an issue first.** The architecture has strong
  opinions (documented in `docs/PLAN.md`), and agreeing on the shape
  before you write code respects your time.

Questions about the brain repo format (note kinds, frontmatter,
lenses) belong on the
[brainoutside-template](https://github.com/hassancs91/brainoutside-template)
repo — that is where the contract lives.

## Security issues

Do not open a public issue. See the
[reporting section in docs/SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability).

## Running it locally

Docker is the only requirement:

```
./dev.sh          # macOS / Linux
.\dev.ps1         # Windows
```

Either script builds and starts the full stack (web + mcp + worker +
postgres + redis) and prints the URLs. Source edits are live — the repo
is bind-mounted; `web` reloads itself, `mcp` and `worker` need
`./dev.sh reload`.

## Running the tests

Tests run on the host, not in the container:

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-django
DJANGO_SETTINGS_MODULE=config.settings.dev .venv/bin/python -m pytest apps/core/tests apps/core/mcp/tests -q
```

(Windows: activate `.venv` and set the env var in PowerShell.) The suite
is DB-light — most tests need no database at all, and the ones that do
use SQLite via `config.settings.dev`.

## The guardrails you will hit

These exist because each mistake was made once, for real. The test
suite enforces all of them, so you will find out either way — this is
just the why:

- **CSP is enforced in dev, on purpose.** An inline `style="…"`
  attribute or a nonce-less inline `<script>` is silently dropped by the
  browser, so the page renders and the feature is simply dead.
  Report-only mode hid a release blocker for the life of this project;
  it stays enforced.
- **`static/css/tw.css` is a committed build artifact.** Adding a
  Tailwind class to a template does nothing until you run
  `./dev.sh css` (or `.\dev.ps1 css`) and commit the result. A test
  fails if the artifact is stale.
- **Style against semantic tokens** (`bg-surface`, `border-line`,
  `text-muted`), never a hex value or a palette primitive. Dark mode is
  a token swap; components carry no `dark:` variants.
- **"Is this engine, or user-config?"** Anything personal — names,
  domains, repo URLs — goes in settings, the database, or the brain
  repo. Engine code must grep clean of personal values.

## Commit style

One finding or change per commit. The message says what the change
does, and — for fixes — what was verified and what was not. Read
`git log` for the house voice; commit messages are the project's
real changelog.

## Code style

Match what surrounds you. Comments explain constraints the code cannot
express ("this must run before X because Y"), not what the next line
does. If a module has a long docstring explaining its shape, keep it
true when you change the shape.
