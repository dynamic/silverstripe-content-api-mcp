# content-api-mcp

MCP server for [`dynamic/silverstripe-content-api`](https://github.com/dynamic/silverstripe-content-api)
— a stdio proxy that exposes the module's token-authenticated `/content-api/v1` REST surface as
MCP tools, so an agent can read/write SilverStripe content directly instead of shelling out to
`curl`.

One process per site: point it at a single site's base URL + API token via environment
variables. All 14 tools are generated at startup from a bundled, version-pinned copy of the
module's own `schema/endpoints.json` spec — the tool names, descriptions, and input schemas here
are exactly what that file documents, not a hand-maintained re-description of the API.

**Setting this up autonomously?** See [`AGENTS.md`](AGENTS.md) — a machine-actionable runbook.
This README is the human-facing overview.

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
wrapped here.

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
   `CONTENT_API_TOKEN`; see [docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

Full reference lives in [docs/](docs/index.md):

| Page | Covers |
|---|---|
| [Installation](docs/installation.md) | `uvx`/`pip` install, `.mcp.json` wiring, version pinning |
| [Configuration](docs/configuration.md) | Every environment variable, token-file resolution |
| [Tools](docs/tools.md) | Each of the 14 tools in detail |
| [Workflows](docs/workflows.md) | End-to-end agent recipes |
| [Validation](docs/validation.md) | The thin-proxy stance and error shape |
| [Troubleshooting](docs/troubleshooting.md) | The GUI/`launchd` token problem and other failure modes |
| [Architecture](docs/architecture.md) | `settings.py`/`client.py`/`server.py`, vendored `_base` |
| [Development](docs/development.md) | Dev setup, testing, `sync-spec.sh`, `usage-report.sh`, releases |
