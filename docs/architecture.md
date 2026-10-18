# Architecture

For contributors modifying this server, or building another server on its core.

```
content_api_mcp/
  settings.py   — standalone server config (env vars → ContentApiSettings)
  server.py     — standalone stdio server: settings → one SiteConfig → core
  core/         — the shared core (also used by DAISY's hosted daisy-content)
    client.py     — multi-site HTTP client: request building, error mapping, schema cache
    tools.py      — spec-driven SpecTool generation, argument validation, annotations
    errors.py     — the `content-error:` trailer appended to every tool error
    shaping.py    — response_format, the default limit, the response-size cap
    bootstrap.py  — content_get_started
    workflows.py  — the workflow guides (content_get_workflow + MCP prompts)
    meta.py       — register_all(): spec tools + meta tools + prompts on a FastMCP app
    spec.py       — loads the bundled spec, reads its baseRoute
  _base/        — vendored subset of dynamic/daisy-base's mcp_base (#27)
  schema/endpoints.json — bundled, version-pinned copy of the module's spec
  schema/module-ref     — the module commit that copy came from
```

## Two servers, one core

`content_api_mcp.core` is the whole tool surface. Two servers use it:

- **This package's standalone server** (`server.py`): one process per site, over stdio, run by
  the developer's MCP host. Settings become a single `SiteConfig`, so `site` is optional on
  every tool. It registers with `allow_file_path=True`: the process runs on the caller's own
  machine, so `content_asset_upload` may read a local `filePath`.
- **DAISY's hosted `daisy-content`** (private): one process serving many sites over
  streamable HTTP behind the DAISY gateway, with a `CONTENT_SITES` registry. It depends on this
  package and registers with `allow_file_path=False`, which removes `filePath` from the
  advertised schema, because a hosted process must never read its own filesystem on a caller's
  behalf.

The dependency only points one way: `daisy-content` imports this package, and nothing here
imports anything from DAISY (`tests/core/test_site_config.py` fails the suite if a module under
`content_api_mcp` imports `mcp_base`, `content_mcp` or a `daisy*` package). `pip install` from
this public repo needs no credentials.

## `core/client.py`: `ContentClient` and `SiteConfig`

- **`SiteConfig`**: `base_url`, `token`, `header` (default `X-Silverstripe-Apitoken`, the raw
  token value, not `Authorization: Bearer`), plus `token_file` (re-read on every request, so a
  re-minted token reaches a running server, #23) and `ca_file` (sent as `verify=` per request,
  so it wins over `REQUESTS_CA_BUNDLE`, #32).
- **`parse_site_registry(raw)`**: the `CONTENT_SITES` JSON shape, `{slug: {base_url, token |
  token_file, header?, ca_file?}}`. Fails loudly on a malformed entry or a `base_url` that
  doesn't end with the spec's `baseRoute`.
- **`ContentClient(sites, user_agent=None)`**: one shared session with bounded retry
  (idempotent methods only; `Retry-After` is not slept on), cookies refused, redirects never
  followed (a custom auth header would otherwise be re-sent to the redirect target).
  - **`call(site, entry, arguments, allow_file_path=..., timeout=...)`**: resolves `filePath`
    (only when allowed; size cap, credential-looking paths refused), substitutes path
    parameters, and sends GET arguments as query params (bools as `1`/`0`, so ORM filters
    match) or other methods as a JSON body.
  - **Path segments** are percent-encoded with `:` left literal (`quote(..., safe=":")`): `/`,
    `?`, `#` and whitespace can't reshape the request, the `ext:` prefix still matches
    server-side, and an external id with a space or `/` round-trips (module #240). Empty and
    dot-only segments are refused.
  - **Error mapping**: 401 `AuthenticationError`, 403 `AuthorizationError` (with distinct
    messages for the population gate and the single-record update gate), 404 `NotFoundError`
    (an `UNKNOWN_CLASS` lists the site's exposed class refs), 429 `RateLimitError`, ≥500
    `ServiceError` (with commit-uncertainty warnings on `batch`), other 4xx `MCPError`
    carrying the status. A transport failure or refused 3xx is a `ContentTransportError`.
  - **Schema cache**: parameter-less `schema/site` and `schema/{classRef}` reads are cached per
    `(site, path)` for `CONTENT_SCHEMA_CACHE_TTL` seconds (default 300).

## `core/tools.py`: `SpecTool` and `build_tools`

- **`SpecTool`**: a FastMCP `Tool` whose `parameters` is the spec entry's `inputSchema`, plus the
  injected `site` and shaping arguments, never derived from a Python signature. `run()`
  validates arguments with `jsonschema` (FastMCP doesn't, for a `Tool` subclass), dispatches the
  blocking call via `asyncio.to_thread()`, and re-raises every `MCPError` and
  `ContentTransportError` as a `ToolError` whose text ends with the `content-error:` trailer
  (`core/errors.py`, see [Validation](validation.md#error-unwrapping)).
- **`build_tools(spec, client, allow_file_path=False)`**: one `SpecTool` per spec entry. Every
  entry must have a row in `_TOOL_ANNOTATIONS` (MCP `readOnlyHint`/`destructiveHint`/
  `idempotentHint`), or the build fails at startup. Bulk tools get a 120 s timeout, trivial
  reads 10 s, the rest the client's default.

## `settings.py` and `server.py`

`ContentApiSettings` extends the vendored `BaseMCPSettings`, with every field read from a
`CONTENT_API_*` variable (see [Configuration](configuration.md)). Its `_resolve_token_file()`
`model_validator(mode="before")` fills `CONTENT_API_TOKEN` from `CONTENT_API_TOKEN_FILE`, in
"before" mode so a still-missing token joins the same combined `ValidationError` as any other
missing field.

`server.py` builds one `SiteConfig` (slug: the base URL's host name), sets the client's default
timeout from `CONTENT_API_TIMEOUT`, and calls `core.register_all(mcp, client,
allow_file_path=True)`. `main()` runs stdio with `show_banner=False` (FastMCP's banner phones
PyPI for an update check).

## Why `_base`

The settings/logging/HTTP-session conventions and the `MCPError` hierarchy originate in
[`mcp-base`](https://github.com/dynamic/daisy-base) (private). Rather than depend on it, the
narrow subset used here is vendored into `content_api_mcp/_base/` (#27), so a public install
needs no GitHub credentials. See `content_api_mcp/_base/__init__.py` and
[Development](development.md#bumping-the-_base-pin) for how it's kept in sync.
