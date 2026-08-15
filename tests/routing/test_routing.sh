#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/../.." && pwd)
AGENT_OPS_ROOT=$ROOT
# shellcheck source=../helpers/assert.sh
source "$ROOT/tests/helpers/assert.sh"
export TEST_STATE
TEST_STATE=$(mktemp -d)
trap 'rm -rf "$TEST_STATE"' EXIT
export AI_OPS_STATE=$TEST_STATE
PATH="$ROOT/tests/helpers:$ROOT/bin:$PATH"
export PATH

out=$(ai-opencode --profile "$ROOT/project-profiles/example.json" models)
assert_contains "$out" "opencode-go/deepseek-v4-flash" "allowlist printed"
assert_contains "$out" "opencode-go/glm-5.3" "catalog via mock"
pass "models discovery"

# unknown role
set +e
err=$(ai-opencode --profile "$ROOT/project-profiles/example.json" review "$TEST_STATE" nosuch "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ] || { echo "FAIL: unknown role should refuse"; exit 1; }
assert_contains "$err" "unknown role" "unknown role message"
pass "unknown role refused"

# denied model via custom role
write_profile "$TEST_STATE/profile.json" \
  'profile["roles"]["bad"]={"model":"opencode-go/gpt-5.6-luna","mode":"readonly"}'
# need a git dir
git init -q -b main "$TEST_STATE/repo"
git -C "$TEST_STATE/repo" config user.email t@t
git -C "$TEST_STATE/repo" config user.name t
printf 'a\n' > "$TEST_STATE/repo/f"
git -C "$TEST_STATE/repo" add f
git -C "$TEST_STATE/repo" commit -q -m i
set +e
err=$(ai-opencode --profile "$TEST_STATE/profile.json" review "$TEST_STATE/repo" bad "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ] || { echo "FAIL: denied model should refuse"; exit 1; }
assert_contains "$err" "not allowed" "denied model"
pass "denied model refused"
