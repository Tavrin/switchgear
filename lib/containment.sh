# OS containment interface. Stage 0 does not wrap a real provider.
# If the profile requires a backend that is missing, refuse. No silent fallback.
# shellcheck shell=bash

containment_backend_ok() {
  local mode="${1:-none}"
  case "$mode" in
    none) return 0 ;;
    bwrap)
      if [ -n "${AI_OPS_BWRAP:-}" ]; then
        [ -x "$AI_OPS_BWRAP" ] && return 0
        return 1
      fi
      command -v bwrap >/dev/null 2>&1
      ;;
    *) return 1 ;;
  esac
}

assert_containment() {
  local mode required
  mode=$(profile_get containment.mode none)
  required=$(profile_get containment.required false)
  if [ "$required" = "true" ]; then
    if ! containment_backend_ok "$mode"; then
      refuse "containment.required=true but backend '$mode' is unavailable (no silent fallback)"
    fi
  fi
  if [ "$mode" = "bwrap" ] && [ "$required" != "true" ]; then
    # requested but not required: still refuse if missing, do not silently drop
    if ! containment_backend_ok "$mode"; then
      refuse "containment.mode=bwrap but bwrap is unavailable (no silent fallback)"
    fi
  fi
}
