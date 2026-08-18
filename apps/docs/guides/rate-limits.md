# Rate limits

This is your server. Rate limiting here is not a sales tier — it exists so
one runaway agent can't hammer your brain, and so a leaked key has a
ceiling on the damage it does before you notice and revoke it.

## How limits are assigned

Every credential carries its own per-minute limit and its own counter.

| Caller | Bucket | Limit |
|---|---|---|
| API key (`mcpsk_…`) | per key | that key's `rate_limit_per_min`, default **60** |
| Connector URL (`mcpurl_…`) | per token | that token's own limit |
| You, signed in to the ops UI | — | not limited |
| Anonymous | per client IP | `ANONYMOUS_RATE_LIMIT_PER_MIN`, default **20** |

Minting a second key does not raise a limit — it creates a second,
independent one. That is the point: each consumer gets a budget you can
reason about and revoke on its own.

Set a key's limit when you create it on `/ops/consumers/`, or edit it
there afterwards. Connector URLs get theirs on `/ops/connectors/`.

There is no plan, no quota, and nothing to buy. Every endpoint on this
server costs zero credits.

## Being throttled

A request over the limit gets a `429` and is not executed:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 12
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
Content-Type: application/json

{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "per-key limit exceeded",
    "limit_per_min": 60,
    "retry_after_s": 12
  }
}
```

- `Retry-After` (seconds) — sleep at least this long before retrying.
- `X-RateLimit-Limit` — the bucket size for this credential.
- `X-RateLimit-Remaining` — always `0` on a 429.
- `error.retry_after_s` mirrors `Retry-After` for clients that find
  headers awkward to read.

`X-RateLimit-*` headers are sent **only on a 429**, so you cannot watch
your remaining budget drop in advance. Handle the 429 rather than trying
to avoid it.

## Backing off

Read `Retry-After` and sleep for at least that long:

```python
import time, requests

def call_with_backoff(url, headers, body, max_retries=5):
    for attempt in range(max_retries):
        r = requests.post(url, headers=headers, json=body)
        if r.status_code != 429:
            return r
        delay = int(r.headers.get("Retry-After", "1"))
        time.sleep(delay + 0.1)
    raise RuntimeError("rate-limit retries exhausted")
```

Counters reset on a fixed one-minute window, not a sliding one, so the
worst case is a full minute's wait.

## When Redis is down

Limits are counted in Redis so every worker process shares one view. Each
process probes Redis once at startup; a process that finds it unreachable
falls back to an in-memory counter **for its whole life**. The limit still
applies, but independently per worker, so an N-worker deployment allows
roughly N× the configured rate — and it stays that way until that process
restarts, even after Redis comes back.

If you suspect this, restart the app. The fallback is logged at boot
(`cache: Redis unreachable … falling back to LocMemCache`).

## Raising a limit

Edit the consumer on `/ops/consumers/` (or the token on
`/ops/connectors/`) and save. It takes effect on the next request — there
is nobody to ask.
