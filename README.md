# content-api-mcp

MCP server for [`dynamic/silverstripe-content-api`](https://github.com/dynamic/silverstripe-content-api)
— a stdio proxy that exposes the module's token-authenticated `/content-api/v1` REST surface as
MCP tools, so an agent can read/write SilverStripe content directly instead of shelling out to
`curl`.

One process per site: point it at a single site's base URL + API token via environment
variables. All 12 tools are generated at startup from a bundled, version-pinned copy of the
module's own `schema/endpoints.json` spec — the tool names, descriptions, and input schemas here
are exactly what that file documents, not a hand-maintained re-description of the API.

## Tools

| Tool | Method | Path |
|------|--------|------|
| `content_auth_session` | GET | `auth/session` |
| `content_schema_site` | GET | `schema/site` |
| `content_schema_class` | GET | `schema/{classRef}` |
| `content_records_list` | GET | `records/{classRef}` |
| `content_records_read` | GET | `records/{classRef}/{id}` |
| `content_records_stage` | POST | `records/{classRef}/{id}/{action}` |
| `content_batch` | POST | `batch` |
| `content_compose_page` | POST | `compositions/page` |
| `content_asset_upload` | POST | `assets` |
| `content_asset_read` | GET | `assets/{id}` |
| `content_page_convert` | POST | `pages/{id}/convert` |
| `content_page_apply_template` | POST | `pages/{id}/apply-template` |

Call `content_schema_site` first — it discovers the site's exposed classes, capabilities, and
integrations (elemental, linkfield, Essentials palette), and whether population endpoints are
enabled. `content_schema_class` then gives one class's full payload contract (fields, has_one
kinds, writability) for building `content_compose_page` / `content_batch` payloads.

The module's own generic colymba `/api` CRUD surface (stage-unaware, same token) is **not**
wrapped here — see `genericCrud` in the spec if you need it directly.

## Setup

1. Mint a token on the target site:

   ```bash
   ddev sake tasks:MintContentApiToken --email=admin
   ```

2. Add the server to your MCP client config (e.g. a project's `.mcp.json`):

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
           "CONTENT_API_TOKEN": "<minted-token>"
         }
       }
     }
   }
   ```

   If your MCP client might be launched from the GUI rather than a terminal (e.g. a
   desktop app), use `CONTENT_API_TOKEN_FILE` pointing at a token file instead of
   `CONTENT_API_TOKEN` inline — see Troubleshooting below.

## Environment variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `CONTENT_API_BASE_URL` | yes | — | e.g. `https://example.com/content-api/v1` |
| `CONTENT_API_TOKEN` | one of this or `_TOKEN_FILE` | — | from `MintContentApiToken` |
| `CONTENT_API_TOKEN_FILE` | one of this or `_TOKEN` | — | path to a file containing the token, read once at startup. Preferred when the MCP host may not inherit your shell environment (see Troubleshooting) |
| `CONTENT_API_HEADER` | no | `X-Silverstripe-Apitoken` | colymba `TokenAuthenticator.tokenHeader` — only override if a site changes that config |
| `CONTENT_API_TIMEOUT` | no | `30` | request timeout, seconds |

## Troubleshooting

**A freshly minted token is rejected (`Token invalid`) even though it works with `curl`.**

First isolate whether the problem is the token itself or the MCP host's environment:

```bash
curl -H "X-Silverstripe-Apitoken: <token>" https://<site>/content-api/v1/auth/session
```

If that returns `200`, the token is fine and the problem is that the MCP host process
never saw the updated `CONTENT_API_TOKEN` value. This is expected if the host app was
launched from the Dock, Spotlight, or Finder rather than a terminal: GUI-launched apps
on macOS inherit `launchd`'s environment, not your shell profile, so editing `~/.zshrc`
(or any shell rc file) and relaunching the app never picks up the new value — no matter
how many times you restart it — because the app was never spawned from a shell that
sources that profile in the first place.

Two fixes:

- **Preferred: use `CONTENT_API_TOKEN_FILE` instead of `CONTENT_API_TOKEN`.** Write the
  token to a file (e.g. `~/.config/content-api-mcp/<site>.token`, `chmod 600`) and point
  `CONTENT_API_TOKEN_FILE` at it in your MCP client config. A file read at process
  startup doesn't depend on environment inheritance at all, so this works identically
  whether the host was launched from a terminal or the GUI, and survives token rotation
  without touching shell profiles.
- **Fallback: inject the variable at the macOS user-session level** with
  `launchctl setenv CONTENT_API_TOKEN <value>` so GUI-launched apps see it too. This
  doesn't persist across reboots unless wrapped in a LaunchAgent, and needs re-running
  on every token rotation — `CONTENT_API_TOKEN_FILE` avoids both problems.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

Run the server locally over stdio (e.g. via the [MCP inspector](https://modelcontextprotocol.io/docs/tools/inspector)):

```bash
CONTENT_API_BASE_URL=https://essentials-ss6.ddev.site/content-api/v1 \
CONTENT_API_TOKEN=<token> \
content-api-mcp
```

### Keeping the spec in sync

The tool set is generated from `content_api_mcp/schema/endpoints.json`, a copy of the module's
own spec. When the module ships a new/changed endpoint:

```bash
scripts/sync-spec.sh /path/to/silverstripe-content-api   # defaults to ~/Sites/silverstripe-content-api
```

Review the diff, bump this repo's version, PR, and tag a release so consumers pick up the change.

## Architecture

- `content_api_mcp/settings.py` — `ContentApiSettings`, extends `mcp_base.BaseMCPSettings` with the
  per-site connection fields above.
- `content_api_mcp/client.py` — `ContentApiClient`: all HTTP request-building (path templating,
  GET query flattening, POST body, auth header, error envelope unwrapping). No MCP-specific code.
- `content_api_mcp/server.py` — registration-only: loads the bundled spec and generates one
  `SpecTool` per entry (schema passthrough, no hand-written handlers), then runs over stdio.

Built on [`mcp-base`](https://github.com/dynamic/daisy-base) (the shared library behind Dynamic
Agency's `daisy-*` MCP server fleet) for settings/logging/HTTP-session conventions — but run
standalone over stdio, not registered in the DAISY gateway, since this server is per-site
(one base URL + token per process) rather than multi-tenant/OAuth.
