# Changelog

Releases are tagged `vX.Y.Z`; each tag builds and publishes
`ghcr.io/<owner>/brainoutside`. Between releases, `git log` is the real
changelog — commit messages here carry what changed, what was verified,
and what was not.

## [Unreleased]

First public beta, in preparation. The engine as it stands:

- A git repo of markdown is the brain; the server clones, indexes and
  serves it — never the other way around.
- Read surfaces: REST + MCP, with visibility tiers (`public` /
  `agents-only` / `private`) enforced server-side via per-tier
  materialized snapshots.
- Write surface: feed → agent-extracted proposal → rules 1–8
  validation → human approval in the ops UI → one commit, pushed with
  a repo-scoped write credential.
- First-run wizard at `/setup` — two required env vars, no terminal.
- Chat test bench, visibility rings, topic graph, timeline, full token
  ledger.
- Runs on Claude via an Anthropic API key **or** a subscription token
  (no API billing required).
