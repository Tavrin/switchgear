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
write_profile "$TEST_STATE/profile.json" 'profile["timeout_s"]=2' 'profile["timeout_min"]=1'

export MOCK_OPENCODE_BEHAVIOR=hang-grandchild
export MOCK_CHILD_PIDFILE=$TEST_STATE/child.pid
export AI_OPENCODE_TIMEOUT=2
set +e
out=$(ai-opencode --profile "$TEST_STATE/profile.json" scout "$PRIMARY" "hang" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
job=$(printf '%s\n' "$out" | job_id_from_stdout)
st=$(result_status "$job")
assert_eq "$st" "timeout" "timeout status"
# grandchild should be gone
if [ -f "$TEST_STATE/child.pid" ]; then
  cpid=$(cat "$TEST_STATE/child.pid")
  if [ -d "/proc/$cpid" ]; then
    echo "FAIL: grandchild $cpid still alive" >&2
    kill -9 "$cpid" 2>/dev/null || true
    exit 1
  fi
fi
orphans=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["process"]["orphans_remaining"])' "$TEST_STATE/jobs/$job/result.json")
assert_eq "$orphans" "0" "no orphans"
pass "process group timeout reaps grandchild"
