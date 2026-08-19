#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/../.." && pwd)
# Generic substrate must not name project-specific nouns.
# The nouns are the CONSUMING project's, not this repo's, so they live in an
# operator-owned file rather than being hardcoded here. That is both the right
# design for a general-purpose substrate and the reason this file no longer
# names anyone's private repositories.
NOUNS_FILE=${SWITCHGEAR_PROJECT_NOUNS:-$ROOT/tests/policy/project-nouns.txt}
if [ ! -f "$NOUNS_FILE" ]; then
  echo "ok - no project-noun list at $NOUNS_FILE; nothing to enforce"
  exit 0
fi
# `|| true`: grep -v exits 1 when every line is a comment, and pipefail would
# turn an empty-but-valid noun list into a failing gate.
nouns=$(grep -vE '^[[:space:]]*(#|$)' "$NOUNS_FILE" | paste -sd'|' - || true)
if [ -z "$nouns" ]; then
  echo "ok - project-noun list is empty; nothing to enforce"
  exit 0
fi
pattern="(^|[^A-Za-z0-9_-])($nouns)([^A-Za-z0-9_-]|\$)"
# standalone tracker CLI name
br_pattern='(^|[^A-Za-z0-9_-])br([^A-Za-z0-9_-]|$)'

# Scan every generic-substrate directory that exists. Listing a directory that
# has been deleted would otherwise reduce coverage silently, so the set actually
# scanned is printed and must not be empty.
SCAN=()
for d in bin lib python adapters policies skills models commands; do
  if [ -d "$ROOT/$d" ]; then SCAN+=("$ROOT/$d"); fi
done
[ ${#SCAN[@]} -gt 0 ] || { echo "FAIL: no generic-substrate directories found" >&2; exit 1; }

hits=$(grep -RInE "$pattern" "${SCAN[@]}" || true)
if [ -n "$hits" ]; then
  echo "FAIL: project nouns in generic code:" >&2
  echo "$hits" >&2
  exit 1
fi

# 'br' as a token (not branch/break/...)
hits=$(grep -RInE "$br_pattern" "${SCAN[@]}" || true)
if [ -n "$hits" ]; then
  echo "FAIL: tracker token in generic code:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "ok - no project nouns in generic substrate (scanned: ${SCAN[*]##*/})"
