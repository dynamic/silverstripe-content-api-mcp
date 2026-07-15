# Changelog

## 1.0.0

First stable release. Closes all 6 issues found in the 2026-07-15 code audit (#3–#8).

### Added
- CI workflow (`.github/workflows/ci.yml`): `ruff check` + `pytest` on every push/PR to `main`,
  authenticated for the private `mcp-base` dependency (#3).
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

### Known follow-ups (not blocking this release)
- `scripts/sync-spec.sh` still defaults `MODULE_DIR` to `$HOME/Sites/silverstripe-content-api`, a
  checkout deleted 2026-07-12; the module now lives at
  `~/Sites/content-api-testbed/vendor/dynamic/silverstripe-content-api`. Pass the path explicitly
  until the default is updated.
- The `relations` field descriptions added in this release exist only in this repo's bundled
  `schema/endpoints.json`, not yet in the upstream module's own copy (the vendor checkout had
  unrelated in-progress work at release time) — fold the same doc note into the module's spec on
  the next sync so it isn't lost on the next `scripts/sync-spec.sh` run.
