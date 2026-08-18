# Deploying BrainOutside to Coolify

Verified 2026-07-31 against a blank `config.settings.prod` instance.

**Two environment variables are required.** Everything else either
generates itself on first boot or is configured in the browser at
`/setup`. If you have an older copy of this file demanding `SECRET_KEY`,
`FIELD_ENCRYPTION_KEY` and `OAUTH_ISSUER`, that predates M5.3 — those are
now generated and persisted for you.

---

## 1. Before you start

- A **git remote Coolify can pull from** (this repo). Coolify clones it
  and builds the image itself; there is no published image yet.
- A **brain repository** — your notes. Start from `brain-template`. It
  can be empty of notes but must carry the contract files (`CLAUDE.md`,
  `.claude/skills/`, `lenses/`), or the server refuses to serve it and
  says so.
- A **domain** pointed at the VPS, with Coolify terminating TLS.
- A **Claude credential**: an Anthropic API key, or an `sk-ant-oat`
  subscription token. Pasted into the wizard, never into the environment.

## 2. Create the resource

Coolify → **New Resource → Docker Compose**, point it at this repo and
`docker-compose.yml`. Set the domain on the `web` service (port 8000).

**Environment variables — these two, and no others are required:**

| Var | Value |
|---|---|
| `POSTGRES_PASSWORD` | any long random string |
| `ALLOWED_HOSTS` | your domain, e.g. `brain.example.com` — no scheme, comma-separated for several |

> **Coolify trap:** an env var set to an EMPTY STRING is treated as unset
> by this app, deliberately. Delete the line rather than blanking it.

`ALLOWED_HOSTS` stays in the environment rather than the UI on purpose:
getting it wrong 400s every request *including the page that would let
you fix it*.

### Generated for you on first boot

`SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, `MCP_LOOPBACK_SECRET` and
`DJANGO_ADMIN_URL_PATH` are created once and written to
`$BRAIN_STATE_DIR/boot-secrets.json` on the `brain-state` volume.
Setting any of them yourself still wins.

> **Back up that volume.** Losing `FIELD_ENCRYPTION_KEY` makes every
> stored credential permanently unreadable — the database survives and
> the secrets in it do not.

`PUBLIC_BASE_URL` and `OAUTH_ISSUER` derive from `ALLOWED_HOSTS`.

### Worth setting

| Var | Why |
|---|---|
| `ADMIN_IP_ALLOWLIST` | CIDRs allowed to reach `/ops/…`. Empty = no IP check. See §5. |
| `APP_NAME` | Branding in the tab, landing page and login. Also settable on `/ops/settings/`, which wins over this — set it here only to brand the very first boot. |
| `BRAIN_COMMIT_NAME` / `_EMAIL` | Author on approval commits, so server writes are distinguishable from yours. |

`SECURE_SSL_REDIRECT_ENABLED` defaults to on and is correct behind
Coolify, which terminates TLS and sets `X-Forwarded-Proto`. Only set it
to `0` if you are running without a TLS-terminating proxy — otherwise
every request 301s to https and you cannot reach the wizard.

## 3. First boot

The web container runs migrations and `collectstatic`, then serves. It
becomes healthy on `/healthz`; `/readyz` deliberately reports **503**
until a valid brain is cloned, so do not use `/readyz` as the health
check — a blank instance would never come up.

Then open `https://your-domain/`. It redirects to `/setup/`.

## 4. The wizard

Six steps, no terminal, no `docker exec`:

1. **Create your account** — the single operator account. **Do this
   immediately:** until it exists, anyone who can reach the server can
   create it. The page says so, in red.
2. **Create your brain** — point at your brain repository.
3. **Let the server read it** — the app generates an ed25519 keypair and
   shows you the public half. Add it to the repo as a deploy key
   **without write access**, then press Verify. Failures show git's own
   message, unedited.
4. **Let the server write back** — a fine-grained PAT with
   contents:read+write on that one repo. Separate from the read key on
   purpose: a stolen read key leaks your brain, a stolen write key
   rewrites it. Skippable; approvals then commit locally without pushing.
5. **Connect Claude** — API key or `sk-ant-oat` subscription token.
6. **Build your brain** — clone, index, and materialise one snapshot per
   visibility tier. Runs on the worker; safe to close the tab.

