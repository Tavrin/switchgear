#!/usr/bin/env bash
# Hermetic suite. Never calls a live model. Never touches live config.
set -euo pipefail

ROOT=$(cd "$(dirname -- "$0")/.." && pwd)
export AGENT_OPS_ROOT=$ROOT
export PATH="$ROOT/tests/helpers:$ROOT/bin:$PATH"
unset OPENCODE_CONFIG_DIR || true
unset AI_OPS_WRITE || true

echo "== schema =="
python3 "$ROOT/tests/schema/test_schemas.py"

run_one() {
  echo "== $1 =="
  bash "$ROOT/tests/$1"
}

run_one policy/test_no_project_nouns.sh
run_one routing/test_routing.sh
run_one policy/test_refusals.sh
run_one readonly/test_readonly.sh
run_one process/test_process_group.sh
run_one lease/test_lease.sh
run_one write/test_write_containment.sh
run_one write/test_redteam_containment.sh

# ai-cmd unit checks
echo "== ai-cmd =="
SYN=$(mktemp -d)
eval "$("$ROOT/tests/helpers/make-synthetic-repo" "$SYN")"
STATE=$(mktemp -d)
export TEST_STATE=$STATE
# shellcheck source=helpers/assert.sh
source "$ROOT/tests/helpers/assert.sh"
write_profile "$STATE/profile.json" 'profile["commands"]={"probe":{"argv":["true"]}}'
ai-cmd --dir "$PRIMARY" --profile "$STATE/profile.json" -- probe
set +e
ai-cmd --dir "$PRIMARY" --profile "$STATE/profile.json" -- probe ';evil' >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ]
set +e
ai-cmd --dir "$PRIMARY" --profile "$STATE/profile.json" -- nosuch >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ]
rm -rf "$SYN" "$STATE"
echo "ok - ai-cmd refusals"

echo
echo "ALL HERMETIC TESTS PASSED"
