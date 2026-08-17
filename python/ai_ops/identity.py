from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from .env import GIT, git_clean_env
from .errors import Refuse
from .paths import is_within, reject_symlinks, require_absolute, stat_identity


@dataclass(frozen=True)
class WorktreeIdentity:
    realpath: str
    st_dev: int
    st_ino: int
    git_dir: str
    common_git_dir: str
    common_dev: int
    common_ino: int
    head: str
    branch: str
    linked_worktree: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Repo-local config knobs that make git execute an arbitrary command. A worker that
# can write inside its worktree must never be able to reach these through the host
# controller's git invocations.
# Note: there is no way to *unset* diff.external via -c (an empty value makes git
# exec ""), so it is not listed here. The real defence is --git-dir pinning below:
# config is read from the trusted git dir, never from a repo the worker controls.
_SAFE_CONFIG = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.ext.allow=never",
)


def _git(cwd: str, *args: str) -> str:
    """Discovery-only git. Follows the in-tree .git pointer, so it must stay
    restricted to commands that never scan the tree (rev-parse and friends)."""
    env = git_clean_env()
    proc = subprocess.run(
        [GIT, "-C", cwd, "--no-optional-locks", *_SAFE_CONFIG, *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise Refuse(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _git_pinned(ident: "WorktreeIdentity", *args: str, text: bool = True):
    """Git pinned to the validated git dir.

    The worktree is worker-writable in bounded-write mode, and for a linked
    worktree `.git` is a regular file *inside* it. Passing --git-dir means git
    never consults that pointer, so a worker cannot redirect the controller's
    git at a repository whose config it also controls (textconv / fsmonitor).
    """
    env = git_clean_env()
    proc = subprocess.run(
        [
            GIT,
            "--git-dir",
            ident.git_dir,
            "--work-tree",
            ident.realpath,
            "--no-optional-locks",
            *_SAFE_CONFIG,
            *args,
        ],
        check=False,
        capture_output=True,
        text=text,
        env=env,
        cwd=ident.realpath,
    )
    if proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        raise Refuse(f"git {' '.join(args)} failed: {err.strip()}")
    return proc.stdout


def assert_gitdir_pointer_intact(ident: "WorktreeIdentity") -> None:
    """Refuse if the worktree's .git pointer no longer resolves to the git dir we
    validated. Makes the ordering guarantee explicit instead of incidental."""
    git_file = os.path.join(ident.realpath, ".git")
    if os.path.islink(git_file):
        raise Refuse("worktree .git became a symlink during the job")
    if ident.linked_worktree:
        try:
            with open(git_file, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise Refuse(f"worktree .git pointer unreadable: {exc}") from exc
        if raw.count("gitdir:") != 1:
            raise Refuse("worktree .git pointer is malformed")
        target = raw.split("gitdir:", 1)[1].strip()
        if not os.path.isabs(target):
            target = os.path.abspath(os.path.join(ident.realpath, target))
        if os.path.realpath(target) != os.path.realpath(ident.git_dir):
            raise Refuse("worktree .git pointer was redirected during the job")
    elif not os.path.isdir(git_file):
        raise Refuse("worktree .git is no longer a directory")


def inspect_worktree(path: str) -> WorktreeIdentity:
    require_absolute(path, "worktree")
    abs_path = reject_symlinks(os.path.abspath(path), "worktree")
    if not os.path.isdir(abs_path):
        raise Refuse(f"no such directory: {abs_path}")
    git_file = os.path.join(abs_path, ".git")
    if not os.path.lexists(git_file):
        raise Refuse(f"{abs_path} is not a git worktree")
    inside = _git(abs_path, "rev-parse", "--is-inside-work-tree").strip()
    if inside != "true":
        raise Refuse(f"{abs_path} is not a git worktree")
    toplevel = os.path.abspath(_git(abs_path, "rev-parse", "--show-toplevel").strip())
    if os.path.realpath(toplevel) != os.path.realpath(abs_path):
        raise Refuse(f"toplevel {toplevel} != {abs_path}")
    git_dir = os.path.abspath(_git(abs_path, "rev-parse", "--absolute-git-dir").strip())
    common = _git(abs_path, "rev-parse", "--git-common-dir").strip()
    if not os.path.isabs(common):
        common = os.path.abspath(os.path.join(abs_path, common))
    else:
        common = os.path.abspath(common)
    head = _git(abs_path, "rev-parse", "HEAD").strip()
    branch = _git(abs_path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    linked = os.path.isfile(git_file) and not os.path.islink(git_file)
    if os.path.islink(git_file):
        raise Refuse("refusing symlink .git")

    # Git-dir LEGITIMACY, not merely stability. git_dir/common_git_dir above were
    # derived by following the in-tree .git pointer, which is worker-writable in
    # bounded-write mode. A poisoned pointer SURVIVES the job that wrote it (that
    # job refuses, but the bytes are already on disk), so a later job would adopt
    # a worker-owned repository as authoritative and every downstream digest,
    # freeze and promotion comparison would be computed against it.
    if linked:
        # A real linked worktree's git dir lives under the primary's
        # .git/worktrees/, never inside the worktree itself.
        if is_within(git_dir, abs_path):
            raise Refuse(f"git dir {git_dir} is inside the worktree (poisoned .git pointer)")
        if is_within(common, abs_path):
            raise Refuse(f"common git dir {common} is inside the worktree (poisoned .git pointer)")
        if not is_within(git_dir, common):
            raise Refuse("linked worktree git dir is not a member of its common git dir")
    else:
        # Primary checkout: the git dir must be exactly <worktree>/.git.
        expected = os.path.join(abs_path, ".git")
        if os.path.realpath(git_dir) != os.path.realpath(expected):
            raise Refuse(f"primary checkout git dir {git_dir} is not {expected}")
    # do not follow symlinks on the worktree root
    dev, ino = stat_identity(abs_path)
    cdev, cino = stat_identity(common if os.path.isdir(common) else os.path.dirname(common))
    return WorktreeIdentity(
        realpath=abs_path,
        st_dev=dev,
        st_ino=ino,
        git_dir=git_dir,
        common_git_dir=common,
        common_dev=cdev,
        common_ino=cino,
        head=head,
        branch=branch,
        linked_worktree=linked,
    )


def require_linked(ident: WorktreeIdentity) -> None:
    if not ident.linked_worktree:
        raise Refuse(f"{ident.realpath} is not a linked worktree (.git is not a file)")


def same_identity(a: WorktreeIdentity, b: WorktreeIdentity) -> bool:
    return (
        a.realpath == b.realpath
        and a.st_dev == b.st_dev
        and a.st_ino == b.st_ino
        and a.git_dir == b.git_dir
        and a.common_git_dir == b.common_git_dir
        and a.head == b.head
    )


def identity_core(a: WorktreeIdentity) -> dict[str, Any]:
    """Identity without HEAD (HEAD may change on write of files, not git)."""
    return {
        "realpath": a.realpath,
        "st_dev": a.st_dev,
        "st_ino": a.st_ino,
        "git_dir": a.git_dir,
        "common_git_dir": a.common_git_dir,
        "common_dev": a.common_dev,
        "common_ino": a.common_ino,
    }


def same_core(a: WorktreeIdentity, b: WorktreeIdentity) -> bool:
    return identity_core(a) == identity_core(b)


def tree_digest(ident: WorktreeIdentity) -> str:
    """NUL-safe content digest of porcelain + HEAD + diff.

    These are the tree-scanning commands, so they run pinned to the validated
    git dir and only after the .git pointer has been re-checked.
    """
    import hashlib

    assert_gitdir_pointer_intact(ident)
    head = _git_pinned(ident, "rev-parse", "HEAD")
    status = _git_pinned(ident, "status", "--porcelain=v1", "-z", text=False)
    # --no-ext-diff / --no-textconv are the only reliable way to stop a
    # repository-owned diff.external or diff.<d>.textconv from executing on the
    # host. --git-dir pinning does not help when the repository's OWN config is
    # hostile (a disposable clone), which is the case this rail must survive.
    diff = _git_pinned(ident, "diff", "--no-ext-diff", "--no-textconv", "HEAD", text=False)
    blob = head.encode() + b"\n" + status + b"\n" + diff
    return hashlib.sha256(blob).hexdigest()


def changed_files(ident: WorktreeIdentity) -> list[str]:
    """Paths the subject job actually touched, tracked + untracked.

    Used to check that a reviewer names the change it claims to have reviewed.
    """
    assert_gitdir_pointer_intact(ident)
    out = _git_pinned(ident, "status", "--porcelain=v1", "-z", text=False)
    names: list[str] = []
    for entry in out.split(b"\x00"):
        if not entry:
            continue
        # porcelain v1 -z: XY<space>path ; rename targets arrive as a separate record
        path = entry[3:].decode("utf-8", "replace").strip()
        if path:
            names.append(path)
    return sorted(set(names))


def git_identity_digest(ident: WorktreeIdentity) -> str:
    from .digest import sha256_text

    assert_gitdir_pointer_intact(ident)
    head = _git_pinned(ident, "rev-parse", "HEAD").strip()
    branch = _git_pinned(ident, "rev-parse", "--abbrev-ref", "HEAD").strip()
    remotes = _git_pinned(ident, "remote", "-v")
    wtl = _git_pinned(ident, "worktree", "list", "--porcelain")
    cfg = _git_pinned(ident, "config", "--list", "--local")
    return sha256_text("\n".join([head, branch, remotes, wtl, cfg]))
