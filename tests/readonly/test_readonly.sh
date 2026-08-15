#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/../.." && pwd)
AGENT_OPS_ROOT=$ROOT
# shellcheck source=../helpers/assert.sh
source "$ROOT/tests/helpers/assert.sh"
export TEST_STATE
TEST_STATE=$(mktemp -d)
SYN=$(mktemp -d)
trap 'rm -rf "$TEST_STATE" "$SYN"' EXIT
export AI_OPS_STATE=$TEST_STATE
PATH="$ROOT/tests/helpers:$ROOT/bin:$PATH"
eval "$("$ROOT/tests/helpers/make-synthetic-repo" "$SYN")"
write_profile "$TEST_STATE/profile.json"

# happy path
export MOCK_OPENCODE_BEHAVIOR=ok
before_porc=$(git -C "$PRIMARY" status --porcelain=v1)
out=$(ai-opencode --profile "$TEST_STATE/profile.json" scout "$PRIMARY" "look around")
job=$(printf '%s\n' "$out" | job_id_from_stdout)
assert_eq "$(result_status "$job")" "ok" "happy status"
assert_eq "$(git -C "$PRIMARY" status --porcelain=v1)" "$before_porc" "tree unchanged after ok"
pass "readonly happy path"

# edit tracked -> dirty
export MOCK_OPENCODE_BEHAVIOR=edit-tracked
set +e
out=$(ai-opencode --profile "$TEST_STATE/profile.json" scout "$PRIMARY" "x" 2>&1)
rc=$?
set -e
assert_eq "$rc" "2" "dirty exit"
job=$(printf '%s\n' "$out" | job_id_from_stdout)
assert_eq "$(result_status "$job")" "dirty" "dirty status"
git -C "$PRIMARY" checkout -- README.md
pass "readonly dirty tracked"

# already-dirty file content change
printf 'seed\n' >> "$PRIMARY/untracked.txt"
export MOCK_OPENCODE_BEHAVIOR=edit-dirty
set +e
out=$(ai-opencode --profile "$TEST_STATE/profile.json" scout "$PRIMARY" "x" 2>&1)
rc=$?
set -e
assert_eq "$rc" "2" "already-dirty exit"
pass "readonly already-dirty content change"

# malformed json
export MOCK_OPENCODE_BEHAVIOR=malformed
set +e
out=$(ai-opencode --profile "$TEST_STATE/profile.json" scout "$PRIMARY" "x" 2>&1)
rc=$?
set -e
# tree unchanged so not dirty; provider may still exit 0
job=$(printf '%s\n' "$out" | job_id_from_stdout)
# readonly does not require handoff; ok if events captured
[ -n "$job" ]
pass "readonly malformed does not crash"
