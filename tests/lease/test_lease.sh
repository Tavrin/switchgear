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

# acquire twice: second fails while this shell is the owner (PPID of the CLI)
ai-opencode --profile "$TEST_STATE/profile.json" lease acquire --dir "$WT" --owner first
set +e
err=$(ai-opencode --profile "$TEST_STATE/profile.json" lease acquire --dir "$WT" --owner second 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "live lease" "second acquire"
pass "second acquire refused"

# race: two acquires after release
ai-opencode --profile "$TEST_STATE/profile.json" lease release --dir "$WT"
out1=$TEST_STATE/r1 out2=$TEST_STATE/r2
set +e
ai-opencode --profile "$TEST_STATE/profile.json" lease acquire --dir "$WT" --owner a >"$out1" 2>"$out1.err" &
p1=$!
ai-opencode --profile "$TEST_STATE/profile.json" lease acquire --dir "$WT" --owner b >"$out2" 2>"$out2.err" &
p2=$!
wait $p1; r1=$?
wait $p2; r2=$?
set -e
wins=0
[ "$r1" -eq 0 ] && wins=$((wins + 1))
[ "$r2" -eq 0 ] && wins=$((wins + 1))
assert_eq "$wins" "1" "exactly one race winner"
pass "simultaneous acquire: one winner"

# stale / pid reuse: live pid, wrong starttime
ai-opencode --profile "$TEST_STATE/profile.json" lease release --dir "$WT" || true
ai-opencode --profile "$TEST_STATE/profile.json" lease acquire --dir "$WT" --owner staleowner
lf=$(echo "$TEST_STATE"/leases/*.json)
# pick a live pid (this shell) but smash starttime
python3 - "$lf" "$$" <<'PY'
import json, sys
path, pid = sys.argv[1], int(sys.argv[2])
obj = json.load(open(path, encoding="utf-8"))
obj["owner_pid"] = pid
obj["owner_starttime"] = "1"
json.dump(obj, open(path, "w", encoding="utf-8"), indent=2)
PY
# should be treated as not live; acquire replaces
ai-opencode --profile "$TEST_STATE/profile.json" lease acquire --dir "$WT" --owner reused
show=$(ai-opencode --profile "$TEST_STATE/profile.json" lease show --dir "$WT")
assert_contains "$show" "reused" "replaced stale"
assert_contains "$show" "live=true" "new lease live"
pass "stale lease with live pid + wrong starttime is replaced"
