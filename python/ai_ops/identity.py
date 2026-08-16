from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from .env import GIT, git_clean_env
from .errors import Refuse
from .paths import reject_symlinks, require_absolute, stat_identity


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


def _git(cwd: str, *args: str) -> str:
    env = git_clean_env()
    proc = subprocess.run(
        [GIT, "-C", cwd, "--no-optional-locks", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise Refuse(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


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


def tree_digest(path: str) -> str:
    """NUL-safe content digest of porcelain + HEAD + diff."""
    from .digest import sha256_text

    head = _git(path, "rev-parse", "HEAD")
    status = subprocess.run(
        [GIT, "-C", path, "--no-optional-locks", "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
        env=git_clean_env(),
    ).stdout
    diff = subprocess.run(
        [GIT, "-C", path, "--no-optional-locks", "diff", "HEAD"],
        check=True,
        capture_output=True,
        env=git_clean_env(),
    ).stdout
    blob = head.encode() + b"\n" + status + b"\n" + diff
    import hashlib

    return hashlib.sha256(blob).hexdigest()


def git_identity_digest(path: str) -> str:
    from .digest import sha256_text

    head = _git(path, "rev-parse", "HEAD").strip()
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    remotes = _git(path, "remote", "-v")
    wtl = _git(path, "worktree", "list", "--porcelain")
    cfg = _git(path, "config", "--list", "--local")
    return sha256_text("\n".join([head, branch, remotes, wtl, cfg]))
