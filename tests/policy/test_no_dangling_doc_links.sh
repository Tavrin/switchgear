#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/../.." && pwd)
# Every relative markdown link must resolve to a file that exists.
#
# Three dangling links shipped in a single release pass: docs deleted as internal
# artifacts (SECURITY-REMEDIATION, INSTALL-MAP) stayed referenced from
# ARCHITECTURE, CONTAINMENT and THREAT-MODEL. Nothing caught them, because a
# broken link breaks no behaviour -- it only breaks the claim that the docs
# describe this repo. That is the one defect class this project grades as
# serious, so it gets a gate rather than a habit.
#
# Scope: relative targets only. External URLs are not checked (that needs the
# network, and this suite is hermetic); anchors are stripped before the check.

fail=0
while IFS= read -r md; do
  dir=$(dirname -- "$md")
  # Markdown inline links: ](target) -- take the target, drop title text.
  while IFS= read -r target; do
    [ -n "$target" ] || continue
    case "$target" in
      http://*|https://*|mailto:*|'#'*) continue ;;
    esac
    # Strip any #anchor, and any surrounding angle brackets.
    path=${target%%#*}
    path=${path#<}
    path=${path%>}
    [ -n "$path" ] || continue
    if [ "${path#/}" != "$path" ]; then
      resolved="$ROOT$path"          # repo-absolute
    else
      resolved="$dir/$path"          # relative to the linking file
    fi
    if [ ! -e "$resolved" ]; then
      echo "FAIL: ${md#"$ROOT"/} links to missing '$target'" >&2
      fail=1
    fi
  done < <(grep -oE '\]\([^)]+\)' "$md" | sed -e 's/^](//' -e 's/)$//' -e 's/[[:space:]].*$//')
done < <(find "$ROOT" -name '*.md' -not -path '*/.git/*' -not -path '*/build/*' -print)

# Backtick-quoted doc references are how the three real hits were written, and an
# inline-link check alone would have missed every one of them.
while IFS= read -r md; do
  while IFS= read -r ref; do
    name=${ref//\`/}
    [ -n "$name" ] || continue
    if ! find "$ROOT" -name "$(basename -- "$name")" -not -path '*/.git/*' \
         -not -path '*/build/*' -print -quit | grep -q .; then
      echo "FAIL: ${md#"$ROOT"/} names missing doc '$name'" >&2
      fail=1
    fi
  done < <(grep -oE '`[A-Za-z0-9._/-]+\.md`' "$md" || true)
done < <(find "$ROOT" -name '*.md' -not -path '*/.git/*' -not -path '*/build/*' -print)

[ "$fail" -eq 0 ] || exit 1
echo "ok - every relative doc link and backticked doc reference resolves"
