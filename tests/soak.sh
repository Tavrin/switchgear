#!/usr/bin/env bash
# Sustained concurrent load against one state root.
#
# Everything else in this suite verifies one job, or two. The properties that
# only fail under load were therefore unverified: does the concurrency cap hold
# when more jobs want slots than exist, do leases actually serialize writers
# across many worktrees, do markers and file descriptors leak over hundreds of
# jobs, does the state root stay consistent.
#
# Hermetic: the committed mock provider, no live calls, no spend. Opt-in because
# it takes minutes rather than seconds -- CI runs the fast suite.
#
#   bash tests/soak.sh [JOBS] [WORKTREES] [CONCURRENCY]
#
# Exits non-zero on the first invariant violation and says which.
set -euo pipefail

ROOT=$(cd "$(dirname -- "$0")/.." && pwd)
JOBS=${1:-60}
WORKTREES=${2:-4}
CAP=${3:-3}

PYTHON=/usr/bin/python3
MAIN="$ROOT/python/ai_ops/__main__.py"
MOCK="$ROOT/tests/helpers/mock_provider.py"

WORK=$(mktemp -d -t aiops-soak-XXXXXX)
STATE="$WORK/state"
BUDGET="$WORK/budget.json"
trap 'rm -rf "$WORK"' EXIT

echo "== soak: $JOBS jobs across $WORKTREES worktrees, concurrency cap $CAP =="
echo "   work dir: $WORK"

printf '{"max_concurrent_jobs": %d}\n' "$CAP" > "$BUDGET"
export AI_OPS_BUDGET_FILE="$BUDGET"
export AI_OPS_PROVIDER="$MOCK"
# Keep the cool-down out of the way; this run is minutes long, not hours.
export AI_OPS_CONCURRENCY_WAIT_S=120

"$PYTHON" "$MAIN" --state "$STATE" state provision "$STATE" >/dev/null

# --- worktrees -------------------------------------------------------------
declare -a TREES=()
for i in $(seq 1 "$WORKTREES"); do
  out=$(bash "$ROOT/tests/helpers/make-synthetic-repo" "$WORK/repo$i")
  TREES+=("$(echo "$out" | sed -n 's/^PRIMARY=//p')")
done

PROFILE="$WORK/profile.json"
"$PYTHON" - "$ROOT" "$PROFILE" <<'PY'
import json, sys
root, out = sys.argv[1], sys.argv[2]
d = json.load(open(f"{root}/project-profiles/example.json"))
d["write_enabled"] = True
json.dump(d, open(out, "w"), indent=2)
PY

fds_before=$(ls /proc/self/fd | wc -l)

# --- launch ----------------------------------------------------------------
echo "-- launching --"
launched=0
for n in $(seq 1 "$JOBS"); do
  tree=${TREES[$(( (n - 1) % WORKTREES ))]}
  "$PYTHON" "$MAIN" --profile "$PROFILE" --state "$STATE" --provider "$MOCK" \
      --json scout "$tree" "soak $n" --background >/dev/null 2>&1 &
  launched=$((launched + 1))
  # Do not fork faster than the machine can start them; the cap is what is
  # under test, not the shell's ability to spawn.
  if (( n % 10 == 0 )); then wait; fi
done
wait
echo "   launched $launched"

