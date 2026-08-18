# my-brain-web-app — Phase 2 Plan (v2, post-grill)

The online head for Hasan's mind (`my-brain` repo). A Django app that owns a
server-side clone of the brain repo and exposes it as REST + MCP + UI, with a
gated write path, structured logging, and a chat test bench.

**Core principle (from phase 1):** the git repo stays the single source of
truth. Everything in this app is either a *view* of the repo or a *gated
pipeline into* the repo. The DB is a rebuildable index for repo-derived data
— if DB and repo disagree, the repo wins and a `drift` event is logged.
(Exception, made explicit by the grill: Feeds, Events, SdkOperations, and
Chat history are *primary* data that exist only in the DB — see §10 Backups.)

> **v2 note:** this plan was adversarially reviewed ("grilled") by three
> independent reviewers against the real code of `mcp-api-starter-template`,
> the real content of `my-brain`, and the current Claude Agent SDK docs.
> 52 findings; every blocker/major is resolved inline below. §12 is the log.

---

## 1. Base: vendored core, not a full fork  ✅ DECIDED (Hasan, 2026-07-28)

Original plan: fork `mcp-api-starter-template` wholesale. The grill changed
the recommendation. The template is 22 apps / ~30 models / 15 middleware
built for multi-tenant SaaS; for a single-user internal tool ~8–10 apps are
dead weight, and the "dormant" parts are **not inert**:

- REST serves zero-credit endpoints **anonymously** (`apps/core/rest.py` —
  401 only when `credits_cost > 0`). With billing dormant, every brain read
  endpoint would be world-readable by default.
- Rate limits are per-*user*, not per-key (`apps/rate_limit/throttle.py`) —
  all consumer keys share one bucket, and the plans app sits in the hot path
  of every request; a seeded demo plan imposes a 30/min limit on migrate.
- Dashboard login is magic-link **email only** — an email provider must be
  configured just to log in.
- Per-key `max_visibility` requires touching framework apps, which forfeits
  clean upstream merges (`docs/FORKING.md` §"framework apps") — the fork's
  main justification.

**Recommended: fresh minimal Django project that VENDORS the template's
crown jewels** — `apps/core` (the `@endpoint` registry + REST + MCP bridge),
`apps/api_keys`, `apps/mcp_proxy` (auth + lockout), plus its Dockerfile and
security-middleware patterns — and skips accounts/allauth, billing, plans,
notifications, playground, and the multi-user dashboard. Consequences:

- Anonymous reads become impossible **by construction**: our thin REST
  wrapper requires an authenticated principal on every call, period.
- Auth for the ops UI: plain Django session login (single superuser) behind
  a network boundary (§9), no magic-link/email dependency.
- Per-key tier + per-key rate limit live in our own app cleanly.
- We keep the endpoint registry's "one class → REST + MCP + docs" and the
  hardened MCP proxy — the parts that are genuinely expensive to rebuild.

Fallback (if Hasan prefers the full fork for product-dogfooding reasons):
viable, but M1 must then budget six extra work items: deny-anonymous
override, per-key throttle, api_keys side-table for tier, email provider
config, demo-plan re-seed, and Dockerfile additions. Either way §2–§11 are
unchanged — they don't depend on this choice.

## 2. App layout

| App | Responsibility |
|---|---|
| `vendored/core`, `vendored/api_keys`, `vendored/mcp_proxy` | Endpoint registry (REST+MCP+docs), key auth — from the template, trimmed |
| `apps/brain` | Git clone manager, sync + webhook, Entity index, per-tier snapshot builder, validator, drift check |
| `apps/mind` | The public surface: endpoint classes |
| `apps/feeds` | Feed proposals, approval queue, commit+push on approve |
| `apps/reader` | SdkRunner (shared Agent SDK service), `assemble-context`, chat |
| `apps/events` | Structured event log + dashboards |
| `apps/brainconfig` | Settings page: Claude SDK config, budgets, test connection |

Registry constraints (verified): slugs are kebab-case (`get-note`, not
`get_note`) and REST is POST-only — fine for MCP tools, documented for REST
consumers.

## 3. Data model

### `apps/brain`
- **Entity** — one row per content entity, **built from frontmatter +
  filesystem only** (INDEX.md is a *view*, never a parse source — grill B7).
  Fields: `entity_id`, `kind`, `path`, `title`, `description`, `status`,
  `superseded_by`, `visibility` (resolved — see §5), `topics` (JSON),
  `projects` (JSON), `source`, `source_url`, `date`, `last_verified`,
  `content_hash`, `indexed_at`. Per-kind frontmatter schemas: the repo has
  five dialects (knowledge notes, project cards `kind:`/`last-verified:`,
  identity, lens, no-frontmatter files) — one parser per kind, `_TEMPLATE.md`
  and non-entity files excluded by pattern from both indexing and the drift
  hash (grill B8, B12).
