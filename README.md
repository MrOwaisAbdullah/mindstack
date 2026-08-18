# BrainOutside

**Your brain, kept outside your head — where your agents can read it.**

A self-hosted memory server for your AI agents. One git repo full of
markdown is your brain; BrainOutside serves it over REST and MCP, with
visibility tiers enforced server-side and a human gate on every write.

Single-user and single-brain by design. That is the product, not a
limitation waiting to be fixed.

**Two ways to run a brain, one repo between them.** Start local:
clone [brainoutside-template](https://github.com/hassancs91/brainoutside-template),
open it in VS Code, and Claude Code is the whole interface — free,
private, zero infrastructure. Or self-host this server as the brain's
online head, so every agent you run anywhere can read your mind over
MCP and REST. They compose: the local repo IS the repo the server
clones. Start local today, add the server when you want it, migrate
nothing. Site and docs: [brainoutside.com](https://brainoutside.com).

> Status: pre-release. The engine and the first-run wizard are built
> and verified end to end; what remains before the public beta is
> packaging, docs, and launch assets.

## Where this came from

This project grew out of [Andrej Karpathy's llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
and the wave of markdown knowledge bases around it. The core insight
there is right, and it is the foundation this builds on: plain markdown
plus a coding agent beats RAG for personal knowledge — compile
knowledge in at ingestion time and the artifact compounds, instead of
being re-assembled from chunks on every query.

Building mine, I kept hitting four walls, and they became this project:

- **A wiki holds knowledge, not you.** An agent could recall from mine,
  but it couldn't *write as me* — there was no identity, no voice, no
  beliefs in it. Here `identity/` is first-class, and the note kinds
  (`take`, `story`, `lesson`, `fact`) are shaped for creating content,
  not just referencing it.
- **No safe way to grow.** A wiki that maintains itself fills up with
  unreviewed extractions, and a brain you don't trust is a brain you
  stop using. Here every write is gated: agents propose, you approve,
  approval is one signed commit.
- **Recall isn't the point — creation is.** I built this to *make
  things* from my brain — replies, posts, scripts — in my own voice.
  Lenses and context packs turn the brain into a writing instrument.
- **A local folder serves one tool on one machine.** I wanted every
  agent I run, anywhere, to read my mind. That is this server: the
  brain's online head — self-hosted, private, with visibility tiers
  enforced server-side.

The llm-wiki was not the only influence; if you recognize your project
in this lineage and want a link here, open an issue.

The server itself grew out of my
[MCP API boilerplate](https://learnwithhasan.com/boilerplates/mcp-api-boilerplate/)
— a Django starter that already had the REST + MCP plumbing, API keys
and self-documenting endpoint pages wired up. That heritage is why you
may occasionally find a feature the brain doesn't use; they get removed
as they're found, and a report is welcome.

## Origin

This is a personal fork of [BrainOutside](https://github.com/hassancs91/brainoutside)
by Hasan Aboul Hasan (MIT), described above. Git history was
intentionally stripped at fork time — this repo is the maintained record
of that fact; see [`CREDITS.md`](CREDITS.md) for full lineage. I run this
as my own personal brain, not a rebrand or resale, and intend to
contribute improvements back upstream where they fit.

## Why not just use a vector database

Because you cannot read one. Your brain here is plain markdown in a
normal git repo:

- **You can read it.** Open it in any editor. `git log` it. Fix a note by
  editing a file.
- **You can leave.** It is your repo. Delete the server and the brain is
  still there, intact and useful.
- **It has history.** Change your mind and the old note is *superseded*,
  not deleted — so your brain records how your thinking moved, not just
  where it landed.

## How it works

**One repo is the brain.** Atomic notes with a strict frontmatter
contract: opinionated `take`s, `story`s with real numbers, `lesson`s,
citable `fact`s. Every note carries provenance back to its source.

**Agents read it through a lens.** A lens is a named retrieval scope —
topics, note types, and a visibility ceiling. Ask for a context pack and
you get the right 3–7 files, not the whole repo.

**Nothing enters without you.** Feed a source — a video, a post, a
transcript, a raw thought — and an agent *proposes* notes. You approve in
a UI. Approval is one signed git commit. A brain that fills itself with
unreviewed extractions is a brain you stop trusting.

**Tiers are enforced, not decorative.** Every note resolves to `public`,
`agents-only` or `private`, and each API key sees only its tier — because
the reader agent runs against a materialized snapshot of that tier and
physically cannot read above it.

## What you get

- **REST + MCP** — point Claude Code, or any MCP client, at your own mind
- **A gated write path** — proposals land in an approval queue; approval
  commits and pushes
- **A chat test bench** — talk to your brain at any tier, with the sources
  it used shown per message
- **Visuals** — visibility rings, a topic graph, live read activity, and a
  timeline of every position you have revised
- **A full ledger** — every token, every read, every SDK run

## Requirements

Somewhere to run Docker (a VPS with Coolify, or `docker compose` on any
box), a GitHub account, and a Claude credential — an Anthropic API key
**or** a Claude subscription token (`sk-ant-oat`), so you can run this
without API billing.

## Getting started

The 10-minute path is [`docs/INSTALL.md`](docs/INSTALL.md): two
environment variables, `docker compose up`, and the `/setup` wizard
does the rest in the browser — account, brain repo from the template,
deploy key, write credential, Claude credential, first build. No
terminal after the compose command. On Coolify it is shorter still:
[`docs/DEPLOY.md`](docs/DEPLOY.md).

## Your brain repo

The brain template lives in its own repo, separate from this server —
not in-tree here. Mine is `mindstack-brain`, a private fork of
[brainoutside-template](https://github.com/hassancs91/brainoutside-template):
the contract, both agent skills, note templates and identity files. It
ships with zero notes on purpose — an empty brain that is truly yours
beats a seeded one you have to clean out.

## Docs

| | |
|---|---|
| [`docs/INSTALL.md`](docs/INSTALL.md) | Installing: compose happy path, Coolify pointer, updating |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | The full Coolify runbook — proxy/CDN client IPs, backups, webhook |
| [`docs/SECURITY.md`](docs/SECURITY.md) | The honest security posture, and how to report a vulnerability |
| [`docs/PLAN.md`](docs/PLAN.md) | Full architecture, data model, milestones |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Ground rules, dev stack, tests, guardrails |

## Running it locally

```sh
./dev.sh        # macOS / Linux
.\dev.ps1       # Windows — same commands
```

Builds on first run and starts web + mcp + worker + postgres + redis,
waits for the healthcheck, prints the URLs. Same containers as the
deploy; only `docker-compose.local.yml` differs.

| Command | |
|---|---|
| `./dev.sh` | build if needed, start everything, wait for health |
| `./dev.sh reload [svc]` | restart app containers — picks up code, no rebuild |
| `./dev.sh rebuild [--no-cache]` | rebuild images and recreate containers |
| `./dev.sh down [--volumes]` | stop and remove (`--volumes` also drops the DB) |
| `./dev.sh logs [svc]` / `ps` / `status` | follow logs / container state / `+ /readyz` |
| `./dev.sh shell [svc]` / `manage <args>` / `superuser` | bash in / `manage.py` in / create a login |
| `./dev.sh css [--watch]` | rebuild the committed Tailwind artifact |

Source edits are live — the repo is bind-mounted, so `web` reloads
itself. `mcp` and `worker` need `./dev.sh reload`. Only a
`requirements.txt` or `Dockerfile` change needs `rebuild`.

App on <http://localhost:8000>, Postgres on `localhost:5433`, Redis on
`localhost:6380` (offset so a host install keeps its default port).
