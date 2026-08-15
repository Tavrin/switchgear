#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/../.." && pwd)
# Generic substrate must not name project-specific nouns.
pattern='(^|[^A-Za-z0-9_-])(another project|Freelancer|the old merge-policy script|beads|/mnt/linux-extra|GPU[[:space:]]+lease)([^A-Za-z0-9_-]|$)'
# standalone tracker CLI name
br_pattern='(^|[^A-Za-z0-9_-])br([^A-Za-z0-9_-]|$)'

hits=$(grep -RInE "$pattern" \
  "$ROOT/bin" "$ROOT/lib" "$ROOT/adapters" "$ROOT/policies" "$ROOT/skills" \
  || true)
if [ -n "$hits" ]; then
  echo "FAIL: project nouns in generic code:" >&2
  echo "$hits" >&2
  exit 1
fi

# 'br' as a token (not branch/break/...)
hits=$(grep -RInE "$br_pattern" \
  "$ROOT/bin" "$ROOT/lib" "$ROOT/adapters" "$ROOT/policies" "$ROOT/skills" \
  || true)
if [ -n "$hits" ]; then
  echo "FAIL: tracker token in generic code:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "ok - no project nouns in generic substrate"
