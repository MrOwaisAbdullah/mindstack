# Installing BrainOutside

One image, five containers (`web`, `mcp`, `worker`, `postgres`,
`redis`), one compose file. **Two environment variables are required**
— `POSTGRES_PASSWORD` and `ALLOWED_HOSTS` — everything else either
generates itself on first boot or is configured in the browser at
`/setup`.

You need: somewhere to run Docker, a GitHub account (for your brain
repo), and a Claude credential — an Anthropic API key **or** a Claude
subscription token (`sk-ant-oat`), so no API billing is required.

## The happy path

```sh
git clone https://github.com/hassancs91/brainoutside.git && cd brainoutside
cp .env.example .env    # fill the two REQUIRED lines at the top
printf 'services: {web: {ports: ["127.0.0.1:8000:8000"]}}\n' > docker-compose.override.yml
docker compose up -d --build          # first build takes a few minutes
# point your TLS proxy at 127.0.0.1:8000, open https://your-domain/
# → it redirects to /setup, and the wizard does the rest. No terminal again.
```

The override file exists because `web` deliberately publishes **no**
port — a bare `ports: ["8000"]` would expose plain HTTP to anyone who
scans the host. Bind it to loopback and put your TLS proxy (Caddy,
nginx, Traefik…) in front; the proxy must forward `X-Forwarded-Proto`,
or set `SECURE_SSL_REDIRECT_ENABLED=0` and accept plain HTTP on a
network you trust.

## On Coolify

Even shorter — Coolify's proxy replaces the override file and the TLS
setup. The full runbook, including the proxy/CDN client-IP
configuration and the Coolify empty-env-var trap, is
[docs/DEPLOY.md](DEPLOY.md).

## After the wizard

- **Lock down `/ops/…`** — it can approve writes and read every private
  note. Network boundary options and the reasoning:
  [docs/SECURITY.md](SECURITY.md).
- **Wire the GitHub webhook** (repo → Webhooks →
  `https://your-domain/webhooks/github`, JSON, push events, secret =
  `GITHUB_WEBHOOK_SECRET`). Pushes then reindex in seconds; without it
  the 15-minute sync beat is your only freshness.
- **Back up** the Postgres database and the `brain-state` volume —
  losing `boot-secrets.json` makes every stored credential unreadable.
  Commands in [DEPLOY.md §6](DEPLOY.md#6-backups).

## Updating

```sh
git pull && docker compose up -d --build
```

Migrations run automatically on boot. Tagged releases publish
`ghcr.io/<owner>/brainoutside` (amd64 + arm64) — pinning to a tag
instead of building from source is supported from the first beta tag.

## Local development

`./dev.sh` (macOS/Linux) or `.\dev.ps1` (Windows) runs the same
containers with the repo bind-mounted and ports published on
localhost. See [CONTRIBUTING.md](../CONTRIBUTING.md).
