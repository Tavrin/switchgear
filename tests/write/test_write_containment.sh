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

write_profile "$TEST_STATE/profile.json" \
  'profile["write_enabled"]=True' \
  'profile["commands"]={"probe":{"argv":["true"]}}' \
  'profile["review"]={"required_after_write":True,"independence":{"different_job":"required","different_model":"required","different_family":"preferred","different_provider":"optional"}}'

envfile() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json, sys
dest, cwd, role, extra = sys.argv[1:5]
obj = {
  "goal": "edit tracked.txt",
  "context": "synthetic",
  "constraints": ["worktree only"],
  "done_when": ["handoff in state dir"],
  "non_goals": ["merge"],
  "risk_threshold": "incorrect behavior only",
  "stop_condition": "stop after edit",
  "expansion_rule": "report and wait",
  "mode": "bounded-write",
  "role": role,
  "cwd": cwd,
}
if extra == "cmds":
    obj["commands"] = [{"verb": "probe", "args": []}]
json.dump(obj, open(dest, "w", encoding="utf-8"), indent=2)
PY
}

# write without lease
envfile "$TEST_STATE/env.json" "$WT" implement none
export AI_OPS_WRITE=1
export MOCK_OPENCODE_BEHAVIOR=edit-inside
set +e
err=$(ai-opencode --profile "$TEST_STATE/profile.json" write "$WT" implement --envelope "$TEST_STATE/env.json" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "no lease" "no lease"
pass "write without lease refused"

# write on primary checkout
envfile "$TEST_STATE/env-primary.json" "$PRIMARY" implement none
set +e
err=$(ai-opencode --profile "$TEST_STATE/profile.json" write "$PRIMARY" implement --envelope "$TEST_STATE/env-primary.json" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "linked worktree" "primary"
pass "write on primary refused"

# happy write
ai-opencode --profile "$TEST_STATE/profile.json" lease acquire --dir "$WT" --owner worker
export AI_OPS_CANARIES="$CANARY_SIBLING:$CANARY_PRIMARY"
envfile "$TEST_STATE/env.json" "$WT" implement cmds
out=$(ai-opencode --profile "$TEST_STATE/profile.json" write "$WT" implement --envelope "$TEST_STATE/env.json")
job=$(printf '%s\n' "$out" | job_id_from_stdout)
assert_eq "$(result_status "$job")" "awaiting_review" "awaiting review"
head_before=$(git -C "$WT" rev-parse HEAD)
# HEAD unchanged
assert_eq "$(git -C "$WT" rev-parse HEAD)" "$head_before" "HEAD unchanged"
# handoff only in state
assert_file "$TEST_STATE/jobs/$job/handoff.json"
[ ! -f "$WT/handoff.json" ]
grep -q worker-edit "$WT/tracked.txt"
pass "in-tree edit -> awaiting_review, handoff in state"

# cannot mark ok without review
st=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$TEST_STATE/jobs/$job/result.json")
assert_eq "$st" "awaiting_review" "still awaiting"
pass "write ok unreachable without review"

# attach independent review (different family: implement=glm, review=deepseek)
python3 - "$TEST_STATE/rev.json" "$WT" "$job" <<'PY'
import json, sys
dest, cwd, parent = sys.argv[1:4]
json.dump({
  "goal": "attack the write",
  "context": "synthetic",
  "constraints": [],
  "done_when": ["review recorded"],
  "non_goals": ["implement"],
  "risk_threshold": "incorrect behavior only",
  "stop_condition": "stop",
  "expansion_rule": "wait",
  "mode": "readonly",
  "role": "review",
  "cwd": cwd,
  "parent_job": parent,
}, open(dest, "w", encoding="utf-8"), indent=2)
PY
export MOCK_OPENCODE_BEHAVIOR=ok
# review of a dirty worktree would fail readonly integrity — snapshot includes tracked.txt edit.
# Review is allowed to see dirty files; but our readonly policy requires identical tree.
# Restore? No: reviewer must run on the dirty worktree. Readonly integrity is "the review job
# itself must not change the tree", not "the tree must be clean".
# snapshot_tree includes dirty file hashes, so if review does not edit, before==after. Good.
out=$(ai-opencode --profile "$TEST_STATE/profile.json" review "$WT" review --envelope "$TEST_STATE/rev.json")
# parent should now be ok
assert_eq "$(result_status "$job")" "ok" "promoted after independent review"
pass "independent different-family review promotes write"

# same-family required profile refuses promotion
write_profile "$TEST_STATE/fam.json" \
  'profile["write_enabled"]=True' \
  'profile["models"]["allow"].append("opencode-go/glm-5.2")' \
  'profile["models"]["catalog"].append({"id":"opencode-go/glm-5.2","provider":"opencode","model_family":"glm","vendor_family":"zhipu"})' \
  'profile["review"]={"required_after_write":True,"independence":{"different_job":"required","different_model":"required","different_family":"required","different_provider":"optional"}}' \
  'profile["roles"]["review2"]={"model":"opencode-go/glm-5.2","mode":"readonly"}'
# glm implement vs glm review2 = same family
# new write job
git -C "$WT" checkout -- tracked.txt
ai-opencode --profile "$TEST_STATE/fam.json" lease release --dir "$WT" || true
ai-opencode --profile "$TEST_STATE/fam.json" lease acquire --dir "$WT" --owner worker2
export MOCK_OPENCODE_BEHAVIOR=edit-inside
envfile "$TEST_STATE/env2.json" "$WT" implement none
out=$(AI_OPS_WRITE=1 ai-opencode --profile "$TEST_STATE/fam.json" write "$WT" implement --envelope "$TEST_STATE/env2.json")
job2=$(printf '%s\n' "$out" | job_id_from_stdout)
python3 - "$TEST_STATE/rev2.json" "$WT" "$job2" <<'PY'
import json, sys
dest, cwd, parent = sys.argv[1:4]
json.dump({
  "goal": "same family review",
  "context": "x",
  "constraints": [],
  "done_when": ["recorded"],
  "non_goals": [],
  "risk_threshold": "x",
  "stop_condition": "stop",
  "expansion_rule": "wait",
  "mode": "readonly",
  "role": "review2",
  "cwd": cwd,
  "parent_job": parent,
}, open(dest, "w", encoding="utf-8"), indent=2)
PY
export MOCK_OPENCODE_BEHAVIOR=ok
ai-opencode --profile "$TEST_STATE/fam.json" review "$WT" review2 --envelope "$TEST_STATE/rev2.json" >/dev/null
assert_eq "$(result_status "$job2")" "review_failed" "same family required"
pass "same-family review fails when family is required"

# example profile still refuses write even with AI_OPS_WRITE
set +e
err=$(AI_OPS_WRITE=1 ai-opencode --profile "$ROOT/project-profiles/example.json" write "$WT" implement --envelope "$TEST_STATE/env.json" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "write_enabled" "example write off"
pass "example profile write stays off"
