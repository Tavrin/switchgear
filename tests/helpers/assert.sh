# shellcheck shell=bash
# test helpers

assert_eq() {
  local got="$1" want="$2" msg="${3:-values differ}"
  if [ "$got" != "$want" ]; then
    echo "FAIL: $msg" >&2
    echo "  got:  $got" >&2
    echo "  want: $want" >&2
    return 1
  fi
}

assert_file() {
  [ -f "$1" ] || { echo "FAIL: missing file $1" >&2; return 1; }
}

assert_contains() {
  local hay="$1" needle="$2" msg="${3:-missing substring}"
  case "$hay" in
    *"$needle"*) return 0 ;;
    *) echo "FAIL: $msg" >&2; echo "  needle: $needle" >&2; return 1 ;;
  esac
}

assert_exit() {
  local want="$1"
  shift
  set +e
  "$@"
  local rc=$?
  set -e
  assert_eq "$rc" "$want" "exit code of $*"
}

pass() {
  echo "ok - $*"
}

write_profile() {
  # write_profile DEST [python assignments...]
  local dest="$1"
  shift
  python3 - "$AGENT_OPS_ROOT/project-profiles/example.json" "$dest" "$@" <<'PY'
import json, os, sys
src, dest = sys.argv[1], sys.argv[2]
profile = json.load(open(src, encoding="utf-8"))
state = os.environ["TEST_STATE"]
profile["state_dir"] = state
for expr in sys.argv[3:]:
    exec(expr, {"profile": profile})
os.makedirs(os.path.dirname(dest), exist_ok=True)
json.dump(profile, open(dest, "w", encoding="utf-8"), indent=2)
open(dest, "a", encoding="utf-8").write("\n")
PY
}

job_id_from_stdout() {
  awk -F= '/^job=/{print $2; exit}'
}

result_status() {
  local job="$1"
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' \
    "$TEST_STATE/jobs/$job/result.json"
}
