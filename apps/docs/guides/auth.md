# Authentication

Every `/api/v1/*` call is authenticated with an **API key** sent in the
`Authorization` header. Keys are minted in the ops console at
[API keys]({{ OPS_KEYS_URL }}) and look like `mcpsk_<~43 url-safe
characters>` (256 bits of entropy).

**There is no anonymous tier.** This server fronts a private knowledge
base, so every endpoint requires a valid key — including the ones that
cost nothing to call. An unauthenticated request gets `401
auth_required`, never a partial answer.

Each key also carries a **tier** (`public`, `agents-only`, `private`)
that caps the most sensitive note it can read, and its own rate limit.
Both are set when you mint the key and editable afterwards; a key with
no profile is treated as `public`.

## Sending the header

```bash
curl -X POST {{ PUBLIC_BASE_URL }}/api/v1/ping \
  -H "Authorization: Bearer mcpsk_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Python:

```python
import requests

r = requests.post(
    "{{ PUBLIC_BASE_URL }}/api/v1/ping",
    headers={"Authorization": "Bearer mcpsk_your_key_here"},
    json={},
)
r.raise_for_status()
print(r.json())
```

JavaScript:

```javascript
const r = await fetch("{{ PUBLIC_BASE_URL }}/api/v1/ping", {
  method: "POST",
  headers: {
    "Authorization": "Bearer mcpsk_your_key_here",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({}),
});
if (!r.ok) throw new Error(`HTTP ${r.status}`);
console.log(await r.json());
```

## Key lifecycle

Keys are SHA-256 hashed at rest — we never store the plaintext. **The
secret is shown exactly once at creation.** If you lose it, revoke the
key and mint a new one.

  - Mint a new key: ops console → **API keys** → **Mint a key**
  - Rotate a key: **Rotate** — mints a replacement carrying the same tier
    and rate limit, then revokes the old one in the same transaction
  - Revoke a key: **Revoke** (instant; in-flight calls finish, new calls
    return `401 invalid_credential`)
  - See per-key usage: last-used timestamp, source IP and 7-day read count
    are on the key's card

## Failure modes

| Status | Code | Cause |
|---|---|---|
| `401` | `auth_required` | No `Authorization` header — on any endpoint |
| `401` | `invalid_credential` | Bad / unknown / revoked / expired key |
| `422` | `input_validation_error` | `unknown entity: <id>` also means "exists, but above your key's tier" — the two are deliberately indistinguishable |
| `429` | `rate_limit_exceeded` | Over the key's requests-per-minute limit, or too many failed auth attempts on its prefix. `Retry-After` is set |

See the [error codes guide](/docs/guide/errors/) for the full response
body shape and the complete error catalog.

## Best practices

  - **One key per client.** A separate key for Claude Desktop, for
    claude.ai, and for each script. If one leaks you revoke exactly one,
    and the last-used timestamp on each card tells you which is which.
  - **Grant the lowest tier that works.** `agents-only` is the normal
    choice for an assistant you run yourself; `private` should go only
    to a client you fully control.
  - **Rotate periodically.** **Rotate** carries the tier and limit onto
    the replacement, so the only thing you have to change is the secret
    in your client.
  - **Never put keys in client-side JS.** Keys belong on a server
    you trust. Browser-side calls leak your secret to anyone who
    opens devtools.
  - **Don't commit keys to git.** Use `.env` files in `.gitignore`,
    or your platform's secrets manager.