- **SyncRun** — trigger, commit hash, entity delta, drift flag, duration.

### `apps/feeds`
- **Feed** — `source_id`, `channel` (ui/api/mcp), `raw_payload`, `proposal`
  (JSON), `status` (pending/approved/edited/rejected/failed), `decided_at`,
  `commit_hash`, `retries`, `error`. FK → SdkOperation.

### `apps/reader`
- **ChatSession** — `tier`, `title`, totals. **ChatMessage** — role, content,
  `sources` (JSON: entity_ids + visibility + staleness at serve time), FK →
  SdkOperation.

### `apps/events`
- **Event** — `type` (read/feed/drift/auth_denied/sync/settings_change/
  degraded), `consumer` FK, `entity_ids` (JSON), `details` (JSON); indexed
  on `created_at`, `type`, `consumer`.
- **SdkOperation** — the token ledger, one row per SDK invocation. Written
  **before** the run (`status=running`) and finalized after, so killed runs
  are never invisible (grill C6). Fields: `kind`, `model`, `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`
  (CLI estimate — **display-only**; raw token counts are canonical, cost is
  recomputable from the price table — grill C23), `duration_ms`, `ok`,
  `error_class`, `prompt_hash` (composed-prompt version, for
  reproducibility), generic FK. Usage fields nullable: errored/killed runs
  legitimately have no usage.

### `apps/brainconfig`
- **AppSetting** — KV with a new Fernet-encrypted value field (the template
  has the key but no encrypted settings store — small build, grill A13).
  Keys: `ANTHROPIC_API_KEY`, per-kind `CLAUDE_MODEL`, per-kind
  `MAX_BUDGET_USD` + `MAX_TURNS`, `DAILY_COST_CAP`, `BRAIN_REPO_URL`,
  `GITHUB_WEBHOOK_SECRET`. Empty-string env/setting reads as unset
  (Coolify lesson).

## 4. The git layer (`apps/brain`)

- Clone at `/data/brain-repo`, mounted into **every container that touches
  it** (web + mcp + worker — endpoint code runs in all three, grill A5).
- **Split credentials** (grill C13 — integrity is the crown jewel): the
  standing clone/sync path uses a **read-only** deploy key; the **write**
  credential (fine-grained GitHub PAT or App, contents:write on this one
  repo) exists **only** in the worker container, only for the approval
  handler, at a path on the SDK agents' deny list. Repo gets commit
  notifications so an out-of-band commit is noticed.
- **Cross-container single-writer lock**: flock on the shared volume; ALL
  mutating git ops (approval writes, webhook pulls, beat pulls) take it
  (grill C19).
- Server commits carry a distinct identity: `brain-app
  <brain@learnwithhasan.com>`, with the Feed id in a commit trailer.
- Approval write sequence: lock → pull --rebase → apply proposal files →
  validate → commit `feed: <source-id>` → push → reindex + rebuild
  snapshots → unlock. **Push rejection (non-fast-forward) is a routine
  race, not a failure**: retry the whole locked sequence up to 3× (the
  proposal JSON makes replay safe) before marking the Feed failed;
  any real failure rolls back the tree (grill C20).
- **Webhook** `POST /webhooks/github`: HMAC via `hmac.compare_digest`,
  dedupe on `X-GitHub-Delivery`, no-op if delivered SHA == local HEAD
  (kills the push→webhook echo); 15-min beat pull as fallback.
- **Idempotent bootstrap**: on startup, empty/invalid `/data/brain-repo` →
  clone + full reindex + snapshot build; startup check **fails loudly** if
  CLAUDE.md, `.claude/skills/`, or `lenses/` are missing from the clone
  (grill B1); health check asserts clone HEAD vs origin (grill C22).
- Volume is owned by uid 1000 (image runs non-root) — init ownership on
  first boot (grill A5c).
- **Drift check** per reindex: repo-parsed state hash vs DB hash → `drift`
  event with exact entity ids → auto-repair from repo. Dashboard tile.

### Validator (runs on every feed proposal, pre-commit)
Scoped: note rules apply to `knowledge/` only (grill B8).
1. Per-kind frontmatter schema valid; for knowledge: `id` ↔ filename,
   `type` ↔ folder.
2. `source` + `source_url` present.
3. `topics` ⊆ taxonomy parsed from CLAUDE.md in the clone.
4. Takes/stories contain `> VERBATIM:`.
5. No file deletions.
6. No content in a script the brain's own CLAUDE.md lists under
   **Blocked scripts**. Empty by default — owner policy, not engine
   policy. (Was a hardcoded Arabic ban, which is a fact about one
   owner's corpus and rejected a stranger's notes on an invisible rule.)
