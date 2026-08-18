# Changelog

## 1.4.2

### Docs
- Synced bundled `content_api_mcp/schema/endpoints.json` to module spec `v1.13` (#89, #80,
  #114): `unpublish`'s `force` field description now explains the `delete`-verb requirement is
  scoped to a `SiteTree` class with `enforce_strict_hierarchy` enabled — on any other class the
  bypass is already a no-op, so `delete` isn't required there. Description text only; no
  tool/schema shape change.

## 1.4.1

### Docs
- Synced bundled `content_api_mcp/schema/endpoints.json` to module spec `v1.12` (#126):
  `content_schema_site`'s tool description now tells an agent to check the response's
  `populationEnabled` field before a first batch/composition/asset/page-action write against
  an unfamiliar target — a dev/test rehearsal never exercises the population environment gate,
  so it gives no warning that a live/uat target will 403 `ENV_FORBIDDEN` until actually hit.
  Description text only; no tool/schema shape change.

## 1.4.0

### Added
- **#27**: Vendored the narrow `mcp_base` subset this server actually uses
  (`create_http_session`, `create_base_app`, `BaseMCPSettings`, the
  `AuthenticationError`/`ServiceError`/`MCPError` hierarchy) into
  `content_api_mcp/_base/`, copied faithfully from `dynamic/daisy-base` at
  commit `612a4155d7696c692e82a3376ce94e119a60b141` (each file's header
  names the source commit for future drift tracking). Dropped the
  `mcp-base @ git+...` dependency entirely — this is a **public** repo
  meant for any SilverStripe developer to `pip install`, and depending on
  a private repo meant install failed without a `GITHUB_TOKEN`/`GH_TOKEN`
  scoped to a repo the installer has no other reason to access. Verified in
  a clean venv with no GitHub credentials configured: `pip install -e
  ".[dev]"` succeeds, full test suite green. New `pydantic-settings`
  direct dependency (previously only transitive via `mcp-base`).
- **#23**: `ContentApiSettings.current_token()` re-reads
  `CONTENT_API_TOKEN_FILE` fresh on every tool call instead of resolving
  the token once at process construction. A content-api service-account
  token has a real expiry (7-day TTL by this project's own default) and a
  multi-session workflow — rehearse locally, re-provision after every DB
  sync, replay against a fresh environment — routinely needs to re-mint
  mid-project; before this, the only way for a running MCP server to pick
  up a newly-minted token was a full host restart, which pushed batch-write
  workflows onto a hand-rolled curl wrapper instead of this server. When
  `CONTENT_API_TOKEN` (not `_FILE`) is the configured source, behavior is
  unchanged — an env var doesn't rotate out from under a running process
  either. Raises `AuthenticationError` on a missing/unreadable/empty token
  file at call time, rather than silently sending a stale or empty header.
  **Review fix before merge**: an initial version's precedence inverted
  between construction and request time — when both `CONTENT_API_TOKEN` and
  `CONTENT_API_TOKEN_FILE` were configured, construction resolved the env
  token (as documented), but `current_token()` gated on `token_file` alone,
  so every actual request re-read and authenticated with the *file*
  instead. `CONTENT_API_TOKEN_FILE` is now cleared entirely (not just left
  unread) when the env token wins, so the two agree at every point.
  Also declared `starlette` as a direct dependency (the vendored `_base/`
  modules import it at module scope; previously only a transitive
  dependency via `fastmcp`).

## 1.3.0

### Added
- Synced bundled spec (`content_api_mcp/schema/endpoints.json`) to the module's `v1.11`
  (previously `v1.7` — four versions behind), adding two new tools and extending two existing
  ones:
  - `content_records_parity` (module #120): `GET records/{classRef}/{id}/parity` — compares a
    record and everything it `$owns` between draft and live, reporting a machine-readable
    structure plus a flat `report` list. See `dynamic/silverstripe-content-api` PR #138.
  - `content_fingerprint` (module #131): `GET fingerprint` — a deterministic, path-keyed snapshot
    of the site's content for diffing across gates or environments, plus a reachability-invariant
    check (`violations`). See `dynamic/silverstripe-content-api` PR #139.
  - `content_records_stage` gains `liveOnly` and `dryRun` (module #90/#102, `mode: subtree` only)
    — preview or selectively skip a subtree publish without writing. `mode: subtree` itself now
    also authorization-checks every descendant before writing anything, refusing the whole call
    (`403 FORBIDDEN_CLASS`/`FORBIDDEN_RECORD`, nothing written) on the first one the caller can't
    publish. See `dynamic/silverstripe-content-api` PR #113.
  - `content_batch` gains `dryRun` (module #130) — runs the batch with full
    authorization/validation inside an unconditionally-rolled-back transaction, prefixing response
    statuses `would*` on success. Its rollback is verified the same way an atomic failure's is
    (module #127), including the new `updated`-op re-check that verification gained alongside it.
    See `dynamic/silverstripe-content-api` PR #140 (#130) and PR #135 (#127).
  - No client code change needed — `client.py`'s path-substitution/query-vs-body handling is
    already generic; both new tools and both new parameters work as soon as the spec lands.
  - Docs updated to match: [tools](docs/tools.md) (tool count, two new sections, both changed
    tools' detail text), [validation](docs/validation.md) (`ROLLBACK_UNVERIFIED` now also covers
    `updated` ops and a failed `dryRun`).

## 1.2.1

### Docs
- Synced bundled spec (`content_api_mcp/schema/endpoints.json`) to the module's `v1.7`. No
  input-contract changes (no new tools, no new/changed parameters) — only two tool descriptions
  changed, describing server-side behavior added by the module since the last sync:
  - `content_batch` (module #75): an atomic rollback's independent re-verification now also
    covers `deleted` ops whose mode could actually have reached the draft row (archive, or any
    mode on an unversioned class), not just `created` ops.
  - `content_compose_page` / `content_batch` element-attach ops (module #64, new server-side
    enforcement): attaching an element type the target page's Elemental config
    (`allowed_elements`/`disallowed_elements`) doesn't permit is now rejected with `422
    ELEMENT_NOT_ALLOWED_ON_PAGE`, listing the page's actual allowed types. A compose/batch payload
    that previously succeeded against an older module version can now be rejected if it attaches a
    disallowed element type.
  - Updated `docs/tools.md`, `docs/workflows.md`, `docs/validation.md` to match — both changes
    affect operative recovery guidance for `ROLLBACK_UNVERIFIED`.

## 1.2.0

### Added
- Synced bundled spec (`content_api_mcp/schema/endpoints.json`) to the module's `v1.5`, which
  changes the input contract (not just descriptions) for two tools:
  - `content_records_stage` gains `mode` (`single`|`recursive`|`subtree`, publish only — takes
    precedence over the legacy `recursive` boolean) and `force` (unpublish/archive only, bypass
    the new descendant-cascade guard). `unpublish`/`archive` now refuse with `409
    UNPUBLISH_STRANDS_DESCENDANTS` when the record has live/draft `Hierarchy` descendants
    (`SiteTree.enforce_strict_hierarchy` would otherwise cascade-delete them). See
    `dynamic/silverstripe-content-api` PR #79 (issue #71).
  - `content_batch` gains a `force` field on delete ops (`mode: unpublish`/`archive`) and
    `subtree` on the `publish`/`defaultPublish` enums. An atomic batch's `rolledBack: true` claim
    is now independently re-verified before being reported — an unverified rollback surfaces as
    `500 ROLLBACK_UNVERIFIED` instead, carrying the same `results` array. See
    `dynamic/silverstripe-content-api` PR #74 (issue #70).
  - `content_page_convert.publish` enum also gains `subtree`.
  - No client code change needed — `_resolve_path()` already forwards non-path arguments to the
    JSON body generically, so both new fields work as soon as the spec lands. Docs
    ([tools](docs/tools.md), [workflows](docs/workflows.md#restructure-a-subtree-then-retire-the-old-wrapper),
    [validation](docs/validation.md), [troubleshooting](docs/troubleshooting.md)) updated to match;
    one new regression test pins `mode`/`force` reaching the request body.

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
