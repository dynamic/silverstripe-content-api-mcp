# Configuration

All settings are environment variables, read by `ContentApiSettings`
(`content_api_mcp/settings.py`), which extends `content_api_mcp._base.BaseMCPSettings` (vendored
from `dynamic/daisy-base`, see [Architecture](architecture.md#why-_base-and-why-standalone)).
That base class
normally reads vars under an `MCP_` prefix — every field below overrides that with an explicit
`validation_alias` so it reads from `CONTENT_API_*` regardless.

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `CONTENT_API_BASE_URL` | **yes** | — | e.g. `https://example.com/content-api/v1` |
| `CONTENT_API_TOKEN` | one of this or `_TOKEN_FILE` | — | From `sake tasks:MintContentApiToken` |
| `CONTENT_API_TOKEN_FILE` | one of this or `_TOKEN` | — | Path to a file containing the token, re-read on every tool call (#23). Preferred when the host process may not inherit your shell environment — see [Troubleshooting](troubleshooting.md) |
| `CONTENT_API_HEADER` | no | `X-Silverstripe-Apitoken` | Colymba `TokenAuthenticator.tokenHeader` — only override if a site changes that config |
| `CONTENT_API_TIMEOUT` | no | `30` | Request timeout, seconds |
| `CONTENT_API_CA_FILE` | no | — | PEM CA bundle for verifying the site's TLS certificate when it's signed by a private CA — a DDEV site's mkcert certificate is the usual case (#32). `~` expands; the path must exist at startup. Takes precedence over an exported `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`. See [Troubleshooting](troubleshooting.md#every-call-fails-with-certificate_verify_failed) |
| `CONTENT_API_NAME` | no | `content-api-mcp` | Server name (overrides `BaseMCPSettings.name`) |
| `CONTENT_API_DESCRIPTION` | no | `MCP proxy for a SilverStripe dynamic/silverstripe-content-api site` | Server description |

## Token resolution

Exactly one of `CONTENT_API_TOKEN` / `CONTENT_API_TOKEN_FILE` must resolve to a value. If
neither is set, startup fails with a Pydantic validation error naming `CONTENT_API_TOKEN` as the
missing required field — `CONTENT_API_TOKEN_FILE` is never named directly (it's an optional
field with a default), but if `CONTENT_API_BASE_URL` is also missing, that shows up in the same
combined error rather than being masked (see the "before"-mode rationale in
[Architecture](architecture.md#settingspy--contentapisettings)).

- If `CONTENT_API_TOKEN` is set, it's used as-is for the life of the process —
  `CONTENT_API_TOKEN_FILE` is ignored even if also set, consistently at startup **and** on every
  later call (#23 review follow-up: an earlier version of this resolution only ignored the file
  at startup, then silently re-read and authenticated with it on every request anyway once
  `token_file` was configured alongside `token` — fixed before merge).
- Otherwise, if `CONTENT_API_TOKEN_FILE` is set, the file is read at startup (so a
  missing/unreadable/non-UTF-8 file fails fast, before the server ever reports itself ready) and
  again **fresh on every tool call** (#23) — not just once — via
  `ContentApiSettings.current_token()`, so a token re-minted mid-session is picked up without
  restarting the host. The startup read raises a Pydantic `ValidationError`; a later read that
  fails (file removed, emptied, or mid-rewrite by a rotation script) raises `AuthenticationError`
  instead, since by then construction's own validation has long since passed. Both point at
  [Troubleshooting](troubleshooting.md).
- This resolution runs as a Pydantic `model_validator(mode="before")` — deliberately "before",
  not "after" — so a still-missing token combines into the **same** validation error as any
  other missing required field (e.g. a missing `CONTENT_API_BASE_URL`) instead of masking it.

## Auth header mechanics

The token is resolved fresh on **every** tool call via `current_token()` (#23) and set on the
`CONTENT_API_HEADER` name as a **raw value** — not an `Authorization: Bearer` scheme — for that
one request only, not cached on the HTTP session. This matches colymba's `TokenAuthenticator`,
which expects the token verbatim in its configured header.

## Example

```bash
CONTENT_API_BASE_URL=https://essentials-ss6.ddev.site/content-api/v1 \
CONTENT_API_TOKEN_FILE=~/.config/content-api-mcp/essentials-ss6.token \
content-api-mcp
```