7. INDEX.md **consistency**: every new/changed entity's index line exists
   AND agrees with frontmatter on status and visibility (the old rule 7
   would have passed today's real voice.md mismatch — grill B5/B7/B14).
8. Proposal bodies must not quote content from `visibility: private`
   entities (anti-exfiltration backstop — grill C12c).

## 5. Visibility model (rebuilt — the grill's biggest target)

**Frontmatter is authoritative.** INDEX.md lines are a generated view.

**Every file gets a resolved tier** via: explicit frontmatter `visibility:`
→ else a **per-directory default map**, checked into CLAUDE.md in the same
M0 commit (grill B3/B4): `identity/` per-file frontmatter (voice.md is
agents-only *today* — B5), `knowledge/` frontmatter (default agents-only
per contract §4), `projects/` frontmatter, `content-catalog/` public,
`lenses/` public, `raw/` **inherits max visibility of the notes that link
it** (never browsable — B11), `eval/`, templates, `PENDING.md`, and
**anything unclassified → private, deny-by-default** (B2).

**Line-level filtering**: the repo already uses inline `(agents-only: …)`
spans inside public files (identity/core.md, projects template — B6). The
file-access layer strips these spans for tiers below agents-only. The
convention gets written into CLAUDE.md.

**Per-tier materialized snapshots** (grill C11 — the load-bearing fix):
visibility can't be enforced on the reader agent by path rules, because
tiers live in frontmatter and public/private files share directories. So on
every reindex the snapshot builder exports `/data/brain-views/{public,
agents-only,private}/` containing: allowed files (with sub-tier line spans
stripped), a **generated, tier-filtered INDEX.md**, and tier-appropriate
identity files. SDK agents get `cwd` + `Read` scoped to **their tier's
snapshot only** — an agent physically cannot read above tier. The dumb
layer serves from the same snapshots, so both layers enforce identically.
`get-index` serves the generated index (raw INDEX.md is not reliably
parseable and self-violates its format — B7).

**No anonymous access, any layer**: every REST/MCP call requires an
authenticated key; the file-access service hard-denies a null principal
(grill A1). `auth_denied` → Event.

**Known M0 content chores this model exposes** (grill B2/B5/B13/B16):
`PENDING.md` → private by default map + review; decide the public-voice
question (voice.md is agents-only but `get-identity` must serve *something*
to public consumers — likely a public voice subset file); promote lens
metadata (topics, ceiling) from prose into frontmatter; don't render
agents-only provenance links (`source:` pointing at private repos) to lower
tiers (B15).

## 6. The read surface (`apps/mind`)

Endpoint classes (kebab-case slugs, POST, auto REST + MCP + docs):

**Dumb layer** (serves tier-snapshot bytes):
- `get-index` — generated index for caller tier.
- `list-notes` — Entity query: kind/topic/project/status filters.
- `get-note` — full markdown by entity_id.
- `get-lens`, `get-identity` — tier-filtered per file (B5).
- `get-raw` — resolves ONLY via a link from a note/card the caller can see;
  inherits that note's tier (B11).

**Smart layer**:
- `assemble-context(task, lens?)` — reader agent over the caller-tier
  snapshot; returns `{context_pack, entity_ids_used, tokens}`. Documented
  honest latency: 5–30 s (subprocess spin-up + agentic turns — C2).

**Write door**:
- `propose-feed(source, payload)` — creates a pending Feed; never touches
  the repo. Per-key rate-limited from M1 (A3 — our own throttle, per key
  not per user). Payload cap env-tunable for long transcripts (A12).

## 7. Claude Agent SDK integration (`apps/reader`)

**Runtime** (grill C1): `claude-agent-sdk` pinned in the image (pinning the
SDK pins its bundled CLI binary); Debian-slim base (bundled binary is
glibc-linked — no Alpine); **git installed** (template image lacks it);
build-time smoke test imports the SDK and runs `--version`. Worker image
grows ~100–250 MB; accepted.

**Lockdown — the real config, not vibes** (grill C4, C12a):
- `permission_mode` deny-by-default; allow rules scoped to the snapshot:
  `Read(//data/brain-views/<tier>/**)`, `Grep`, `Glob`; **everything else
  denied by name** (Bash, Write, Edit, WebFetch, WebSearch, Agent…) so the
  tools leave context entirely; explicit deny on secret paths;
  `strict_mcp_config=True`; `setting_sources=[]` (nothing auto-loads from
  `~/.claude` or the repo's `.claude/`).
- `PreToolUse` hook as belt-and-braces.
- Container egress firewalled to `api.anthropic.com` + GitHub only (C12a).
- Adversarial test in CI: agent prompted to read `/proc/self/environ` and
  to fetch a URL — both must fail.

**Prompt composition** (grill C7/C8, B9): server composes prompts —
Claude Code preset **+ append** (never replace), appending CLAUDE.md body +
a **server-mode variant** of the skill (frontmatter stripped, interactive
parts overridden). The repo skills stay canonical for local Claude Code;
the server-mode preambles ALSO live in the repo (committed M0) so local
and server share one versioned source. Divergence is acknowledged and
covered by the M4 eval instead of pretended away. Specifically:
- Feeder server-mode: "emit a proposal object matching this JSON schema;
  never write files, never commit, never wait for approval" — enforced by
  `output_format` JSON-schema (C9) AND by the tool policy. The skill's
  Stage-2/gate/queue-file/URL-fetch instructions are overridden; URL/
  transcript fetching happens in trusted Django code *before* the agent
  runs, so the agent needs no network.
- Reader server-mode: no "ask if MIND_PATH missing" (headless), lens passed
  as a parameter; public-tier prompts are composed only from public-visible
  material so the prompt itself can't leak private project names.
- `prompt_hash` of the composed prompt recorded per SdkOperation.

**Budgets — real knobs** (grill C5): there is no per-run token cap. Per
kind: `max_budget_usd` (soft, checked between turns) + `max_turns` +
wall-clock timeout with subprocess kill as the hard stop. Plus a **daily
circuit breaker**: SdkRunner refuses to start when today's summed cost >
`DAILY_COST_CAP` (admin-exemptable) (C16). Plus a spend limit set in the
Anthropic console as the independent backstop. **Model routing**: retrieval
(`assemble-context`, chat) defaults to a Sonnet-class model; feed
extraction may use a bigger model. Realistic cost (C16): ~$0.10–0.25 per
assemble/chat run on Sonnet-class; frequency, not per-op size, is the cost
driver — hence circuit breaker + per-key rate limits at M1.

**Execution + streaming** (grill A2, C10 — open question resolved): chat
and `assemble-context` run **in-process in async Django views** (the SDK's
`query()` is async; `include_partial_messages=True`) streaming via
`StreamingHttpResponse` SSE under an ASGI server (uvicorn). The queue
worker is used **only** for feed extraction (fire-and-forget; UI polls the
Feed row — the template's native pattern). No worker→SSE plumbing. Worker
concurrency note: each SDK run holds a worker slot; ack-timeout must exceed
task timeout or long runs double-bill (A11).

**Degraded mode** (grill C21): Anthropic outage → dumb layer unaffected;
`propose-feed` still accepts and queues (capture must never lose data),
extraction retries with backoff; chat/assemble fail fast with a clear
error, never auto-retry; health tile shows degraded; SDK errors map to
`ok=false` + `error_class`.

**SDK session transcripts** (C3): the CLI writes JSONL session files inside
the container — they contain note content, so: retention/cleanup job, and
treated as sensitive (§9).

## 8. UI (Django templates, learnwithhasan theme)

Theme derived from the live site's `tokens.css` contract: paper `#fffef7` /
cream `#faf8f5` surfaces, ink `#1a1a2e`, muted `#6b6b7b`, indigo accent
`#6366f1` (hover `#4f46e5`), violet/coral secondary, teal `#4db8a8`
success, yellow warn, red `#ff6b6b` danger; dark terminal ink scale
(`#0d1117`…`#c9d1d9`) for logs/code. Space Grotesk display, Inter body,
JetBrains Mono for ids/code. Radius 14/10px, soft double shadows. Going
vendored-core (§1) means we build our few pages on these tokens directly —
no restyling of 22 apps' worth of SaaS dashboard templates (A10).

Pages (ops-first V1): Dashboard (sync health, pending feeds, reads, token
spend day/week, most-served notes, staleness >45d) · Brain browser
(filterable entities, rendered note view, frontmatter chips, staleness
flags) · Feed queue (**diff view — proposals rendered as diff, not
executed markdown**, per C12d; edit-before-approve re-validates; reject
with reason) · Chat (sessions, tier switcher, sources panel, per-message
tokens) · Logs (event stream filters + SdkOperation ledger + aggregates) ·
Settings (SDK key write-only encrypted, model/budget pickers, daily cap,
repo/webhook config, pull-now, rebuild-index, **Test connection** →
model+latency+tokens, logged) · Consumers (keys: tier, rate limit, last
used, revoke).

Auth: single Django superuser session login; UI reachable only through the
network boundary (§9).

## 9. Security posture (public VPS; ranked)

1. **Reader-agent leak** → per-tier snapshots (§5) — structural fix.
2. **Prompt injection via fed content** (C12): fed URLs/transcripts are
   attacker-influenceable input to an agent. Mitigations: no-network
   no-Bash agent config (§7), fetch-before-agent in trusted code, egress
   firewall, feeder runs against the minimum snapshot (not private),
   validator rule 8, approval UI renders diffs. Residual risk: injected
   content can still shape *proposals* — the human gate is the final
   control; the UI must make injected-looking proposals visible, not
   pretty.
3. **Write credential theft = mind poisoning** (C13) → split RO/RW
   credentials (§4), RW only in worker, commit notifications.
4. **Public surface area** (C14): only REST + MCP + webhook are public.
   The ops UI (feed approval! settings!) sits behind Tailscale or
   Cloudflare Access. API keys stored **hashed** (verify template does
   this; fix if not), per-key rate limits live from M1, timing-safe HMAC +
   delivery dedupe on the webhook, fail2ban + key-only SSH baseline.
5. **Secrets on one host** (C15): Fernet-at-rest protects DB dumps, not a
   compromised host — stated honestly. Dedicated workspace-scoped
   Anthropic key + console spend limit. Chat/proposals/SDK transcripts
   contain private content in plaintext on disk: retention jobs, encrypted
   offsite backups, and the documented truth that `private` notes are only
   as private as the VPS.

## 10. Ops

- **Backups** (C17): nightly `pg_dump`, encrypted, offsite (B2/S3), with a
  tested restore path. Rebuildable from repo: Entity, SyncRun, snapshots.
  **Not rebuildable** (primary data): Feeds, Events, SdkOperations, Chat.
- Coolify: compose resource; named volumes for clone + snapshots +
  postgres; volume-rename safety = idempotent bootstrap (§4); env
  empty-string guards everywhere.
- Monitoring: health endpoint asserts DB + clone HEAD + last sync age +
  circuit-breaker state; dashboard tiles mirror it.

## 11. Build sequence — step by step

Each step small, verifiable, committed on its own. DONE = check passes.

### M0 — Prepare the brain repo (in `my-brain`)
- **0.1** Extend `.gitignore` (`.vscode/`, OS junk); verify no local paths
  or secrets get staged (`.vscode/settings.json` embeds a machine path —
  stays untracked). *Check: `git status` staging list reviewed.*
- **0.2** Commit the full contract: `CLAUDE.md`, `.claude/skills/` (both),
  `lenses/`, `eval/`, all `_TEMPLATE.md`, `README.md`, catalog files.
  (Grill B1: today even CLAUDE.md and the only lens are untracked.)
  *Check: fresh clone elsewhere contains everything the server needs.*
- **0.3** Contract amendments, one commit: per-directory visibility default
  map + inline `(agents-only:)` span convention written into CLAUDE.md §4;
  lens metadata promoted to frontmatter. Resolved decisions: `PENDING.md`
  stays as-is — it is the feeder's workbench by design ("not part of the
  mind" per its own header) — classified **workbench/admin-only**, excluded
  from all snapshots, rendered as an "open items" panel in the ops UI.
  `voice.md` stays **agents-only** (it's an agent instruction manual with
  strategy + partially unapproved distillations); a distilled public voice
  subset is deferred until a public clone is actually built — until then
  public tier serves no voice file. *Check: every tracked file resolves to
  a tier via frontmatter or the map; nothing unclassified.*
- **0.4** Server-mode preambles for feeder/reader added under
  `.claude/skills/*/server-mode.md`. *Check: reviewed against §7 rules.*
- **0.5** Private GitHub repo, push `main`; create **read-only deploy key**
  + **fine-grained write PAT** (contents:write, this repo only); enable
  commit email notifications. *Check: clone with RO key works; push with
  RO key fails; push with PAT works.*

### M1 — Read-only online brain
- **1.1** Scaffold fresh Django project; vendor `core`/`api_keys`/
  `mcp_proxy` from the template; strip demo endpoints; wire deny-anonymous
  into the REST path. *Check: unauthenticated REST call → 401 even at
  credits_cost 0; `make stack-up`-equivalent boots; /healthz green.*
- **1.2** Theme: base templates on the lwh tokens. *Check: paper/ink/indigo
  renders; dark log pane style present.*
- **1.3** Git layer: bootstrap clone (RO key), cross-container flock,
  pull, volume ownership init. *Check: empty volume → boots cloned;
  restart reuses; lock verified across two containers.*
- **1.4** Entity model + per-kind parsers + `rebuild_index`. *Check: counts
  match repo; templates/non-entities excluded; all five frontmatter
  dialects parse; voice.md indexes as agents-only.*
- **1.5** Visibility resolution + line-span stripper + **snapshot builder**
  → `/data/brain-views/{tier}/` incl. generated INDEX. *Check: public
  snapshot contains no agents-only/private file, no stripped spans, no
  PENDING.md; generated index matches Entity table.*
- **1.6** Sync: webhook (HMAC, dedupe, SHA no-op) + beat pull + SyncRun +
  drift check/repair + events. *Check: local push → server reindexed in
  seconds; corrupted DB row → drift event + self-repair; echo push → no-op.*
- **1.7** Consumers: key model + tier + per-key rate limit + hashed
  storage. *Check: tier and limit enforced in tests.*
- **1.8** Dumb endpoints (`get-index`, `list-notes`, `get-note`,
  `get-lens`, `get-identity`, `get-raw`-via-links). *Check: REST + MCP +
  docs live; per-endpoint tier tests incl. raw inheritance.*
- **1.9** `brainconfig`: encrypted AppSetting + Settings page + SdkRunner
  skeleton + **Test connection** (SdkOperation logged, row-before-run).
  *Check: bad key → clean error row; good key → model/latency/tokens.*
- **1.10** Dashboard v1 + brain browser. *Check: every entity reachable;
  staleness flags correct.*
- **1.11** Deploy to Coolify; ops UI behind Tailscale/CF Access; egress
  firewall; fail2ban/SSH baseline; nightly pg_dump job. *Check: public
  domain serves REST/MCP only; UI unreachable from open internet;
  restore-from-backup drill passes.*

### M2 — Write path
- **2.1** Feed model + `propose-feed` (rate-limited) + UI form; fetch of
  URLs/transcripts happens here, in trusted code. *Check: all three
  channels land pending Feeds; repo untouched; payload cap tested.*
- **2.2** Feeder agent: server-mode prompt, JSON-schema output, minimum
  snapshot, no network/Bash; worker execution; SdkOperation logged.
  *Check: real blog post → 2–4 schema-valid proposed notes; adversarial
  injected page → proposal contains no private content and no tool-denial
  garbage.*
- **2.3** Validator rules 1–8 as pure functions. *Check: each rule rejects
  a crafted bad proposal; rule 7 catches a status/visibility mismatch;
  rule 8 catches quoted private content.*
- **2.4** Approval queue UI (diff view, edit → re-validate, reject).
  *Check: edit path re-validates before approve enables.*
- **2.5** Approval handler: locked sequence + push-reject retry ×3 +
  rollback + PAT-only-in-worker. *Check: kill push mid-flight → clean
  tree, Feed failed after retries; race with a local push → auto-recovers.*
- **2.6** Round-trip: online feed → local pull; local feed → online
  browser. *Check: both directions; supersede test with a synthetic
  superseded pair (repo has zero real ones — B14).*

### M3 — Smart reader + chat
- **3.1** ASGI switch (uvicorn) + in-process streaming SSE plumbing.
  *Check: token stream visible in browser; sync endpoints unaffected.*
- **3.2** `assemble-context`: reader agent on caller-tier snapshot;
  budgets + circuit breaker live. *Check: public vs private tier packs
  differ correctly; breaker trips at cap; /proc + URL adversarial tests
  fail closed.*
- **3.3** Chat: sessions, tier switcher, sources panel, per-message
  tokens; transcript retention job. *Check: Qissatuna question in public
  mode → nothing private; session resume works or is explicitly ephemeral.*
- **3.4** Dashboards: event stream, ledger aggregates, most-served,
  spend-vs-cap. *Check: numbers reconcile with raw queries.*

### M3.5 — Brain visuals (the mind, visible) ✅ DONE 2026-07-30
One shared data source, four views, staged by when their data exists.
Behind ops login; a public showcase mode is a post-M5 option, not now.
JS lib vendored/self-hosted — Cytoscape 3.34.0 (MIT) for the force
layout only; rings and timeline are plain SVG. No CDN.

- **3.5.1** ✅ `GET /ops/graph.json` off Entity + Events: entity nodes
  (kind, tier, status, topics, staleness, read-count) + **topic hub
  nodes**, edges typed topic/project/superseded/source. Topics are hubs
  rather than pairwise links — 124 edges instead of ~590, and the force
  layout gets real cluster centres. `?days=N` scopes the read counts.
  Also carries resolved **lens definitions** (see 3.5.3).
  *Checked: 12/12 counts reconcile with independent raw ORM queries.*
- **3.5.2** ✅ Visibility rings — concentric private/agents-only/public
  dashboard centrepiece; dots by kind, sized by read-count, hollow when
  superseded. The signature visual (tiers are enforced here, not
  decorative). No library: polar layout of ~50 circles is plain SVG.
  *Checked in-browser: all 49 entities in their resolved ring; flipping
  a note to private moved its dot on the next reindex.*
- **3.5.3** ✅ Graph explorer (`/ops/graph/`) — cose force layout,
  isolated entities parked in a labelled column so they don't squash the
  core; lens picker highlights its subgraph; click-through to the note.
  **Lens membership is resolved server-side** (`graph.lens_definitions`)
  from the lens file's `topics`/`types`/`visibility-ceiling`/`identity`,
  so the picture and any future reader-side lens filter are one
  computation. *Checked: highlight == a membership set recomputed
  independently from the lens frontmatter, 35/35, nothing above ceiling.*
- **3.5.4** ✅ Live activity overlay — `GET /ops/activity.json` is a
  **cursor** over the read log (`?after=<id>`), polled every 4s and
  re-broadcast as a `brain:reads` DOM event; rings ping, graph nodes
  flash, ticker names endpoint/tier/consumer. *Checked: one real chat
  run lit exactly its 7 sources-panel entities, in both visuals.*
- **3.5.5** ✅ Belief timeline (`/ops/timeline/`) — growth per month
  stacked by kind with a running total, plus every supersede chain as
  position-over-time. *Checked: bars reconcile with an independent
  recount; a synthetic pair fed through the clone rendered as
  hollow→arrow→filled; zero chains degrades to an explanatory state.*

### M4 — Prove accuracy, migrate consumers
Runs AFTER the M5 beta release (decided 2026-07-30) — the eval happens
in public, build-in-public style. Sequence: M3 → M3.5 (.1–.3) → M5 →
M4, with 3.5.4–.5 landing whenever their data exists.

- **4.1** Build the eval it needs (grill B10 — `eval/` is one manual
  protocol, no dataset): assemble the 15-post X-reply set + a harness that
  runs local-Claude-Code vs `assemble-context` blind; Hasan scores.
  *Check: comparison run completed; no server-attributable regressions.*
- **4.2** Point My-Agents-Team + pipelines at MCP/REST with agents-only
  keys. *Check: one real task per consumer from online context only.*
- **4.3** Feed this app's project card into the brain (via mind-feeder,
  gated as always). *Check: card indexed and served.*

### M5 — Open-source beta (decided 2026-07-30: after M3/M3.5, MIT)
The project goes public as **BrainOutside** (brainoutside.com), a
self-hosted "build your own brain" tool: single-user, single-brain by
design — multi-user is a different product. The running release
checklist and the setup/deployment design doc are internal working
notes (untracked since 2026-08-08; they exist in older history).
De-Hasanify happened continuously, not as a big-bang at M5.

- **5.1** De-Hasanify the engine: commit identity/email → setting, theme
  naming generic, no personal URLs/paths in code. *Check: grep for
  `learnwithhasan`/`hassancs91`/`lwh` finds only docs and history.*
- **5.2** Public `brain-template` repo: folder skeleton, generalized
  CLAUDE.md contract, `_TEMPLATE.md`s, both skills, one example lens,
  placeholder identity files. The app's startup contract check is the
  template validator. *Check: fresh template clone boots the app clean.*
- **5.3** ✅ **DONE 2026-07-31.** First-run wizard: create admin → connect
  brain repo → paste Anthropic key OR subscription token (`sk-ant-oat` —
  the no-API-billing path is a headline feature) → bootstrap.
  *Checked: a blank instance running `config.settings.prod` with only
  `POSTGRES_PASSWORD` + `ALLOWED_HOSTS` set was driven through the whole
  wizard in a real browser to 49 indexed entities and a dashboard with
  the rings drawn — no terminal, no DEPLOY.md.* Boot secrets generate
  once and persist (cross-container race verified with 6 simultaneous
  containers); git credentials moved to encrypted settings with env/file
  override; `bootstrap()` now refuses a clone whose origin isn't the
  configured repo; `/ops/health/` reports and repairs.
  **One release blocker found and NOT fixed here:** the enforced CSP
  strips inline style attributes, so the ops UI is largely unstyled on a
  real deployment — dev runs CSP report-only, which hid it. (Resolved by
  5.6: every inline style attribute is gone and the strict CSP ships.)
- **5.6** **UI rewrite — runs BEFORE 5.4/5.5** (decided 2026-07-31).
  Restore a real Tailwind build (v4,
  standalone CLI, no Node in the repo), wire `tokens.css` into `@theme`
  so the semantic tokens become utilities, build a component layer, then
  redesign all ~13 ops pages and delete all 455 inline `style="…"`
  attributes. Launching with the better UI means the launch assets and
  docs screenshots get made once, against what ships.
  *Check: zero inline styles in templates; `tw.css` regenerable from
  source; every page verified in a real browser under ENFORCED CSP.*

  This is also the fix for the release blocker below — the enforced CSP
  strips inline style attributes, so the ops UI is largely unstyled on
  any real deployment, and `dev.py`'s report-only CSP hid it from every
  visual check this project has done. The rewrite removes the need to
  loosen CSP at all; the interim `style-src-attr` band-aid is explicitly
  NOT applied.

- **5.4** Cross-platform: bash/Makefile twin of `dev.ps1`; README with
  the rings hero GIF; INSTALL; honest SECURITY posture (§9 truths).
  *Check: setup succeeds on a clean Linux/macOS box.*
- **5.5** History audit — if anything personal ever landed in git
  history, publish with fresh history from a chosen commit. LICENSE
  (MIT) + CONTRIBUTING. *Check: public repo contains no personal data,
  no secrets, in any commit.*

## 12. Grill log (what changed and why)

| # | Finding (source) | Resolution |
|---|---|---|
| 1 | Anonymous REST reads at credits_cost=0 (A1) | Vendored core + deny-null-principal by construction (§1, §5) |
| 2 | Reader agent bypasses visibility; tier was a UI label (C11) | Per-tier materialized snapshots (§5) |
| 3 | Prompt injection via fed content → exfiltration (C12) | No-network agents, fetch-in-trusted-code, egress firewall, validator rule 8, diff-rendered approvals (§7, §9) |
| 4 | `allowed_tools` doesn't confine; cwd doesn't sandbox (C4) | Scoped allow rules + name-deny + setting_sources=[] + hooks + CI adversarial tests (§7) |
| 5 | Contract files (CLAUDE.md, skills, lens, eval) untracked (B1) | M0.2 explicit commit list + loud startup check (§4, §11) |
| 6 | PENDING.md: tracked, unclassified, non-public content (B2) | Deny-by-default for unclassified + M0.3 review (§5) |
| 7 | No visibility default for undeclared files; both naive defaults wrong (B3/B4) | Per-directory default map committed into CLAUDE.md (§5) |
| 8 | voice.md agents-only vs INDEX says nothing (B5); line-level `(agents-only:)` spans unhandled (B6) | Frontmatter authoritative; span stripper; get-identity tier-filtered; public-voice decision in M0.3 (§5) |
| 9 | INDEX.md not machine-parseable, self-violating format (B7) | Entity from frontmatter only; INDEX becomes generated view; validator rule 7 checks agreement (§3, §4) |
| 10 | Skills are interactive; headless reuse misbehaves (B9/C8) | Server-mode preambles, in-repo, versioned; divergence covered by M4 eval (§7) |
| 11 | MAX_TOKENS_PER_OP knob doesn't exist (C5) | max_budget_usd + max_turns + kill-timeout + daily circuit breaker + console limit (§7) |
| 12 | Worker→SSE streaming was hand-waving (A2/C10) | In-process async SSE under ASGI; worker only for feeds (§7) |
| 13 | Rate limits per-user not per-key; demo plan seeds 30/min; magic-link login (A3/A6/A7) | Vendored core: own throttle, own auth (§1) |
| 14 | No git/Node in image; 3 containers share the clone; non-root volume (A5/C1) | §7 runtime + §4 volume/lock design |
| 15 | RW deploy key = poisoning credential (C13) | RO key + worker-only fine-grained PAT + notifications (§4) |
| 16 | Feeds/Events/Chat are primary data, no backups (C17) | Nightly encrypted offsite pg_dump + restore drill (§10) |
| 17 | Push reject = routine race treated as failure (C20) | Locked retry ×3, replay-safe proposals (§4) |
| 18 | eval/ is one manual protocol, no dataset (B10) | M4.1 builds dataset + harness first (§11) |
| 19 | get_raw browsable, raw has no visibility (B11) | Link-resolved raw, inherits note tier (§5, §6) |
| 20 | cost_usd is a client estimate; usage nullable on errors (C6/C23) | Tokens canonical, cost display-only, row-before-run (§3) |

Minor findings (slugs/POST-only, payload caps, ack-timeout, session
transcripts, provenance links, template poisoning of drift hash, author
identity, webhook echo, Coolify volume rename, outage mode) are folded
into §§3–11 where they land.

## 13. Open questions

1. ~~§1 base decision~~ — **resolved 2026-07-28: vendored-core** ("we don't
   need all features anyway").
2. Ops-UI boundary: Tailscale vs Cloudflare Access (both fine; pick per
   existing setup).
3. ~~Public-voice subset~~ — resolved: deferred until a public clone exists
   (see M0.3).
4. Postgres stays (settled — worker queue + events benefit; SQLite dropped).
5. ~~Open source?~~ — **resolved 2026-07-30: yes, MIT, beta release after
   M3/M3.5 (M5); M4 eval runs in public. Visuals: all four M3.5 views,
   staged.**
