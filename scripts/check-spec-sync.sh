#!/usr/bin/env bash
# Fail if the bundled MCP tool spec has drifted from the content-api module's
# own copy — a local-ci custom check (see .local-ci.json), not a GitHub
# Actions workflow (Actions is disabled repo-wide by policy; local-ci is the
# test/lint gate here — see README/AGENTS.md).
#
# Usage:
#   scripts/check-spec-sync.sh [path-to-silverstripe-content-api-checkout]
#
# If the module checkout isn't present at the expected path (e.g. CI-less
# environments without the sibling repo checked out), this WARNs via exit 0
# rather than failing a check that has nothing to compare — it's a drift
# detector, not a hard dependency on that checkout existing.

set -euo pipefail

MODULE_DIR="${1:-$HOME/Sites/content-api-testbed/vendor/dynamic/silverstripe-content-api}"
SRC="$MODULE_DIR/schema/endpoints.json"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/content_api_mcp/schema/endpoints.json"

if [[ ! -f "$SRC" ]]; then
  echo "skip: module checkout not found at $MODULE_DIR — nothing to compare against"
  exit 0
fi

if diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
  echo "spec in sync"
  exit 0
fi

echo "error: content_api_mcp/schema/endpoints.json has drifted from the module's copy" >&2
echo "  module:  $SRC" >&2
echo "  bundled: $DEST" >&2
echo "run scripts/sync-spec.sh, review the diff, and cut a release" >&2
diff "$SRC" "$DEST" >&2 || true
exit 1
