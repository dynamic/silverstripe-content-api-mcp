# Changelog

## 1.1.3

### Docs
- Synced bundled spec (`content_api_mcp/schema/endpoints.json`) to the module's `v1.4`: the
  `content_schema_class` tool description now documents the module's new many_many `through`
  relation support (join-DataObject-backed relations round-trip `{"id", "extraFields"}` the same
  way `many_many_extraFields` relations do) and the schema's new `extraFields` array on
  has_many/many_many entries. No tool input/output shape change; description text only. See
  `dynamic/silverstripe-content-api` PR #60.

## 1.1.2

### Docs
- Synced bundled spec (`content_api_mcp/schema/endpoints.json`) to the module's `v1.3`: the
  `content_schema_class` tool description now documents the module's new schema honesty flags
  (`computed`/`importOwned` + optional `note` on a field entry — advisory markers that a write
  will be accepted but then silently overwritten). No tool input/output shape change; description
  text only. See `dynamic/silverstripe-content-api` PR #59.

## 1.1.1

### Docs
- Split reference documentation out of the single README into a `docs/` tree (installation,
  configuration, tools, workflows, validation, troubleshooting, architecture, development) and
  added `AGENTS.md`, a machine-actionable setup runbook for autonomous agent configuration.
  README trimmed to link into `docs/`. No runtime behavior change — `settings.py`'s only edit
  updates a comment/error-message path reference from README to `docs/troubleshooting.md`.

## 1.1.0

### Added
- `filePath` as an alternative to `base64` on `content_asset_upload` (synced from
  `dynamic/silverstripe-content-api` spec v1.2, module issue #39). The client resolves it
  locally — reads and base64-encodes the file on the machine running the MCP host — and never
  forwards the path itself upstream. Fixes a real failure mode: without this, an agent whose only
  handle on a file is a local path has to reproduce the entire base64 payload as literal text to
  call the tool, which for a real image (hundreds of KB or more) risks silent corruption on
  reassembly — an image's dimensions parse fine from its header even when the body is truncated
  or garbled, so a corrupted upload can still report success.

### Fixed
- `scripts/sync-spec.sh`'s default `MODULE_DIR` pointed at a checkout deleted 2026-07-12; now
  defaults to `~/Sites/content-api-testbed/vendor/dynamic/silverstripe-content-api`, where the
  module actually lives (flagged as a known follow-up in 1.0.0).
- Code review on the `filePath` change (above) surfaced four issues in the first pass, all fixed
  before release:
  - Dropping `base64` from `content_asset_upload`'s `required` list left nothing enforcing that
    one of `base64`/`filePath` is actually provided — a call with neither now raises a clear
    client-side error instead of forwarding a bodyless request upstream.
  - The `base64`/`filePath` mutual-exclusion check used a truthy test, so an explicit
    `base64: ""` alongside `filePath` silently bypassed it; now presence-based (`is not None`).
  - `filePath` had no size cap — the whole file is read into memory then base64-encoded (~33%
    larger again) before sending, so an oversized file meant a memory spike and a slow, likely-
    timing-out request instead of a fast, clear rejection. Capped at 25 MiB.
  - `filePath` had no restriction on *what* could be read — a caller (or a compromised/injected
    tool argument) could point it at any local file the MCP host can access, e.g. `~/.ssh/id_rsa`,
    and it would be read, base64-encoded, and POSTed upstream as if it were an image. Added a
    denylist for dotfiles/dotdirs and common credential-file patterns (`id_rsa`, `*.pem`, `*.key`,
    etc.) as defense-in-depth — explicitly not a security boundary by itself (this MCP host already
    has the same filesystem access as the agent invoking it, and a denylist is trivially bypassed
    by renaming a file); the actual boundary remains not letting untrusted input control tool
    arguments in the first place. This closes the easy/accidental case with a clear error rather
    than a silent exfiltration.
- Re-syncing the vendored spec (`scripts/sync-spec.sh`) silently dropped a `relations` field
  description — the only documented warning that a polymorphic `has_one` write rejects a bare
  id/externalId and needs an explicit `{"class": ...}` hint — from `content_batch` and
  `content_compose_page`. That description was only ever patched into this repo's bundled copy in
  the 1.0.0 release (#6), never fed back into the module's own spec (the real sync source), so a
  blind `cp` from there couldn't know to keep it. Restored at the source
  (`dynamic/silverstripe-content-api`'s own `schema/endpoints.json`) so it survives every future
  sync instead of being silently lost again.

## 1.0.0

First stable release. Closes all 6 issues found in the 2026-07-15 code audit (#3–#8).

### Added
- Smoke tests (`tests/test_server.py`): `create_server()` builds without error, and every entry
  in the bundled `schema/endpoints.json` has the keys `build_tools()` requires (#8).
- `## Validation` section in the README documenting this server's thin-proxy stance: schemas are
  advisory, the PHP content-api is the single source of truth for enforcement (#6).
- A `relations` field description on `content_batch` and `content_compose_page`, documenting the
  polymorphic `has_one` `{"class": "...", "id": n}` hint shape (#6).

### Fixed
- `USER_AGENT` is now derived from `__version__` instead of a separately hardcoded string, so it
  can no longer drift on a version bump (#8).
- The HTTP client no longer follows redirects (`allow_redirects=False`) — a custom auth header
  survives a same-host redirect in `requests`, so an open redirect or misconfigured base URL could
  previously have egressed the raw API token to a redirect target. Any 3xx now raises a clear error
  instead of being silently chased or swallowed (#7).
- A 2xx response with a non-JSON, non-empty body (e.g. an HTML page from an auth/routing
  misconfiguration) now raises a clear error instead of being treated as an empty-success `{}`. A
  genuinely empty 2xx body is still treated as success (#5).

### Changed
- `mcp-base` is now pinned to a `dynamic/daisy-base` commit SHA instead of tracking branch HEAD,
  making installs reproducible (`daisy-base` cuts no git tags, so a SHA is the only pin available) (#4).

### Resolved without code (#3)
- #3 asked for a CI workflow so `pytest`/`ruff` regressions couldn't land unnoticed. Resolved
  by policy, not automation: this org runs tests via the `local-ci` skill, not GitHub Actions
  (Actions is disabled repo-wide, reserved for non-testing jobs). README's `## Development`
  section now documents `local-ci` (or plain `ruff check . && pytest`) as the actual pre-push gate.

### Known follow-ups (not blocking this release)
- `scripts/sync-spec.sh` still defaults `MODULE_DIR` to `$HOME/Sites/silverstripe-content-api`, a
  checkout deleted 2026-07-12; the module now lives at
  `~/Sites/content-api-testbed/vendor/dynamic/silverstripe-content-api`. Pass the path explicitly
  until the default is updated.
- The `relations` field descriptions added in this release exist only in this repo's bundled
  `schema/endpoints.json`, not yet in the upstream module's own copy (the vendor checkout had
  unrelated in-progress work at release time) — fold the same doc note into the module's spec on
  the next sync so it isn't lost on the next `scripts/sync-spec.sh` run.
