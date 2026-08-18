# MCP setup

This server speaks the **Model Context Protocol** so every endpoint you
register also surfaces as a callable tool inside Claude (Desktop, Code,
or any MCP-aware client). One endpoint definition → one REST route + one
MCP tool, no duplicate code.

## First, mint a key

Every client below authenticates the same way: a bearer key from
[API keys]({{ OPS_KEYS_URL }}) → **Mint a key**. Give it the
`agents-only` tier unless you specifically want the client to reach
`private` notes. The secret is shown once.

> **Two ways in, depending on the client.** Clients that can send an
> `Authorization` header — Claude Desktop, Claude Code, Cursor — use a
> bearer key, as below. Clients that cannot, notably **claude.ai on web
> and mobile**, need a URL that carries its own credential: mint one on
> [Connectors]({{ OPS_CONNECTORS_URL }}), which issues a
> `/mcp/k/<token>/` address. That surface is off by default — set
> `MCP_URL_AUTH_ENABLED=true` and restart, or the minted URL will 404.
>
> OAuth / Dynamic Client Registration is not implemented, so a client
> that insists on the OAuth handshake will not connect.

## Claude Desktop

Claude Desktop only natively supports **stdio** MCP servers, so
connecting to a remote HTTP endpoint needs a small stdio shim. The
standard one is [`mcp-remote`](https://www.npmjs.com/package/mcp-remote).

Edit `claude_desktop_config.json` (location: `~/Library/Application
Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "this-api": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "{{ PUBLIC_BASE_URL }}/mcp/",
        "--header",
        "Authorization: Bearer mcpsk_your_key_here"
      ]
    }
  }
}
```

Restart Claude Desktop. Open a new conversation; the available tools
appear automatically — every endpoint registered with
`@endpoint(slug=...)` surfaces as a tool named after its slug (e.g.
`get-note`, `list-notes`, `assemble-context`).

## Claude Code

From your terminal:

```bash
claude mcp add this-api {{ PUBLIC_BASE_URL }}/mcp/ \
  --transport http \
  --header "Authorization: Bearer mcpsk_your_key_here"
```

This writes the server entry into `~/.claude.json` (user scope). Use
`claude mcp list` to verify, and `claude mcp remove this-api` to
detach. You can also point a session at an ad-hoc config with
`claude --mcp-config <file.json>`.

## Cursor

Cursor accepts remote MCP servers directly — no shim needed. Edit
`~/.cursor/mcp.json` (or the in-app settings):

```json
{
  "mcpServers": {
    "this-api": {
      "url": "{{ PUBLIC_BASE_URL }}/mcp/",
      "headers": {
        "Authorization": "Bearer mcpsk_your_key_here"
      }
    }
  }
}
```

Restart Cursor; tools appear under your configured server name.

## Authentication

Use the same `Authorization: Bearer mcpsk_<key>` token used for REST —
see the [auth guide](/docs/guide/auth/). There is no anonymous access on
either surface.

The key's **tier** applies to MCP exactly as it does to REST: a tool call
from an `agents-only` key cannot read a `private` note, and gets the same
"unknown entity" answer a non-existent id would. Tier and rate limit are
editable per key on the [API keys]({{ OPS_KEYS_URL }}) page and take
effect on the next call.

## Troubleshooting

  - **No tools show up**: open Claude Desktop devtools (Help →
    Developer Tools), check the MCP server log for connection errors.
    Most common cause: a wrong or revoked key. Confirm the key is still
    active on the [API keys]({{ OPS_KEYS_URL }}) page — its *last used*
    timestamp tells you whether the call ever arrived.
  - **Tools show but return errors**: check the event stream on
    [Logs]({{ OPS_LOGS_URL }}) for the failing call — status codes match
    the [error codes guide](/docs/guide/errors/).
  - **A note you know exists comes back "unknown entity"**: the key's
    tier is below that note's visibility. Raise the tier on its card.
  - **Tool name ends with `__v2` / `__v3`**: the endpoint ships multiple
    versions and the suffix marks the non-default one. The unsuffixed
    name is the stable v1; pick the suffixed version only if you
    specifically need its behavior.

## Local development

When developing endpoints locally, point your MCP client at
`http://localhost:8000/mcp/` (note: `http`, not `https`). Restart the
client after registering a new endpoint — Claude reads the tool list
once at connection time.
