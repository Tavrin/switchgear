#!/usr/bin/env bash
# Live smoke test: exercises the paths tests/run.sh deliberately cannot.
#
# run.sh is hermetic and unsets AI_OPS_ALLOW_LIVE_PROVIDER on purpose -- a CI
# suite must not spend money or depend on a network. But every significant defect
# found on 2026-08-18 lived in the live path and passed the hermetic suite: the
# mock's invented event vocabulary, header stripping that tripped the upstream
# CDN, a doubled /v1 path segment, a launcher that broke when symlinked, a broker
# denying /messages, and a promotion gate that ignored findings. This script is
# the repeatable version of the walk that found them.
#
# OPT-IN and it costs real money (a few cents). Requires:
#   ~/.config/ai-ops/credentials/opencode-go   (mode 600)
#   AI_OPS_LIVE=1
set -uo pipefail

if [ "${AI_OPS_LIVE:-}" != "1" ]; then
  echo "live smoke test skipped (set AI_OPS_LIVE=1 to run; it spends real credit)"
  exit 0
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CLI="$ROOT/bin/ai-opencode"
PROVIDER="${AI_OPS_LIVE_PROVIDER:-$HOME/.opencode/bin/opencode}"
SCOUT_MODEL="${AI_OPS_LIVE_SCOUT_MODEL:-opencode-go/deepseek-v4-flash}"
REVIEW_MODEL="${AI_OPS_LIVE_REVIEW_MODEL:-opencode-go/kimi-k3}"
TIMEOUT="${AI_OPS_LIVE_TIMEOUT:-300}"

[ -x "$PROVIDER" ] || { echo "FAIL: no provider at $PROVIDER"; exit 1; }

WORK=$(mktemp -d -t aiops-live-XXXXXX)
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s -- %s\n' "$1" "${2:-}"; fail=$((fail+1)); }
jq_()  { python3 -c "import json,sys; d=json.load(open('$1')); print(d$2)" 2>/dev/null; }

# The upstream is genuinely flaky (observed repeatedly on 2026-08-18: glm-5.3
# dead, qwen intermittent, sporadic "Unexpected server error"). A smoke test that
# fails at random teaches you to ignore it, so retry ONLY the failures that never
# reached a model -- forwarded==0 means the request did not get past transport,
# which is infrastructure, not a rail defect. A failure WITH forwarded>0 means the
# model answered and our handling broke: that is real and must not be retried away.
live_job() {  # live_job <outfile> <args...>
  local out=$1; shift
  local attempt=1
  while : ; do
    "$CLI" "${BASE[@]}" "$@" > "$out" 2>"${out%.json}.err"
    local st fw
    st=$(jq_ "$out" "['status']"); fw=$(jq_ "$out" "['provider_calls']['forwarded']")
    if [ "$st" = "ok" ] || [ "$st" = "awaiting_review" ]; then return 0; fi
    if [ "${fw:-0}" != "0" ] || [ "$attempt" -ge 3 ]; then return 1; fi
    printf '  \033[33mretry\033[0m %s (attempt %s: %s, never reached a model)\n' \
      "$(basename "$out")" "$attempt" "${st:-no-json}"
    attempt=$((attempt+1)); sleep 3
  done
}

# ---------------------------------------------------------------- fixture ----
mkdir -p "$WORK"/{repo,state}
cd "$WORK/repo"
git init -q -b main
git config user.email live@test; git config user.name live
mkdir -p src
cat > src/calc.py <<'PY'
def divide(a, b):
    # BUG: no zero check
    return a / b
PY
git add -A; git commit -qm init
git worktree add -q ../wt -b work
cd "$WORK"

cat > profile.json <<JSON
{ "name":"live","provider":"opencode","timeout_s":$TIMEOUT,"timeout_min":1,"timeout_max":900,
  "models":{"allow":["$SCOUT_MODEL","$REVIEW_MODEL"],"deny":[]},
  "roles":{"scout":{"model":"$SCOUT_MODEL","mode":"readonly"},
           "review":{"model":"$REVIEW_MODEL","mode":"readonly"},
           "implement":{"model":"$SCOUT_MODEL","mode":"bounded-write"}},
  "review":{"required_after_write":true,
            "independence":{"different_job":"required","different_model":"required",
                            "different_family":"required","different_provider":"optional"}},
  "worktree":{"require_linked_worktree_for_write":true,"require_lease_for_write":true,
              "allow_primary_for_readonly":true},
  "write_enabled":true,"commands":{} }
JSON
cat > envelope.json <<JSON
{ "goal":"Make divide() raise ValueError when b is 0","context":"live smoke test",
  "constraints":["keep the signature"],"done_when":["divide raises ValueError on b==0"],
  "non_goals":["anything else"],"risk_threshold":"incorrect behaviour only",
  "stop_condition":"criterion met","expansion_rule":"report and wait",
  "mode":"bounded-write","role":"implement","cwd":"$WORK/wt" }
JSON
"$CLI" --state "$WORK/state" state provision "$WORK/state" >/dev/null || { echo "FAIL: provision"; exit 1; }

