# session / process-group ownership
# shellcheck shell=bash

collect_descendants() {
  local root="$1"
  python3 - "$root" <<'PY'
import os, sys
root = int(sys.argv[1])
children = {}
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            st = fh.read()
        # comm may contain spaces/parens; ppid is after the last )
        rest = st[st.rfind(")") + 2 :].split()
        ppid = int(rest[1])
        children.setdefault(ppid, []).append(int(pid))
    except (OSError, ValueError, IndexError):
        continue
seen = []
stack = [root]
while stack:
    cur = stack.pop()
    if cur in seen:
        continue
    seen.append(cur)
    stack.extend(children.get(cur, []))
print(" ".join(str(p) for p in seen))
PY
}

pgid_of() {
  local pid="$1"
  python3 - "$pid" <<'PY'
import sys
pid = sys.argv[1]
try:
    st = open(f"/proc/{pid}/stat", encoding="utf-8").read()
    rest = st[st.rfind(")") + 2 :].split()
    print(rest[2])  # pgrp
except Exception:
    sys.exit(1)
PY
}

count_alive() {
  local n=0 p
  for p in "$@"; do
    [ -n "$p" ] || continue
    [ -d "/proc/$p" ] && n=$((n + 1))
  done
  echo "$n"
}

# run_in_session timeout_s out_file err_file -- argv...
# sets: PROC_PID PROC_PGID PROC_RC PROC_TIMED_OUT PROC_ORPHANS
run_in_session() {
  local timeout_s="$1" out_file="$2" err_file="$3"
  shift 3
  [ "${1:-}" = "--" ] && shift

  local pid pgid grace=20
  PROC_TIMED_OUT=0
  PROC_ORPHANS=0
  PROC_PID=""
  PROC_PGID=""
  PROC_RC=""

  setsid "$@" >"$out_file" 2>"$err_file" &
  pid=$!
  PROC_PID=$pid
  # leader may take a moment to appear in /proc
  local i
  for i in 1 2 3 4 5; do
    [ -d "/proc/$pid" ] && break
    sleep 0.05
  done
  pgid=$(pgid_of "$pid")
  PROC_PGID=${pgid:-$pid}

  local elapsed=0 rc=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$elapsed" -ge "$timeout_s" ]; then
      PROC_TIMED_OUT=1
      break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  if [ "$PROC_TIMED_OUT" -eq 1 ]; then
    local descendants
    descendants=$(collect_descendants "$pid")
    if [ -n "$PROC_PGID" ]; then
      kill -TERM -- "-$PROC_PGID" 2>/dev/null || true
    fi
    kill -TERM $descendants 2>/dev/null || true
    local g=0
    while [ "$g" -lt "$grace" ]; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
      g=$((g + 1))
    done
    if [ -n "$PROC_PGID" ]; then
      kill -KILL -- "-$PROC_PGID" 2>/dev/null || true
    fi
    kill -KILL $descendants 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    PROC_RC=124
    sleep 0.2
    # recount
    local leftover=0 p
    for p in $descendants; do
      if [ -d "/proc/$p" ]; then
        leftover=$((leftover + 1))
      fi
    done
    PROC_ORPHANS=$leftover
    return "$PROC_RC"
  fi

  set +e
  wait "$pid"
  rc=$?
  set -e
  PROC_RC=$rc
  return "$rc"
}
