# Error codes

Every API failure returns a JSON body with this shape:

```json
{
  "error": {
    "code": "<machine_code>",
    "message": "<human-readable explanation>"
  }
}
```

Some errors include extra fields alongside `code` + `message` —
`retry_after_s` on a 429, `errors` on an input-validation failure,
etc. Those are documented per-code below.

The `X-Request-ID` **response header** carries a stable id that
correlates the failure with your server's logs. Grep for it in
`docker compose logs web` to find the matching row.

Match on `error.code` (stable across versions) and surface
`error.message` to your users.

## 4xx — client errors

| Status | `error.code` | What happened | Fix |
|---|---|---|---|
| `400` | `invalid_json` | Body wasn't valid JSON | Verify your JSON encoder; common cause is unescaped quotes or trailing commas |
| `400` | `idempotency_key_invalid` | `Idempotency-Key` header is malformed (too long, non-printable, or empty) | Use a printable string up to 255 chars (UUIDs are ideal) |
| `401` | `auth_required` | No `Authorization` header | Add `Authorization: Bearer mcpsk_<key>`; see the [auth guide](/docs/guide/auth/) |
| `401` | `invalid_credential` | Bad, unknown, revoked or expired key | Mint a fresh one on `/ops/consumers/` |
| `404` | `not_found` | Resource referenced by id doesn't exist (or doesn't belong to you) — used by the `/api/v1/_jobs/<id>` poll endpoint | Verify the id, or wait for the job to be created |
| `409` | `idempotency_request_in_flight` | A previous call with the same `Idempotency-Key` is still executing | Wait and retry; do not start a new call concurrently |
| `422` | `input_validation_error` | Input failed Pydantic validation | Inspect the `errors` array on the response (Pydantic's standard shape: `loc`, `msg`, `type`, `input`) |
| `422` | `idempotency_key_mismatch` | Same `Idempotency-Key` reused with a different request body | Either retry the original body or use a fresh idempotency key |
| `429` | `rate_limit_exceeded` | Per-minute bucket exhausted | Read the `Retry-After` header (also `retry_after_s` in the body); back off + retry. See [rate limits guide](/docs/guide/rate-limits/) |

## 5xx — server errors

| Status | `error.code` | What happened | What to do |
|---|---|---|---|
| `500` | `internal_error` | The endpoint raised. Nothing is notified automatically — the traceback is in the server log | Find the `X-Request-ID` in `docker compose logs web` |
| `503` | `endpoint_disabled` | This endpoint was switched off on the server | Re-enable it in the Endpoints card on `/ops/settings/` |

## Validation-error body shape

A `422 input_validation_error` carries the underlying Pydantic
diagnostics in an `errors` array:

```json
{
  "error": {
    "code": "input_validation_error",
    "message": "Input validation failed.",
    "errors": [
      {
        "type": "string_too_long",
        "loc": ["url"],
        "msg": "String should have at most 500 characters",
        "input": "https://example.com/..."
      }
    ]
  }
}
```

Walk `errors[*].loc` to map each entry back to the offending input
field; surface `msg` to your end user.

## Special cases

  - **MCP tool errors**: when called via MCP (not REST), failures
    surface as MCP-protocol tool errors with the exception's text as
    the error message — not as a `{"error": {...}}` JSON payload. The
    machine code isn't preserved across the protocol boundary. If you
    need the structured code, call the endpoint via REST.
  - **This server sends no webhooks**, so there are no delivery
    failures to surface. The only webhook is GitHub's inbound push hook
    — see the [webhooks guide](/docs/guide/webhooks/).

## When in doubt

Open `/ops/logs/` — every call is recorded there with its status and the
entities it served. Denied calls show up as `auth_denied` events with the
reason.

For anything that isn't in the log, the server's own output has it:

```bash
docker compose logs web --tail 200
```
