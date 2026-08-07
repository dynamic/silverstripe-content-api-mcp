# AGENTS.md

Machine-actionable setup runbook for `content-api-mcp`. This file is for an agent configuring
this server autonomously — exact commands, no prose to interpret. For human-facing
documentation, see [`README.md`](README.md) and [`docs/`](docs/index.md).

## Prerequisites (verify before proceeding)

- Python `>=3.11` available.
- `uv`/`uvx` on PATH (preferred), or `pip` + a venv.
- `GITHUB_TOKEN` or `GH_TOKEN` exported with read access to `dynamic/daisy-base` (private repo —
  the `mcp-base` dependency installs from it via git, not PyPI; install fails without this).
- A target SilverStripe site running `dynamic/silverstripe-content-api`, reachable over HTTPS,
  with the module's `/content-api/v1` route live.

## Step 1 — Mint an API token on the target site

```bash
ddev sake tasks:MintContentApiToken --email=<service-account-email>
```

(Drop `ddev` if the target isn't a DDEV project — use whatever `sake` invocation the project
uses.) Output includes the plaintext token on its own line — capture it now, it is shown once.
Note the printed expiry (`tokenLife` default 7 days).

## Step 2 — Store the token

This runbook defaults to a token file rather than the inline `CONTENT_API_TOKEN` shown as the
first example in the human-facing README/[Installation](docs/installation.md) — an autonomous
setup can't assume it's terminal-launched, so it's more conservative by default. It works
identically whether the MCP host is launched from a terminal or a GUI app:

```bash
mkdir -p ~/.config/content-api-mcp
printf '%s' '<minted-token>' > ~/.config/content-api-mcp/<site-identifier>.token
chmod 600 ~/.config/content-api-mcp/<site-identifier>.token
```

## Step 3 — Add the server to the MCP client config

Edit the project's `.mcp.json` (create it if absent):

```json
{
  "mcpServers": {
    "content-api": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/dynamic/silverstripe-content-api-mcp",
        "content-api-mcp"
      ],
      "env": {
        "CONTENT_API_BASE_URL": "https://<site>/content-api/v1",
        "CONTENT_API_TOKEN_FILE": "~/.config/content-api-mcp/<site-identifier>.token"
      }
    }
  }
}
```

Substitute the real site host in `CONTENT_API_BASE_URL` and the real path in
`CONTENT_API_TOKEN_FILE`. If the environment is guaranteed terminal-launched and a token file is
not wanted, `CONTENT_API_TOKEN: "<minted-token>"` inline is equivalent — but see Step 5 before
choosing that.

To pin a specific released version instead of `main` HEAD, append `@vX.Y.Z` to the `--from` git
URL (check [`CHANGELOG.md`](CHANGELOG.md) for the latest tag).

## Step 4 — Verify

```bash
curl -sf -H "X-Silverstripe-Apitoken: $(cat ~/.config/content-api-mcp/<site-identifier>.token)" \
  "https://<site>/content-api/v1/auth/session"
```

Expect HTTP 200 with a JSON body reporting the member and expiry. A non-200 here means the
token or site config is wrong — fix that before touching the MCP client, since the MCP server
adds no diagnostics beyond forwarding this same error.

Then start/restart the MCP client and confirm the `content-api` server is connected with 14
tools registered (`content_auth_session`, `content_schema_site`, `content_schema_class`,
`content_records_list`, `content_records_read`, `content_records_parity`, `content_fingerprint`,
`content_records_stage`, `content_batch`, `content_compose_page`, `content_asset_upload`,
`content_asset_read`, `content_page_convert`, `content_page_apply_template`).

## Step 5 — Environment variable reference

| Variable | Required | Default | Notes |
|---|---|---|---|
| `CONTENT_API_BASE_URL` | yes | — | `https://<site>/content-api/v1` |
| `CONTENT_API_TOKEN` | one of this or `_TOKEN_FILE` | — | Raw token value |
| `CONTENT_API_TOKEN_FILE` | one of this or `_TOKEN` | — | Path to a file holding the token, read once at startup — **use this over `CONTENT_API_TOKEN` when the MCP host may be launched from a GUI**, since a GUI-launched process on macOS inherits `launchd`'s environment, not the shell profile a `~/.zshrc`-exported token relies on |
| `CONTENT_API_HEADER` | no | `X-Silverstripe-Apitoken` | Only change if the target site's colymba `TokenAuthenticator.tokenHeader` was customized |
| `CONTENT_API_TIMEOUT` | no | `30` | Seconds |

## Troubleshooting quick reference

| Symptom | Fix |
|---|---|
| `curl` in Step 4 returns non-200 | Token expired/invalid or site misconfigured — re-mint (Step 1), not an MCP-layer problem |
| MCP tool calls fail with `Token invalid` but the Step 4 `curl` succeeded | The MCP host process didn't inherit the env var — switch to `CONTENT_API_TOKEN_FILE` (Step 2/3) |
| Install fails resolving `mcp-base` | `GITHUB_TOKEN`/`GH_TOKEN` missing or lacks access to `dynamic/daisy-base` |
| Startup raises a validation error naming `CONTENT_API_TOKEN` | Neither resolved — check the token file path (Step 2) is correct and readable |
| A tool call fails with a redirect/`ServiceError` message | `CONTENT_API_BASE_URL` is wrong (e.g. `http://` when the site requires `https://`, or a URL that redirects) — this server never follows redirects by design |
| A tool call fails with "non-JSON body" | `CONTENT_API_BASE_URL` doesn't point at `.../content-api/v1` exactly, or the site isn't reachable |

Full prose version of the above: [`docs/troubleshooting.md`](docs/troubleshooting.md).

## Next steps for the agent

Once connected, call `content_schema_site` first — it reports what the target site actually
exposes (classes, integrations, whether population endpoints are enabled) before attempting any
write. See [`docs/workflows.md`](docs/workflows.md) for worked examples.
