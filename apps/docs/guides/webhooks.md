# Webhooks

There is exactly one webhook here, and it points **inward**: GitHub tells
your server that the brain repo changed, and the server re-syncs.

This server does not send webhooks. Nothing subscribes to events, and
there is nothing to register a callback URL with.

## What it is for

The server keeps a clone of your brain repo and an index built from it.
Something has to tell it when you push. Two things can:

- the **GitHub push webhook** below — near-instant, and
- the **periodic sync beat**, which pulls every 15 minutes regardless.

The beat means the webhook is an optimisation, not a requirement. Skip
this whole page and your brain is at most 15 minutes stale.

## Setting it up

**1. Generate a secret** and save it on `/ops/settings/` as the GitHub
webhook secret. Any long random string works:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**2. In your brain repo on GitHub**, go to Settings → Webhooks → Add
webhook and set:

| Field | Value |
|---|---|
| Payload URL | `https://your-server.example.com/webhooks/github` |
| Content type | `application/json` |
| Secret | the string from step 1 |
| Events | Just the push event |

**3. Push something.** The delivery should show a `200`, and the sync
appears on `/ops/tasks/`.

## How the server verifies it

Every delivery is checked with HMAC-SHA256 over the raw body, compared
against the `X-Hub-Signature-256` header in constant time. A delivery that
fails is rejected and recorded as an `auth_denied` event.

**With no secret set, the endpoint is closed, not open.** An unset secret
disables the webhook entirely rather than accepting unsigned pushes — so
if you skip step 1, step 2 will return an error and the beat will do the
work instead.

Deliveries are de-duplicated on GitHub's `X-GitHub-Delivery` id, and a
push whose commit the server already has is acknowledged without doing
anything — which is what stops the server's own approval commits, made
when you approve a feed on `/ops/feeds/`, from triggering a redundant
sync.

## Troubleshooting

**403 on every delivery** — the secret in `/ops/settings/` and the one in
GitHub don't match, or none is set at all. Re-paste both; the stored one
can only be read back via the reveal button on `/ops/health/`.

**200s, but nothing syncs** — check `/ops/tasks/`. If the sync ran and
failed, the error is there. A common cause is the deploy key having lost
read access to the repo.

**Nothing arrives at all** — check GitHub's Recent Deliveries tab. If your
server isn't reachable from the internet (a home LAN, Tailscale-only),
GitHub cannot deliver and the beat is your sync path. That is a perfectly
normal way to run this.
