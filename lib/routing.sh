# role -> model, allow/deny
# shellcheck shell=bash

role_model_id() {
  local role="$1"
  local id
  id=$(profile_get "roles.${role}.model" "")
  [ -n "$id" ] || { echo "unknown role: $role" >&2; return 1; }
  echo "$id"
}

role_mode() {
  local role="$1"
  profile_get "roles.${role}.mode" ""
}

assert_model_allowed() {
  local model="$1"
  jsonutil allowed "$AI_OPS_PROFILE_FILE" "$model" >/dev/null \
    || refuse "model '$model' is not allowed by the profile"
}

model_object() {
  local model="$1"
  jsonutil expand-model "$AI_OPS_PROFILE_FILE" "$model"
}
