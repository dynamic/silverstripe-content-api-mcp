# Development

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires `GITHUB_TOKEN`/`GH_TOKEN` exported — `mcp-base` is a private git dependency (see
[Bumping the mcp-base pin](#bumping-the-mcp-base-pin)).

## Running checks

```bash
ruff check .
pytest
```

Or via the `local-ci` skill, which runs the same pair plus any auto-fixers.

**No GitHub Actions CI** — Actions is deliberately disabled on this repo (testing runs locally,
not in CI; Actions is reserved for non-testing jobs like image builds). The gate before a push
or PR is running the two commands above and getting a clean result.

## Test file map

| File | Covers |
|---|---|
| `tests/test_settings.py` | `ContentApiSettings` token resolution: env token used directly, `CONTENT_API_TOKEN_FILE` fallback, env-over-file precedence, clear errors for a missing file / neither source set / invalid encoding, and the combined-`ValidationError` regression (missing `base_url` + missing token report as one error) |
| `tests/test_client.py` | `ContentApiClient` request-building: `_resolve_path` (substitution, path-vs-body split, missing-arg raise, percent-encoding, placeholder-injection guard), `_flatten_query` (filter flattening, `None`-dropping, top-level-wins precedence), `_resolve_file_path` (`filePath`/`base64` mutual exclusion, size cap, sensitive-path rejection); `responses`-mocked `call()` round trips: GET query + auth header, POST JSON body, 3xx-not-followed, 2xx non-JSON body error, empty-body and JSON-`null` empty success, 401→`AuthenticationError`, 5xx→`ServiceError`, `details` population |
| `tests/test_server.py` | Startup smoke tests: `create_server()` builds without error, every spec entry has the keys `build_tools()` requires, `USER_AGENT` matches `__version__` |

## Run the server locally

Over stdio (e.g. via the [MCP inspector](https://modelcontextprotocol.io/docs/tools/inspector)):

```bash
CONTENT_API_BASE_URL=https://essentials-ss6.ddev.site/content-api/v1 \
CONTENT_API_TOKEN=<token> \
content-api-mcp
```

## Keeping the spec in sync

The tool set is generated from `content_api_mcp/schema/endpoints.json`, a copy of the module's
own spec — the module is the source of truth, this repo's copy is downstream. When the module
ships a new/changed endpoint:

```bash
scripts/sync-spec.sh /path/to/silverstripe-content-api
```

Defaults to `~/Sites/content-api-testbed/vendor/dynamic/silverstripe-content-api` (the current
vendor checkout location) if no path is given. Reports "already up to date" and exits `0` when
the two files are identical; otherwise copies and prints the version transition. Review the
diff, bump this repo's `pyproject.toml` version, PR, and tag a release so consumers pick up the
change.

**Drift detection is automated** (issue #15) via `.local-ci.json` → `scripts/check-spec-sync.sh`,
run automatically by the `local-ci` skill (not a GitHub Actions workflow — Actions is disabled
on this repo, see above). It fails the check when the two files differ, and no-ops (exit `0`)
when the module checkout isn't present locally — it detects drift, it isn't a hard dependency on
that sibling repo existing.

## Bumping the `mcp-base` pin

`mcp-base` (`pyproject.toml`) is pinned to a `dynamic/daisy-base` commit SHA, not a branch —
daisy-base cuts no git tags (its commit messages reference a version like `v2.7.0`, but that's
never actually tagged), so a SHA is the only reproducible pin available. Bump it deliberately —
not incidentally — when daisy-base changes a symbol this server depends on:
`create_http_session`, `create_base_app`, or the `AuthenticationError`/`ServiceError`/`MCPError`
exception hierarchy. After bumping, re-run the full test suite before releasing.

## Release checklist

1. `ruff check .` and `pytest` clean.
2. Bump `version` in `pyproject.toml` **and** `__version__` in
   `content_api_mcp/__init__.py` — they must match, since `USER_AGENT` is derived from
   `__version__` (see [Architecture](architecture.md#clientpy--contentapiclient)). These have
   drifted before; check both, not just `pyproject.toml`.
3. Update `CHANGELOG.md`.
4. PR, merge, tag (`vX.Y.Z`).
5. If consumers pin a version in their `.mcp.json` (see
   [Installation](installation.md#pin-to-a-release)), note the new tag for them.
