# task envelope validation
# shellcheck shell=bash

validate_envelope_file() {
  local file="$1"
  [ -f "$file" ] || refuse "missing envelope $file"
  jsonutil validate "$AGENT_OPS_ROOT/schemas/task-envelope.schema.json" "$file" \
    || refuse "envelope failed schema: $file"
}

envelope_get() {
  jsonutil get "$1" "$2" "${3:-}"
}

write_min_envelope() {
  local dest="$1" mode="$2" role="$3" cwd="$4" prompt="$5"
  python3 - "$dest" "$mode" "$role" "$cwd" "$prompt" <<'PY'
import json, sys
dest, mode, role, cwd, prompt = sys.argv[1:6]
obj = {
  "goal": prompt or "run job",
  "context": prompt or "",
  "constraints": [],
  "done_when": ["job completes"],
  "non_goals": ["expand scope"],
  "risk_threshold": "only issues that violate the asked question",
  "stop_condition": "answer and stop",
  "expansion_rule": "report and wait",
  "mode": mode,
  "role": role,
  "cwd": cwd,
}
with open(dest, "w", encoding="utf-8") as fh:
    json.dump(obj, fh, indent=2)
    fh.write("\n")
PY
}
