# Development

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

No GitHub credentials needed (#27) — every dependency resolves from PyPI.

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

## Measuring real usage

`scripts/usage-report.sh` mines Claude Code session transcripts (`~/.claude/projects/*/**.jsonl`)
for `mcp__*__content_*` tool calls and reports, per project: call counts per tool, error rate,
grouped error signatures, and response payload size (a rough proxy for context cost). This is
the harvester behind the 2026-09 field audit of real usage on two production consumer projects —
packaged so the measurement is repeatable instead of redone by hand each time someone asks "is
this actually working."

```bash
scripts/usage-report.sh                                    # scan every project
scripts/usage-report.sh -Users-jsirish-Sites-some-project   # scope to one or more
```

Matches by tool-name **suffix**, not a hardcoded server name — a project running more than one
content-api MCP server (e.g. a local DDEV target and a pre-prod target under different name
prefixes) is counted correctly across both. It does not resolve git-worktree transcript
placement automatically (a worktree's sessions are recorded under its main/parent working tree's
project directory, not its own) — pass the parent project's directory name for a worktree-based
project. No `pytest` coverage — it's a read-only reporting script over data this repo doesn't
own the shape of, in the same spirit as `sync-spec.sh`/`check-spec-sync.sh` above.

## Bumping the `_base` pin

`content_api_mcp/_base/` (#27) is a vendored copy of the narrow `mcp_base` subset this server
uses, pinned by commit comment (each file's header names the `dynamic/daisy-base` commit it was
copied from — `612a4155d7696c692e82a3376ce94e119a60b141` as of this writing), not an installed
dependency. daisy-base cuts no git tags (its commit messages reference a version like `v2.7.0`,
but that's never actually tagged), so a commit SHA is the only reproducible reference available.

Bump deliberately — not incidentally — when daisy-base changes a symbol this server depends on:
`create_http_session`, `create_base_app`, `BaseMCPSettings`, or the
`AuthenticationError`/`ServiceError`/`MCPError` exception hierarchy. Re-read the real
implementation in `dynamic/daisy-base` at the new commit rather than hand-editing the vendored
copy — the error hierarchy in particular has behavior `client.py`'s `except` clauses depend on.
Update the source-commit comment at the top of each changed `_base/*.py` file. After bumping,
re-run the full test suite (including `tests/test_client.py`'s error-mapping cases) before
releasing.

## Release checklist

1. `ruff check .` and `pytest` clean.
2. Bump `version` in `pyproject.toml`. `__version__` is derived from installed package metadata
   (`importlib.metadata`, see [Architecture](architecture.md#clientpy--contentapiclient)), not a
   hardcoded literal — it used to drift from `pyproject.toml` before that was fixed, but now only
   the one bump is needed.
3. Update `CHANGELOG.md`.
4. PR, merge, tag (`vX.Y.Z`).
5. If consumers pin a version in their `.mcp.json` (see
   [Installation](installation.md#pin-to-a-release)), note the new tag for them.
