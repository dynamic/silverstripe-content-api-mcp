# Installation

## Requirements

- Python `>=3.11`
- `uv`/`uvx` (recommended) or `pip`
- No GitHub credentials needed (#27) — every dependency resolves from PyPI.
- A target SilverStripe site running `dynamic/silverstripe-content-api`, with a minted API
  token (see [Configuration](configuration.md)).

## Install

No PyPI package — always installed from git.

```bash
# via uvx (no persistent install; re-fetches per invocation unless cached)
uvx --from git+https://github.com/dynamic/silverstripe-content-api-mcp content-api-mcp

# via pip, into a venv
python3 -m venv .venv && source .venv/bin/activate
pip install "git+https://github.com/dynamic/silverstripe-content-api-mcp"
```

## Wire it into an MCP client

Add to the client's `.mcp.json` (or equivalent):

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

If the MCP client might be launched from the GUI rather than a terminal (a desktop app, not a
shell), use `CONTENT_API_TOKEN_FILE` instead of `CONTENT_API_TOKEN` — see
[Troubleshooting](troubleshooting.md) for why.

## Pin to a release

The `.mcp.json` above floats `main` HEAD. To pin a reproducible version, add a git ref to the
`--from` URL:

```json
"git+https://github.com/dynamic/silverstripe-content-api-mcp@v1.0.0"
```

Check the [CHANGELOG](../CHANGELOG.md) for the latest tag.

## Next

- [Configuration](configuration.md) for the full environment-variable reference
- [Tools](tools.md) for what each of the 14 tools does
- Autonomous setup: [`AGENTS.md`](../AGENTS.md)
