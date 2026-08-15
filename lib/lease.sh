# atomic worktree leases (flock + pid/starttime/boot_id)
# shellcheck shell=bash

lease_hash() {
  printf '%s' "$1" | sha256sum | awk '{print $1}'
}

lease_file_for() {
  echo "$STATE/leases/$(lease_hash "$1").json"
}

lease_lock() {
  mkdir -p "$STATE/leases"
  exec 9>"$STATE/leases/.lock"
  flock 9
}

lease_unlock() {
  flock -u 9
  exec 9>&-
}

lease_is_live() {
  local file="$1"
  [ -f "$file" ] || return 1
  local pid start boot
  pid=$(jsonutil get "$file" owner_pid)
  start=$(jsonutil get "$file" owner_starttime)
  boot=$(jsonutil get "$file" boot_id)
  [ "$(boot_id)" = "$boot" ] || return 1
  proc_alive_match "$pid" "$start"
}

lease_acquire() {
  local dir="$1" owner="$2" owner_pid="${3:-}" mode="${4:-bounded-write}"
  local abs
  abs=$(readlink -f -- "$dir")
  [ -d "$abs" ] || refuse "lease dir missing: $dir"
  if [ -z "$owner_pid" ]; then
    owner_pid=$PPID
  fi
  is_digits "$owner_pid" || refuse "lease owner pid must be digits"
  local start
  start=$(proc_starttime "$owner_pid") || refuse "cannot read starttime for pid $owner_pid"

  lease_lock
  local lf replaced=false
  lf=$(lease_file_for "$abs")
  if [ -f "$lf" ]; then
    if lease_is_live "$lf"; then
      local existing
      existing=$(jsonutil get "$lf" owner)
      lease_unlock
      refuse "live lease already held on $abs by $existing"
    fi
    replaced=true
  fi
  local uuid
  uuid=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
  python3 - "$lf" "$uuid" "$owner" "$owner_pid" "$start" "$(boot_id)" "$(utc_now)" "$abs" "$mode" "$replaced" <<'PY'
import json, sys
path, uuid, owner, pid, start, boot, acquired, realpath, mode, replaced = sys.argv[1:]
obj = {
  "lease_uuid": uuid,
  "owner": owner,
  "owner_pid": int(pid),
  "owner_starttime": start,
  "boot_id": boot,
  "acquired_at": acquired,
  "realpath": realpath,
  "mode": mode,
  "replaced_stale": replaced == "true",
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(obj, fh, indent=2)
    fh.write("\n")
PY
  lease_unlock
  echo "lease=$(jsonutil get "$lf" lease_uuid)"
  echo "file=$lf"
  echo "replaced_stale=$replaced"
}

lease_release() {
  local dir="$1"
  local abs lf
  abs=$(readlink -f -- "$dir")
  lf=$(lease_file_for "$abs")
  lease_lock
  rm -f -- "$lf"
  lease_unlock
}

lease_show() {
  local dir="$1"
  local abs lf
  abs=$(readlink -f -- "$dir")
  lf=$(lease_file_for "$abs")
  [ -f "$lf" ] || { echo "no lease for $abs" >&2; return 1; }
  cat "$lf"
  if lease_is_live "$lf"; then
    echo "live=true"
  else
    echo "live=false"
  fi
}

assert_live_lease() {
  local abs="$1"
  local lf
  lf=$(lease_file_for "$abs")
  [ -f "$lf" ] || refuse "no lease for $abs"
  lease_is_live "$lf" || refuse "lease for $abs is not live (pid/starttime/boot mismatch)"
}