**On step 2's "generate from template" link:** it works.
`brainoutside-template` was published on 2026-08-02 — public,
`is_template: true`, and the `/generate` deep link returns 200. Note
that GitHub creates the repo *asynchronously*: for a moment after you
click Create, the repo exists but is empty (`size: 0`), and a server
pointed at it in that window clones nothing. Measured at ~0.7s. If Read
→ Verify says the repo is not a brain yet, wait a second and press it
again.

## 5. Lock down the ops UI

`/ops/…` can approve feeds and read every private note. It is staff-only,
but put a network boundary in front of it too:

- **Tailscale** on the VPS + `ADMIN_IP_ALLOWLIST` set to the tailnet
  range (the middleware 404s everyone else), or
- a **Cloudflare Access** rule covering `/docs/*`, `/ops/*`, `/setup/*`,
  `/login/*` and the generated admin slug.

Public internet should reach only `/`, `/api/`, `/mcp`,
`/webhooks/github` and `/healthz|/readyz`. Putting Access in front of
those breaks your MCP consumers and silently kills the webhook. The
dashboard warns when the ops UI is unrestricted and reachable from a
non-local address.

### If you are behind a CDN or reverse proxy, do this first

Coolify fronts the app with Traefik, and most deploys add Cloudflare on
top. In that shape `REMOTE_ADDR` is the proxy, not the caller — so every
per-IP control lumps the whole internet into one bucket. That is not
cosmetic: the admin-login lockout is per-IP, so an attacker's failed
attempts trip the sentinel on the shared address and **lock you out of
your own admin** while they continue from another edge node.

Set both:

```
TRUSTED_PROXY_IP_HEADER=CF-Connecting-IP
TRUSTED_PROXY_IPS=10.0.0.0/8
```

`TRUSTED_PROXY_IPS` is the address the proxy connects **from**, matched
against `REMOTE_ADDR` — the hop adjacent to this app. Behind Coolify that
is the Traefik container on the Docker network (a private address), *not*
Cloudflare's published ranges. Don't guess it: open `/ops/health/` and
the exposure panel prints the address it actually observes, plus which
of the three states you are in.

The header is trusted only on requests whose peer is in that list, so a
caller reaching the origin directly cannot forge it. Setting one without
the other refuses to boot in prod, because a half-configuration looks
identical to a working one while every per-IP control stays inert.

**`ADMIN_IP_ALLOWLIST` compares against the resolved address**, so
configure this section before setting it — otherwise you are allowlisting
against your proxy's address and the first request 404s you out.

## 6. Backups

Nightly on the host:

```sh
docker exec <postgres> pg_dump -U brain brain | gzip > /backups/brain-$(date +%F).sql.gz
docker run --rm -v <project>_brain-state:/s -v /backups:/b alpine \
  tar czf /b/brain-state-$(date +%F).tgz -C /s .
```

Rebuildable from your repo: entities, sync runs, snapshots. **Not**
rebuildable: feeds, events, SDK ledger, chat history — and
`boot-secrets.json`, which is why the state volume is in the list.

## 7. Webhook (the fast path)

Repo → Webhooks → `https://your-domain/webhooks/github`, content type
JSON, secret = `GITHUB_WEBHOOK_SECRET`, push events only.

With the webhook, a push to your brain repo reindexes in seconds.
Without it, the **15-minute sync beat** picks changes up — the
`brain:sync` schedule in `config/scheduled.py`, registered on every
deploy by the entrypoint's `sync_scheduled` run. (An earlier version of
this page said there was no periodic pull; that was true at the time
and is not any more.) So the webhook is a latency upgrade, not a
correctness requirement — but wire it anyway: fifteen minutes is a long
time to wonder why your new note isn't being served.

## 8. Post-deploy checks

- [ ] `/healthz` 200 immediately; `/readyz` 200 after the wizard finishes
- [ ] `/ops/` unreachable from the open internet, reachable through your
      boundary
- [ ] Settings → Test connection returns model/latency/tokens
- [ ] Push to your brain repo → the server reindexes within seconds.
      If it takes minutes instead, the webhook is not wired and the
      15-minute beat is doing the work; see §7.
- [ ] Restore-from-backup drill into a scratch Postgres, `/readyz` green
