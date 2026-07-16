#!/usr/bin/env bash
# Re-sync the bundled MCP tool spec from the content-api module's own copy.
#
# Usage:
#   scripts/sync-spec.sh [path-to-silverstripe-content-api-checkout]
#
# Run this whenever dynamic/silverstripe-content-api bumps schema/endpoints.json
# (new/changed endpoint, new tool). After syncing, review the diff, bump this
# repo's version, and cut a release so consumers pick up the change.

set -euo pipefail

MODULE_DIR="${1:-$HOME/Sites/content-api-testbed/vendor/dynamic/silverstripe-content-api}"
SRC="$MODULE_DIR/schema/endpoints.json"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/content_api_mcp/schema/endpoints.json"

if [[ ! -f "$SRC" ]]; then
  echo "error: spec not found at $SRC" >&2
  echo "pass the module checkout path as an argument, e.g.:" >&2
  echo "  scripts/sync-spec.sh /path/to/silverstripe-content-api" >&2
  exit 1
fi

SRC_VERSION="$(python3 -c "import json; print(json.load(open('$SRC'))['version'])")"
DEST_VERSION="$(python3 -c "import json; print(json.load(open('$DEST'))['version'])" 2>/dev/null || echo "(none)")"

if diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
  echo "spec already up to date (version $DEST_VERSION)"
  exit 0
fi

cp "$SRC" "$DEST"
echo "synced spec: $DEST_VERSION -> $SRC_VERSION"
echo
echo "next: review 'git diff', bump pyproject.toml version, commit, PR, tag a release."