# --- watch the cap while they drain ---------------------------------------
echo "-- draining (watching the cap) --"
peak=0
deadline=$(( $(date +%s) + 600 ))
while :; do
  counts=$("$PYTHON" "$MAIN" --state "$STATE" --json jobs --all 2>/dev/null \
            | "$PYTHON" -c 'import json,sys
rows=json.load(sys.stdin)["jobs"]
print(sum(1 for r in rows if r["state"]=="running"), sum(1 for r in rows if r["state"]=="queued"))')
  running=${counts%% *}
  queued=${counts##* }
  markers=$(ls "$STATE/running" 2>/dev/null | wc -l)
  (( running > peak )) && peak=$running
  if (( markers > CAP )); then
    echo "FAIL: $markers concurrency markers held, cap is $CAP" >&2
    exit 1
  fi
  # `running` must mean EXECUTING. A job alive but waiting for a slot is
  # `queued`; conflating them reported 11 running against a cap of 2.
  if (( running > CAP )); then
    echo "FAIL: $running jobs report state=running, cap is $CAP (queued=$queued)" >&2
    exit 1
  fi
  (( running == 0 && queued == 0 )) && break
  if (( $(date +%s) > deadline )); then
    echo "FAIL: jobs still running after 10 minutes" >&2
    exit 1
  fi
  sleep 1
done
echo "   peak concurrent: $peak (cap $CAP)"

# --- invariants ------------------------------------------------------------
echo "-- checking invariants --"
SOAK_ROOT="$ROOT" "$PYTHON" - "$STATE" "$JOBS" "$CAP" <<'PY'
import json, os, subprocess, sys
state, expected, cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
main = os.path.join(os.environ["SOAK_ROOT"], "python", "ai_ops", "__main__.py")
def cli(*args):
    return json.loads(subprocess.run(
        ["/usr/bin/python3", main, "--state", state, "--json", *args],
        capture_output=True, text=True, check=True).stdout)

listing = cli("jobs", "--all")
rows = listing["jobs"]
fail = []

if len(rows) != expected:
    fail.append(f"expected {expected} jobs, listing shows {len(rows)}")

bad = [r for r in rows if r["state"] not in ("ok", "died", "cancelled")]
if bad:
    fail.append(f"{len(bad)} job(s) in an unexpected terminal state: "
                + ", ".join(f"{r['job_id'][:8]}={r['state']}" for r in bad[:5]))

ok = [r for r in rows if r["state"] == "ok"]
if len(ok) != expected:
    fail.append(f"{expected - len(ok)} job(s) did not complete cleanly")

# Every completed job must have an evidence stream and a queue time.
for r in ok:
    rec_path = os.path.join(state, "jobs", r["job_id"], "result.json")
    rec = json.load(open(rec_path))
    if "queued_s" not in rec:
        fail.append(f"{r['job_id'][:8]} has no queued_s"); break
    ev = (rec.get("artifacts") or {}).get("events")
    if not ev or not os.path.isfile(ev):
        fail.append(f"{r['job_id'][:8]} has no evidence stream"); break

# No concurrency markers may survive.
markers = os.listdir(os.path.join(state, "running")) if os.path.isdir(os.path.join(state, "running")) else []
if markers:
    fail.append(f"{len(markers)} concurrency marker(s) left behind: {markers[:3]}")

# No lease may still be held.
leases = os.path.join(state, "leases")
held = []
if os.path.isdir(leases):
    for key in os.listdir(leases):
        if os.path.isfile(os.path.join(leases, key, "token.json")):
            held.append(key)
if held:
    fail.append(f"{len(held)} lease(s) still held after every job finished")

# Queueing must have actually happened, or the cap was never exercised and this
# run proves nothing about it.
queued = [json.load(open(os.path.join(state, "jobs", r["job_id"], "result.json"))).get("queued_s", 0)
          for r in ok]
if not any(q and q > 0 for q in queued):
    fail.append("no job ever waited for a slot — the cap was never exercised, "
                "so this run does not test it")

if fail:
    print("FAIL:", file=sys.stderr)
    for f in fail:
        print("  -", f, file=sys.stderr)
    sys.exit(1)

print(f"   {len(ok)}/{expected} completed, max queue wait {max(queued):.1f}s")
PY

fds_after=$(ls /proc/self/fd | wc -l)
echo "   fds: $fds_before -> $fds_after"

echo "-- doctor on the soaked state root --"
"$PYTHON" "$MAIN" --state "$STATE" doctor | tail -1

echo
echo "SOAK PASSED"
