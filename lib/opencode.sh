# OpenCode provider adapter
# shellcheck shell=bash

agent_name_for_mode() {
  case "$1" in
    readonly) echo ai-ops-readonly ;;
    bounded-write) echo ai-ops-bounded-write ;;
    *) refuse "unknown mode $1" ;;
  esac
}

agent_file_for_mode() {
  case "$1" in
    readonly) echo "$AGENT_OPS_ROOT/adapters/opencode/agents/readonly.md" ;;
    bounded-write) echo "$AGENT_OPS_ROOT/adapters/opencode/agents/bounded-write.md" ;;
    *) refuse "unknown mode $1" ;;
  esac
}

runtime_file_for_mode() {
  case "$1" in
    readonly) echo "$AGENT_OPS_ROOT/adapters/opencode/runtime-readonly.json" ;;
    bounded-write) echo "$AGENT_OPS_ROOT/adapters/opencode/runtime-bounded-write.json" ;;
    *) refuse "unknown mode $1" ;;
  esac
}

pin_opencode_env() {
  local mode="$1" job_dir="$2"
  local runtime extra state_glob
  runtime=$(runtime_file_for_mode "$mode")
  [ -f "$runtime" ] || refuse "missing runtime $runtime"
  extra=$(profile_getj external_read || echo '[]')
  state_glob="$STATE/**"
  OPENCODE_CONFIG_CONTENT=$(jsonutil merge-runtime "$runtime" "$extra" "$state_glob")
  export OPENCODE_CONFIG_CONTENT
  export OPENCODE_DISABLE_PROJECT_CONFIG=1
}

discover_models() {
  echo "profile allowlist:"
  python3 - "$AI_OPS_PROFILE_FILE" <<'PY'
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
for m in (p.get("models") or {}).get("allow") or []:
    print(f"  {m}")
PY
  echo
  echo "live catalog:"
  if command -v opencode >/dev/null 2>&1; then
    opencode models 2>/dev/null | grep '^opencode-go/' || opencode models
  else
    echo "  (opencode not on PATH)"
  fi
}