export AI_OPS_ALLOW_LIVE_PROVIDER=1 AI_OPENCODE_TIMEOUT="$TIMEOUT"
BASE=(--json --profile "$WORK/profile.json" --state "$WORK/state" --provider "$PROVIDER")

echo "live smoke test  (scout/impl=$SCOUT_MODEL  review=$REVIEW_MODEL)"

# ------------------------------------------------------------------ scout ----
live_job "$WORK/scout.json" scout "$WORK/repo" "Name the bug in src/calc.py in one sentence."
[ "$(jq_ "$WORK/scout.json" "['status']")" = "ok" ] \
  && ok "scout completes against a live model" \
  || bad "scout" "$(head -c 200 "$WORK/scout.err")"
[ "$(jq_ "$WORK/scout.json" "['provider_calls']['forwarded']")" -ge 1 ] 2>/dev/null \
  && ok "request traversed the credential broker" \
  || bad "broker forwarding" "forwarded was 0 -- credential/broker path not exercised"

# ------------------------------------------------------------------ write ----
TOK=$("$CLI" --profile "$WORK/profile.json" --state "$WORK/state" lease acquire \
        --dir "$WORK/wt" --mode bounded-write --owner live | grep '^lease=' | cut -d= -f2)
[ -n "$TOK" ] && ok "lease acquired" || bad "lease" "no token"

AI_OPS_WRITE=1 live_job "$WORK/write.json" write "$WORK/wt" implement \
  --envelope "$WORK/envelope.json" --token "$TOK"
[ "$(jq_ "$WORK/write.json" "['status']")" = "awaiting_review" ] \
  && ok "write lands in awaiting_review (never ok on its own)" \
  || bad "write" "$(head -c 200 "$WORK/write.err")"
grep -q "ValueError" "$WORK/wt/src/calc.py" 2>/dev/null \
  && ok "model actually edited the file" || bad "edit" "no ValueError in calc.py"
[ "$(jq_ "$WORK/write.json" "['freeze']['changed_files']")" = "['src/calc.py']" ] \
  && ok "per-job delta attributed correctly" \
  || bad "delta" "got $(jq_ "$WORK/write.json" "['freeze']['changed_files']")"

SUBJ=$(jq_ "$WORK/write.json" "['job_id']")

# ----------------------------------------------------------------- review ----
live_job "$WORK/review.json" review "$WORK/wt" review "Review this change."
[ "$(jq_ "$WORK/review.json" "['status']")" = "ok" ] \
  && ok "review completes (diff supplied by the controller)" \
  || bad "review" "$(head -c 200 "$WORK/review.err")"
CALLS=$(jq_ "$WORK/review.json" "['provider_calls']['forwarded']")
[ "${CALLS:-99}" -le 6 ] 2>/dev/null \
  && ok "review is not looping (${CALLS} calls)" \
  || bad "review loop" "$CALLS calls -- reviewer may be hunting for the diff"
REV=$(jq_ "$WORK/review.json" "['job_id']")

# --------------------------------------------------------- negative gates ----
# Must run while the subject is STILL awaiting_review, or it tests the wrong
# gate (an already-promoted subject refuses for a different reason entirely).
git -C "$WORK/repo" worktree add -q "$WORK/other" -b other 2>/dev/null
live_job "$WORK/other.json" review "$WORK/other" review "Say ok." || true
OTHER=$(jq_ "$WORK/other.json" "['job_id']")
if [ -n "$OTHER" ]; then
  "$CLI" --profile "$WORK/profile.json" --state "$WORK/state" \
    promote --subject "$SUBJ" --review "$OTHER" >/dev/null 2>"$WORK/other.err"
  if grep -q "REFUSING" "$WORK/other.err"; then
    ok "cross-worktree review refused: $(grep -o 'REFUSING.*' "$WORK/other.err" | head -1 | cut -c1-60)"
  else
    bad "cross-worktree review" "promotion was NOT refused"
  fi
  STILL=$(python3 -c "import json;print(json.load(open('$WORK/state/jobs/$SUBJ/result.json'))['status'])" 2>/dev/null)
  [ "$STILL" = "awaiting_review" ] \
    && ok "subject survived the bogus promotion attempt" \
    || bad "subject state" "became $STILL after a refused promotion"
else
  bad "cross-worktree review" "could not create the control review job"
fi

# ---------------------------------------------------------------- promote ----
"$CLI" --profile "$WORK/profile.json" --state "$WORK/state" \
  promote --subject "$SUBJ" --review "$REV" > "$WORK/promote.json" 2>"$WORK/promote.err"
FINAL=$(python3 -c "import json;print(json.load(open('$WORK/state/jobs/$SUBJ/result.json'))['status'])" 2>/dev/null)
case "$FINAL" in
  ok)  ok "promoted to ok (reviewer approved with no blocking findings)" ;;
  awaiting_review)
       # A refusal here is a PASS: the gates are supposed to hold. Report which.
       ok "promotion refused, subject held: $(grep -o 'REFUSING.*' "$WORK/promote.err" | head -1)" ;;
  *)   bad "promote" "unexpected subject status: $FINAL" ;;
esac

echo
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
