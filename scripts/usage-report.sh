#!/usr/bin/env bash
# Report real-world usage of the content-api MCP tools from Claude Code
# session transcripts: per-tool call counts, error rate, grouped error
# signatures, response-size/context cost, and per-tool adoption (so a
# shipped-but-unused tool surfaces before the bug it was meant to prevent
# does instead).
#
# This is the harvester behind the 2026-09 field audit of
# dynamic/silverstripe-content-api usage on rocklineind and
# dynamicagency-essentials (~1,500 calls mined this way) — packaged so the
# same measurement can be repeated instead of redone by hand each time.
#
# Usage:
#   scripts/usage-report.sh [project-name ...]
#
# With no arguments, scans every project under ~/.claude/projects/. Pass one
# or more project directory names (as they appear under ~/.claude/projects/,
# e.g. "-Users-jsirish-Sites-rocklineind") to scope the scan.
#
# Gotchas this script exists to get right (see the field audit writeup for
# how each one cost real time before being understood):
#   - A project can run MORE THAN ONE content-api MCP server (e.g. a local
#     DDEV target and a pre-prod target under different tool-name prefixes
#     like `content-api` and `content-api-vesta`) — matching must be by tool
#     SUFFIX (content_records_list, content_batch, ...), not a single
#     hardcoded server name, or a multi-server project is undercounted.
#   - A git worktree records its Claude Code transcripts under the
#     PARENT/main working tree's project directory, not its own — scanning
#     only a worktree's own apparent project name misses its sessions
#     entirely. This script doesn't resolve that automatically; pass the
#     parent project's directory name if you're scanning a worktree-based
#     project.
#   - Subagent transcripts live nested under
#     <project>/<session-uuid>/subagents/*.jsonl, not just
#     <project>/*.jsonl — both are scanned.
#
# Requires: jq, awk.

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required (brew install jq / apt install jq)" >&2
  exit 1
fi

PROJECTS_DIR="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"

if [[ ! -d "$PROJECTS_DIR" ]]; then
  echo "error: projects directory not found: $PROJECTS_DIR" >&2
  exit 1
fi

