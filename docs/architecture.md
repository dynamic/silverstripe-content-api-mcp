# Architecture

For contributors modifying this server. Three modules, each with one job.

```
content_api_mcp/
  settings.py   — connection config (env vars → ContentApiSettings)
  client.py     — all HTTP request-building + response parsing
  server.py     — registration-only: spec → FastMCP tools, stdio run
  _base/        — vendored subset of dynamic/daisy-base's mcp_base (#27)
  schema/endpoints.json — bundled, version-pinned copy of the module's spec
```

## `settings.py` — `ContentApiSettings`

Extends `content_api_mcp._base.BaseMCPSettings` (vendored server-identity/logging fields, which
read env vars under an `MCP_` prefix by default — see [Why `_base`](#why-_base-and-why-standalone)
below). Every field here overrides that with an explicit
`validation_alias` so it reads `CONTENT_API_*` instead — see
[Configuration](configuration.md) for the full field list.

The one non-obvious piece is `_resolve_token_file()`, a Pydantic `model_validator(mode="before")`
that fills `CONTENT_API_TOKEN` from `CONTENT_API_TOKEN_FILE` when the former is absent. It
deliberately runs in `"before"` mode (on the raw env-keyed dict), not `"after"`: an `"after"`
validator is skipped whenever *any* field fails validation, which would silently drop the
token-file resolution whenever another required field (e.g. `base_url`) was also missing —
narrowing a combined "missing config" error down to whichever field failed first. Running before
field validation means a still-missing token surfaces in the same combined `ValidationError` as
any other missing required field, same as it always did.

## `client.py` — `ContentApiClient`

All HTTP request-building and response parsing lives here; `server.py` never touches `requests`
directly.

- **`__init__`**: builds a session via `content_api_mcp._base.create_http_session(user_agent=USER_AGENT)`
  (`USER_AGENT` derived from `__version__`, not hardcoded, so a version bump can't leave it
  stale). No auth header is set here (#23) — see `call()` below.
- **`call(tool_entry, arguments)`**: the entry point `SpecTool` invokes. Builds the auth header
  fresh via `settings.current_token()` (the configured header name, default
  `X-Silverstripe-Apitoken`, holding the raw token value — **not** an `Authorization: Bearer`
  scheme, matching colymba's `TokenAuthenticator`) so a token rotated mid-session is picked up
  without restarting the host. Resolves any `filePath` argument first (see below), then
  path-substitutes, builds the URL, and dispatches
  GET (query params, `_flatten_query()`) vs. everything else (JSON body). `allow_redirects=False`
  is set unconditionally — see [Troubleshooting](troubleshooting.md#a-write-call-fails-with-500serviceerror-and-a-redirect-related-message)
  for why.
- **`_resolve_file_path(tool_entry, arguments)`**: gated on the tool entry's own `inputSchema`
  declaring a `filePath` property (currently only `content_asset_upload`) rather than a
  hardcoded tool name — a no-op for every other endpoint. When present: enforces mutual
  exclusion with `base64` (by *presence*, not truthiness — an explicit `base64: ""` alongside
  `filePath` is still a conflict), rejects a missing/non-file path, rejects a
  sensitive-looking path (dotfiles, key/cert extensions — see
  [Workflows](workflows.md#asset-upload-via-filepath)), rejects anything over 25 MiB, then reads
  and base64-encodes the file, replacing `filePath` with `base64` in the arguments before the
  request is built. The upstream API never receives a `filePath` field.
- **`_resolve_path(path_template, arguments)`**: a single regex pass over the **pristine**
  template (`_PATH_PARAM_RE`), never over an already-substituted string — so one argument's
  value can never contain another argument's `{token}` text and get re-substituted. Each
  consumed key is popped from `arguments` so it can't also leak into the query string or JSON
  body. Values are percent-encoded with `:` left unescaped (`quote(..., safe=":")`) — the API's
  `ext:<id>` convention is matched server-side against the literal, undecoded path segment,
  so encoding it to `%3A` would break every `ext:` lookup. A missing path arg raises `MCPError`
  with `status_code=400` before any HTTP call is made.
- **`_flatten_query(arguments)`**: starts query params from the `filters` dict (if present),
  then overlays every other non-`filters`, non-`None` argument. Top-level args are applied
  *after* filters regardless of argument order, so a `filters` key that happens to collide with
  a reserved param name (e.g. a `_stage` field inside `filters`) can never silently override the
  real one.
- **`_parse_response(response)`**: the error-envelope unwrap. A 3xx is never followed
  (`allow_redirects=False` upstream) and raises `ServiceError` reporting `Location`. `payload is
  None` alone can't be the "parse failed" sentinel — a literal JSON `null` body also parses to
  `None` and must remain a valid empty success. A 2xx with a non-empty, non-JSON body raises
  `ServiceError` (the realistic trigger: an HTML page from a misconfigured base URL); a
  genuinely empty or `null` body returns `{}`. Non-2xx dispatches by status:
  401/403 → `AuthenticationError`, ≥500 → `ServiceError`, everything else → `MCPError` — see
  [Validation](validation.md#error-unwrapping).
- **`_extract_error(payload, response)`**: unwraps `{"error": {code, status, message,
  details?}}`; falls back to a truncated raw response body when the envelope shape isn't
  recognized.

## `server.py` — registration

Registration-only, deliberately with no hand-written per-endpoint handlers.

- **`SpecTool`**: a `fastmcp.tools.tool.Tool` subclass whose `parameters` is the spec entry's
  `inputSchema` **verbatim** — never derived from a Python function signature — so the MCP tool
  contract exactly matches what the module documents. `run()` executes the blocking `client.call`
  via `asyncio.to_thread()` so one slow/retrying upstream request doesn't stall other concurrent
  tool calls (the stdio transport dispatches tasks concurrently, it doesn't serialize them).
- **`load_spec()`**: reads the bundled `schema/endpoints.json` via `importlib.resources`.
- **`build_tools(spec, client)`**: one `SpecTool` per `spec["tools"]` entry, `tool._call` wired
  to `partial(client.call, entry)`.
- **`create_server(settings=None)`**: defaults `settings`, loads the spec, logs its version +
  tool count + target `base_url`, builds the FastMCP app via
  `content_api_mcp._base.create_base_app(settings, register_health=False)` (no `/health` route — this server
  runs over stdio, there's no HTTP listener to attach one to), constructs one shared
  `ContentApiClient`, registers every built tool.
- **`main()`**: `create_server()` then `mcp.run(transport="stdio", show_banner=False)`.
  `show_banner=False` skips FastMCP's startup banner specifically because it phones PyPI for an
  update check — unwanted egress for a server that's launched fresh per session.

## Why `_base`, and why standalone

Uses the same settings/logging/HTTP-session conventions as Dynamic Agency's `daisy-*` MCP
server fleet (`create_http_session`, `create_base_app`, the
`AuthenticationError`/`ServiceError`/`MCPError` hierarchy) — but run standalone over stdio,
**not** registered in the DAISY gateway. The gateway model is multi-tenant/OAuth; this server
is deliberately per-site (one base URL + one token per process), which doesn't fit that shape.

Those conventions originate in [`mcp-base`](https://github.com/dynamic/daisy-base) (private),
but rather than depend on that package directly, the narrow subset this repo actually uses is
vendored into `content_api_mcp/_base/` (#27) — a public repo meant for any SilverStripe
developer to `pip install` needs to work without GitHub credentials for a private repo they have
no other reason to access. See that package's own docblock
(`content_api_mcp/_base/__init__.py`) and [Development](development.md#bumping-the-_base-pin)
for how it's kept in sync when `daisy-base` changes.
