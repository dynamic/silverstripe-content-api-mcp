# content-api-mcp documentation

MCP server for [`dynamic/silverstripe-content-api`](https://github.com/dynamic/silverstripe-content-api)
— a stdio proxy that exposes the module's token-authenticated `/content-api/v1` REST surface as
MCP tools, so an agent can read/write SilverStripe content directly instead of shelling out to
`curl`.

One process per site: point it at a single site's base URL + API token via environment
variables. All 12 tools are generated at startup from a bundled, version-pinned copy of the
module's own `schema/endpoints.json` spec.

## Setting this server up autonomously?

If you're an agent configuring this server for a new project, read
**[`AGENTS.md`](../AGENTS.md)** at the repo root instead of this page — it's a machine-actionable
runbook (exact commands, no prose to parse). This `docs/` tree is the human-facing reference.

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

## Page index

| Page | Covers |
|---|---|
| [Installation](installation.md) | `uvx`/`pip` install, Python version, `.mcp.json` wiring |
| [Configuration](configuration.md) | Every environment variable, defaults, token-file resolution |
| [Tools](tools.md) | Each of the 12 tools in detail |
| [Workflows](workflows.md) | End-to-end agent recipes |
| [Validation](validation.md) | The thin-proxy stance and error shape |
| [Troubleshooting](troubleshooting.md) | The GUI/`launchd` token problem and other failure modes |
| [Architecture](architecture.md) | `settings.py`/`client.py`/`server.py`, `mcp-base` |
| [Development](development.md) | Dev setup, testing, `sync-spec.sh`, releases |

See also: the [module's own documentation](https://github.com/dynamic/silverstripe-content-api/tree/1/docs/en)
for the underlying REST API this server proxies.
