# profile + policy loading
# shellcheck shell=bash

load_profile() {
  local given="${1:-}"
  if [ -n "$given" ]; then
    AI_OPS_PROFILE_FILE=$(readlink -f -- "$given")
  elif [ -n "${AI_OPS_PROFILE:-}" ]; then
    AI_OPS_PROFILE_FILE=$(readlink -f -- "$AI_OPS_PROFILE")
  else
    AI_OPS_PROFILE_FILE=$(readlink -f -- "$AGENT_OPS_ROOT/project-profiles/example.json")
  fi
  [ -f "$AI_OPS_PROFILE_FILE" ] || refuse "missing profile $AI_OPS_PROFILE_FILE"
  jsonutil validate "$AGENT_OPS_ROOT/schemas/project-profile.schema.json" "$AI_OPS_PROFILE_FILE" \
    || refuse "profile failed schema: $AI_OPS_PROFILE_FILE"
}

policy_file_for_mode() {
  case "$1" in
    readonly) echo "$AGENT_OPS_ROOT/policies/readonly.json" ;;
    bounded-write) echo "$AGENT_OPS_ROOT/policies/bounded-write.json" ;;
    *) refuse "unknown mode $1" ;;
  esac
}

write_enabled() {
  [ "$(profile_get write_enabled false)" = "true" ]
}

resolve_timeout() {
  local env_name default_s min max val
  env_name=$(profile_get timeout_env AI_OPENCODE_TIMEOUT)
  default_s=$(profile_get timeout_s 600)
  min=$(profile_get timeout_min 1)
  max=$(profile_get timeout_max 1800)
  if [ -n "${!env_name:-}" ]; then
    val=${!env_name}
  elif [ -n "${AI_OPENCODE_TIMEOUT:-}" ]; then
    val=$AI_OPENCODE_TIMEOUT
  else
    val=$default_s
  fi
  assert_timeout "$val" "$min" "$max"
  echo "$val"
}
