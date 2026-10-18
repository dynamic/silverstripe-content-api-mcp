#!/usr/bin/env bash
# Fail if content_api_mcp/schema/module-ref doesn't name a commit whose schema/endpoints.json
# matches the bundled copy — a local-ci custom check (see .local-ci.json),
# companion to check-spec-sync.sh.
#
# content_api_mcp/schema/module-ref is a provenance record, written by scripts/sync-spec.sh:
# the dynamic/silverstripe-content-api commit the bundled content_api_mcp/schema/endpoints.json
# was last verified against. check-spec-sync.sh never reads it — that check
# always compares against the module's live default-branch tip, not a pin —
# so without this check module-ref could go stale (someone re-syncs
# endpoints.json but forgets to bump it) with nothing catching it, silently
# defeating its one job.
#
# Usage:
#   scripts/check-module-ref.sh [path-to-silverstripe-content-api-checkout] [path-to-module-ref-file]
#
# Same local-dev posture as check-spec-sync.sh: if there's no git checkout
# at the expected path (e.g. a plain composer vendor install with no .git,
# or nothing checked out at all), this WARNs via exit 0 rather than failing
# a check that has nothing to verify against. The module checkout must have
# full history (a shallow clone may not contain the commit schema/module-ref
# names) — CI's checkout step passes fetch-depth: 0 for this reason.
#
# CI sets MODULE_REF_REQUIRE_MODULE=1 (same pattern as check-spec-sync.sh's
# SPEC_SYNC_REQUIRE_MODULE) so a missing/misconfigured module checkout is a
# hard error there instead of a silent skip — step ordering alone ("CI
# always checks the module out first") isn't a structural guarantee; a
# future reordered/removed checkout step should fail the job, not quietly
# stop verifying anything.

set -euo pipefail

MODULE_DIR="${1:-$HOME/Sites/content-api-testbed/vendor/dynamic/silverstripe-content-api}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_REF_FILE="${2:-$REPO_ROOT/content_api_mcp/schema/module-ref}"
DEST="$REPO_ROOT/content_api_mcp/schema/endpoints.json"

if [[ ! -d "$MODULE_DIR/.git" ]]; then
  if [[ "${MODULE_REF_REQUIRE_MODULE:-}" == "1" ]]; then
    echo "error: no git checkout found at $MODULE_DIR" >&2
    exit 1
  fi
  echo "skip: no git checkout at $MODULE_DIR — nothing to verify module-ref against"
  exit 0
fi

ref="$(cat "$MODULE_REF_FILE")"

# A bare ":path" (no ref before the colon) is valid git revision syntax
# meaning "this path in the index" — so an empty or unreadable module-ref
# must be rejected explicitly, or it silently resolves against whatever
# happens to already be checked out instead of failing.
if ! [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
  echo "error: $MODULE_REF_FILE does not contain a valid 40-char commit SHA (got: '$ref')" >&2
  exit 1
fi

module_schema="$(mktemp)"
trap 'rm -f "$module_schema"' EXIT
git -C "$MODULE_DIR" show "$ref:schema/endpoints.json" > "$module_schema"

if diff -q "$module_schema" "$DEST" >/dev/null 2>&1; then
  exit 0
fi

echo "error: content_api_mcp/schema/module-ref names $ref, but that commit's schema/endpoints.json does not match the bundled copy" >&2
echo "  module ref: $ref (schema/endpoints.json from $MODULE_DIR at that commit)" >&2
echo "  bundled:    $DEST" >&2
diff "$module_schema" "$DEST" >&2 || true
exit 1
