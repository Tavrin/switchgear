# quota hook: argv array from profile, never a shell string
# shellcheck shell=bash

run_quota_hook() {
  local raw
  raw=$(profile_getj quota_hook || true)
  [ -n "$raw" ] && [ "$raw" != "null" ] || return 0
  python3 - "$raw" <<'PY' || refuse "quota hook refused"
import json, os, sys
argv = json.loads(sys.argv[1])
if not isinstance(argv, list) or not argv:
    sys.exit(0)
os.execvp(argv[0], argv)
PY
}