if [[ $# -gt 0 ]]; then
  PROJECT_DIRS=("$@")
else
  PROJECT_DIRS=()
  while IFS= read -r -d '' d; do
    PROJECT_DIRS+=("$(basename "$d")")
  done < <(find "$PROJECTS_DIR" -mindepth 1 -maxdepth 1 -type d -print0)
fi

for proj in "${PROJECT_DIRS[@]}"; do
  dir="$PROJECTS_DIR/$proj"

  if [[ ! -d "$dir" ]]; then
    echo "warning: skipping unknown project '$proj' (no dir at $dir)" >&2
    continue
  fi

  transcripts=()
  while IFS= read -r -d '' f; do
    transcripts+=("$f")
  done < <(find "$dir" -name '*.jsonl' -print0 2>/dev/null)

  if [[ ${#transcripts[@]} -eq 0 ]]; then
    continue
  fi

  echo "════════════════════════════════════════════════════════════════"
  echo " $proj"
  echo "════════════════════════════════════════════════════════════════"

  cat "${transcripts[@]}" | jq -Rr '
    fromjson? |
    if .type=="assistant" then
      (.message.content[]? | select(.type=="tool_use") | select(.name | test("^mcp__.*__content_[a-z0-9_]+$")) | "U\t\(.id)\t\(.name)")
    elif .type=="user" then
      (.message.content[]? | select(.type=="tool_result") |
        "R\t\(.tool_use_id)\t\(if (.is_error==true) then "ERR" else "OK" end)\t\((.content | if type=="array" then (map(.text? // "") | join(" ")) else tostring end) | length)\t\((.content | if type=="array" then (map(.text? // "") | join(" ")) else tostring end) | gsub("[\\n\\r\\t]+";" ") | .[0:200])")
    else empty end
  ' | awk -F'\t' '
    function toolname(n,   i, last, needle, nlen) {
      # Strip the mcp__<server>__ prefix, keeping only the trailing
      # content_* tool name — this is what makes call-counting immune to a
      # project running more than one content-api MCP server under
      # different name prefixes (e.g. "content-api" and "content-api-
      # vesta"). Finds the LAST match of "__content_" rather than the
      # first, so a hypothetical server name that itself contains
      # "__content_" (e.g. one literally named "content_api" with an
      # underscore) still resolves to the real trailing tool name, not a
      # false match inside the server-name segment.
      needle = "__content_"
      nlen = length(needle)
      last = 0
      i = index(n, needle)
      while (i > 0) {
        last = i
        i = index(substr(n, i + 1), needle)
        if (i > 0) i += last
      }
      return substr(n, last + 2)
    }
    $1=="U" {
      tool = toolname($3)
      name[$2] = tool
      total[tool]++
      grand_total++
    }
    $1=="R" {
      tool = name[$2]
      if (tool == "") next
      if ($3 == "ERR") {
        err[tool]++
        grand_err++
        sig = $5
        # [0-9][0-9][0-9]+ rather than {3,} — some older one-true-awk
        # builds (e.g. macOS Monterey and earlier) lack ERE interval
        # expression support and would treat {3,} as a literal string.
        gsub(/[0-9][0-9][0-9]+/, "N", sig)
        errsig[tool "\t" sig]++
      } else {
        ok[tool]++
      }
      bytes[tool] += $4
      grand_bytes += $4
      if ($4+0 > maxbytes[tool]) maxbytes[tool] = $4+0
    }
    END {
      if (grand_total == 0) { exit }
      printf "\n%-24s %6s %6s %6s %7s %10s %10s\n", "tool", "calls", "ok", "err", "err%", "avg bytes", "max bytes"
      # Portable in place of gawk-only asorti(): manually collect keys, then
      # a simple descending bubble sort by call count — tool counts are a
      # handful of entries, so O(n^2) is irrelevant here.
      n = 0
      for (t in total) {
        n++
        sorted[n] = t
      }
      for (i = 1; i <= n; i++) {
        for (j = i+1; j <= n; j++) {
          if (total[sorted[j]] > total[sorted[i]]) {
            tmp = sorted[i]; sorted[i] = sorted[j]; sorted[j] = tmp
          }
        }
      }
      grand_ok = 0
      grand_matched = 0
      grand_unmatched = 0

      for (i = 1; i <= n; i++) {
        t = sorted[i]
        c = total[t]; e = err[t]+0; o = ok[t]+0
        matched = o + e
        unmatched = c - matched
        avgb = (matched > 0) ? bytes[t] / matched : 0
        # err% is of MATCHED calls (the only population whose outcome is
        # actually known) — a tool_use with no matching tool_result (a
        # session that ended mid-call, or a transcript gap) is neither a
        # success nor a failure, and folding it into either would misstate
        # the rate rather than just being silent about an unknown.
        printf "%-24s %6d %6d %6d %6.1f%% %10d %10d", t, c, o, e, (matched?100*e/matched:0), avgb, maxbytes[t]+0
        if (unmatched > 0) {
          printf "  (%d unmatched)", unmatched
        }
        printf "\n"
        grand_ok += o
        grand_matched += matched
        if (unmatched > 0) grand_unmatched += unmatched
      }
      grand_avgb = (grand_matched > 0) ? grand_bytes / grand_matched : 0
      printf "%-24s %6d %6d %6d %6.1f%% %10d\n", "TOTAL", grand_total, grand_ok, grand_err, (grand_matched?100*grand_err/grand_matched:0), grand_avgb
      if (grand_unmatched > 0) {
        printf "  (%d call(s) had no matching tool_result — excluded from ok/err/err%%/bytes)\n", grand_unmatched
      }
      printf "  (~%d k tokens of response payload returned)\n", grand_bytes/4000

      hasig = 0
      for (k in errsig) { hasig = 1; break }
      if (hasig) {
        print "\n  error signatures:"
        for (k in errsig) {
          split(k, parts, "\t")
          printf "    %3d  %-22s %s\n", errsig[k], parts[1], parts[2]
        }
      }
    }
  ' || echo "  (error scanning $proj — an unreadable transcript or a jq/awk failure; skipped)" >&2
done
