# tree + git-identity snapshots
# shellcheck shell=bash

snapshot_tree() {
  local dir="$1"
  git -C "$dir" --no-optional-locks rev-parse HEAD
  echo "---STATUS---"
  git -C "$dir" --no-optional-locks status --porcelain=v1
  echo "---DIFF---"
  git -C "$dir" --no-optional-locks diff HEAD | sha256sum | awk '{print $1}'
  echo "---FILES---"
  git -C "$dir" --no-optional-locks status --porcelain=v1 | while IFS= read -r line; do
    [ -n "$line" ] || continue
    local path="${line:3}"
    if [[ "$path" == *" -> "* ]]; then
      path="${path##* -> }"
    fi
    if [ -L "$dir/$path" ]; then
      echo "SYMLINK $path -> $(readlink -- "$dir/$path")"
      echo "SYMLINK_REAL $path -> $(readlink -f -- "$dir/$path" 2>/dev/null || echo MISSING)"
    elif [ -f "$dir/$path" ]; then
      sha256sum -- "$dir/$path"
    elif [ -d "$dir/$path" ]; then
      echo -n "$path "
      find "$dir/$path" -type f -print0 | sort -z | xargs -0 -r sha256sum | sha256sum
    else
      echo "MISSING $path"
    fi
  done
}

snapshot_git_identity() {
  local dir="$1"
  echo "HEAD=$(git -C "$dir" --no-optional-locks rev-parse HEAD)"
  echo "BRANCH=$(git -C "$dir" --no-optional-locks rev-parse --abbrev-ref HEAD)"
  echo "---REMOTES---"
  git -C "$dir" --no-optional-locks remote -v
  echo "---WORKTREES---"
  git -C "$dir" --no-optional-locks worktree list --porcelain
  echo "---CONFIG---"
  git -C "$dir" --no-optional-locks config --list --local
}

snapshot_other_worktrees() {
  local dir="$1"
  local toplevel common
  toplevel=$(git -C "$dir" --no-optional-locks rev-parse --show-toplevel)
  common=$(git -C "$dir" --no-optional-locks rev-parse --git-common-dir)
  git -C "$dir" --no-optional-locks worktree list --porcelain | awk '/^worktree /{print $2}' | while IFS= read -r wt; do
    [ -n "$wt" ] || continue
    [ "$wt" = "$toplevel" ] && continue
    echo "WT $wt"
    echo "  HEAD=$(git -C "$wt" --no-optional-locks rev-parse HEAD 2>/dev/null || echo missing)"
    echo "  STATUS=$(git -C "$wt" --no-optional-locks status --porcelain=v1 | sha256sum | awk '{print $1}')"
  done
  echo "COMMON=$common"
}

snapshot_canaries() {
  local spec="${AI_OPS_CANARIES:-}"
  [ -n "$spec" ] || return 0
  echo "---CANARIES---"
  IFS=':' read -r -a files <<< "$spec"
  local f
  for f in "${files[@]}"; do
    [ -n "$f" ] || continue
    if [ -f "$f" ]; then
      sha256sum -- "$f"
    elif [ -e "$f" ]; then
      echo "EXISTS $f"
    else
      echo "MISSING $f"
    fi
  done
}

snapshot_outside_bindings() {
  # Paths in the worktree that resolve outside it, plus target hashes.
  # Pre-existing outbound symlinks are recorded; a job fails only if this set changes.
  local dir="$1"
  python3 - "$dir" <<'PY'
import hashlib, os, subprocess, sys
abs_dir = os.path.realpath(sys.argv[1])
out = subprocess.check_output(
    ["git", "-C", abs_dir, "--no-optional-locks", "status", "--porcelain=v1"],
    text=True,
)
# also walk obvious link names even if gitignore hides them
candidates = []
for line in out.splitlines():
    if not line.strip():
        continue
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    candidates.append(path)
for root, dirs, files in os.walk(abs_dir):
    if "/.git" in root or root.endswith("/.git"):
        continue
    for name in dirs + files:
        full = os.path.join(root, name)
        if os.path.islink(full):
            rel = os.path.relpath(full, abs_dir)
            if rel not in candidates:
                candidates.append(rel)
for path in sorted(set(candidates)):
    full = os.path.join(abs_dir, path)
    if not (os.path.islink(full) or os.path.exists(full)):
        print(f"MISSING {path}")
        continue
    real = os.path.realpath(full)
    if real == abs_dir or real.startswith(abs_dir + os.sep):
        continue
    digest = "NA"
    if os.path.isfile(real):
        h = hashlib.sha256()
        with open(real, "rb") as fh:
            h.update(fh.read())
        digest = h.hexdigest()
    kind = "symlink" if os.path.islink(full) else "path"
    print(f"{kind} {path} -> {real} {digest}")
PY
}

write_tree_snapshot_file() {
  local dir="$1" dest="$2" helper="$3"
  {
    echo "dir=$dir"
    echo "HEAD=$("$helper" --dir "$dir" head)"
    echo
    echo "=== status ==="
    "$helper" --dir "$dir" status || true
    echo
    echo "=== log ==="
    "$helper" --dir "$dir" log || true
    echo
    echo "=== diff --stat ==="
    "$helper" --dir "$dir" diff-stat || true
  } > "$dest"
}
