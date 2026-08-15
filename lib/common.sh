# shared helpers for ai-opencode. sourced, not executed.
# shellcheck shell=bash

is_digits() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

die() {
  echo "ai-opencode: $*" >&2
  exit 1
}

refuse() {
  echo "ai-opencode: REFUSING — $*" >&2
  exit 1
}

abs_path() {
  readlink -f -- "$1"
}

jsonutil() {
  python3 "$AGENT_OPS_ROOT/lib/jsonutil.py" "$@"
}

profile_get() {
  jsonutil get "$AI_OPS_PROFILE_FILE" "$@"
}

profile_getj() {
  jsonutil getj "$AI_OPS_PROFILE_FILE" "$@"
}

utc_now() {
  date -u +%Y%m%dT%H%M%SZ
}

boot_id() {
  if [ -r /proc/sys/kernel/random/boot_id ]; then
    tr -d '[:space:]' < /proc/sys/kernel/random/boot_id
  else
    echo "unknown-boot"
  fi
}

proc_starttime() {
  local pid="$1"
  [ -r "/proc/$pid/stat" ] || return 1
  awk '{print $22}' "/proc/$pid/stat"
}

proc_alive_match() {
  local pid="$1" start="$2"
  [ -d "/proc/$pid" ] || return 1
  local now
  now=$(proc_starttime "$pid") || return 1
  [ "$now" = "$start" ]
}

safe_token() {
  [[ "$1" =~ ^[A-Za-z0-9._/@:+=-]+$ ]]
}

has_meta() {
  case "$1" in
    *';'*|*'&'*|*'|'*|*'`'|*'$'*|*'('*|*')'*|*'<'*|*'>'*|*$'\n'*|*$'\r'*|*'*'*|*'?'*|*'!'*|*'['*)
      return 0
      ;;
  esac
  return 1
}

expand_state_dir() {
  local raw
  if [ -n "${AI_OPS_STATE:-}" ]; then
    raw=$AI_OPS_STATE
  else
    raw=$(profile_get state_dir)
  fi
  raw=${raw//\$\{HOME\}/$HOME}
  raw=${raw//\$HOME/$HOME}
  mkdir -p "$raw/jobs" "$raw/leases"
  printf '%s' "$raw"
}

path_is_inside() {
  local inner="$1" outer="$2"
  case "$inner" in
    "$outer"|"$outer"/*) return 0 ;;
    *) return 1 ;;
  esac
}

realpath_nofollow_parent() {
  # resolve existing path; if missing, resolve parent
  if [ -e "$1" ]; then
    readlink -f -- "$1"
  else
    local parent
    parent=$(dirname -- "$1")
    printf '%s/%s' "$(readlink -f -- "$parent")" "$(basename -- "$1")"
  fi
}
