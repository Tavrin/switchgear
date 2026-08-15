# cwd / worktree / config / state isolation checks
# shellcheck shell=bash

assert_git_workdir() {
  local abs="$1"
  [ -d "$abs" ] || refuse "no such directory: $abs"
  if [ ! -e "$abs/.git" ] || ! git -C "$abs" --no-optional-locks rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    refuse "$abs is not a git repo/worktree"
  fi
}

assert_linked_worktree() {
  local abs="$1"
  if [ ! -f "$abs/.git" ]; then
    refuse "$abs is not a linked worktree (.git is not a file)"
  fi
  local gitdir line
  line=$(tr -d '\r' < "$abs/.git")
  gitdir=${line#gitdir: }
  gitdir=${gitdir#gitdir:}
  gitdir=$(echo "$gitdir" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if [[ "$gitdir" != /* ]]; then
    gitdir=$(readlink -f -- "$abs/$gitdir")
  else
    gitdir=$(readlink -f -- "$gitdir")
  fi
  [ -d "$gitdir" ] || refuse "worktree gitdir missing: $gitdir"
  local toplevel
  toplevel=$(git -C "$abs" --no-optional-locks rev-parse --show-toplevel)
  toplevel=$(readlink -f -- "$toplevel")
  [ "$toplevel" = "$abs" ] || refuse "toplevel $toplevel != $abs (gitdir indirection)"
}

assert_state_isolated() {
  local state="$1" target="$2"
  local rs rt
  rs=$(readlink -f -- "$state")
  rt=$(readlink -f -- "$target")
  [ "$rs" != "$rt" ] || refuse "result dir $state is the target"
  path_is_inside "$rs" "$rt" && refuse "result dir $state is inside $rt"
  if [ -L "$state" ]; then
    path_is_inside "$rs" "$rt" && refuse "state dir is a symlink into the target"
  fi
}

assert_profile_path_safe() {
  local profile="$1" target="$2"
  local rp rt
  rp=$(readlink -f -- "$profile")
  [ -f "$rp" ] || refuse "profile not a file: $profile"
  if [ -n "$target" ] && [ -d "$target" ]; then
    rt=$(readlink -f -- "$target")
    path_is_inside "$rp" "$rt" && refuse "profile path resolves inside the target worktree"
  fi
  case "$profile" in
    *../*|*/..|../*) refuse "profile path traversal" ;;
  esac
}

assert_opencode_config_dir() {
  if [ -n "${OPENCODE_CONFIG_DIR:-}" ] && [ "$OPENCODE_CONFIG_DIR" != "$HOME/.config/opencode" ]; then
    refuse "OPENCODE_CONFIG_DIR=$OPENCODE_CONFIG_DIR"
  fi
}

assert_timeout() {
  local timeout_s="$1" min="$2" max="$3"
  if ! is_digits "$timeout_s" || [ "$timeout_s" -lt "$min" ] || [ "$timeout_s" -gt "$max" ]; then
    refuse "timeout '$timeout_s' must be ${min}..${max} (0 is not a disable)"
  fi
}
