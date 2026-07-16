# Configuration

All settings are environment variables, read by `ContentApiSettings`
(`content_api_mcp/settings.py`), which extends `mcp_base.BaseMCPSettings`. That base class
normally reads vars under an `MCP_` prefix — every field below overrides that with an explicit
`validation_alias` so it reads from `CONTENT_API_*` regardless.

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `CONTENT_API_BASE_URL` | **yes** | — | e.g. `https://example.com/content-api/v1` |
| `CONTENT_API_TOKEN` | one of this or `_TOKEN_FILE` | — | From `sake tasks:MintContentApiToken` |
| `CONTENT_API_TOKEN_FILE` | one of this or `_TOKEN` | — | Path to a file containing the token, read once at startup. Preferred when the host process may not inherit your shell environment — see [Troubleshooting](troubleshooting.md) |
| `CONTENT_API_HEADER` | no | `X-Silverstripe-Apitoken` | Colymba `TokenAuthenticator.tokenHeader` — only override if a site changes that config |
| `CONTENT_API_TIMEOUT` | no | `30` | Request timeout, seconds |
| `CONTENT_API_NAME` | no | `content-api-mcp` | Server name (overrides `BaseMCPSettings.name`) |
| `CONTENT_API_DESCRIPTION` | no | `MCP proxy for a SilverStripe dynamic/silverstripe-content-api site` | Server description |

## Token resolution

Exactly one of `CONTENT_API_TOKEN` / `CONTENT_API_TOKEN_FILE` must resolve to a value, or
startup fails with a validation error naming both.

- If `CONTENT_API_TOKEN` is set, it's used as-is — `CONTENT_API_TOKEN_FILE` is ignored even if
  also set.
- Otherwise, if `CONTENT_API_TOKEN_FILE` is set, the file is read once at startup and
  `.strip()`'d into the effective token. A missing file, unreadable file, or non-UTF-8 content
  raises a `ValueError` pointing at [Troubleshooting](troubleshooting.md).
- This resolution runs as a Pydantic `model_validator(mode="before")` — deliberately "before",
  not "after" — so a still-missing token combines into the **same** validation error as any
  other missing required field (e.g. a missing `CONTENT_API_BASE_URL`) instead of masking it.

## Auth header mechanics

The resolved token is set once on the HTTP session under the `CONTENT_API_HEADER` name as a
**raw value** — not an `Authorization: Bearer` scheme. This matches colymba's
`TokenAuthenticator`, which expects the token verbatim in its configured header.

## Example

```bash
CONTENT_API_BASE_URL=https://essentials-ss6.ddev.site/content-api/v1 \
CONTENT_API_TOKEN_FILE=~/.config/content-api-mcp/essentials-ss6.token \
content-api-mcp
```
