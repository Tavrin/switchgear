from __future__ import annotations

import os
import stat
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
MAX_GIT_OUTPUT = 64 * 1024 * 1024

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
    import tempfile

    env = git_clean_env()
    argv = [
        GIT,
        "--git-dir",
        ident.git_dir,
        "--work-tree",
        ident.realpath,
        "--no-optional-locks",
        *_SAFE_CONFIG,
        *args,
    ]
    # Capture through a temp file, not controller memory: a worker can inflate a
    # tracked file to gigabytes, and `git diff` on the HOST would then OOM the
    # controller on every subsequent job against that worktree (kimi-4).
    with tempfile.TemporaryFile() as out_fh:
        proc = subprocess.run(
            argv, check=False, stdout=out_fh, stderr=subprocess.PIPE, env=env,
            cwd=ident.realpath,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace")
            raise Refuse(f"git {' '.join(args)} failed: {err.strip()}")
        size = out_fh.tell()
        if size > MAX_GIT_OUTPUT:
            raise Refuse(
                f"git {' '.join(args)} produced {size} bytes, over the {MAX_GIT_OUTPUT} bound"
            )
        out_fh.seek(0)
        data = out_fh.read()
    return data.decode("utf-8", "replace") if text else data


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
        # Containment is not ownership. `git_dir == common` satisfies the check
        # above, so a pointer aimed at the PRIMARY's git dir would be adopted and
        # would then self-ratify forever. Require the git dir to point BACK at
        # this worktree: a genuine linked git dir carries `gitdir` (the path of
        # the worktree's own .git file) and `commondir`.
        if os.path.realpath(git_dir) == os.path.realpath(common):
            raise Refuse("linked worktree git dir must not be the common git dir")
        back = os.path.join(git_dir, "gitdir")
        if not os.path.isfile(back):
            raise Refuse(f"git dir {git_dir} has no back-pointer (not a linked worktree git dir)")
        try:
            with open(back, encoding="utf-8") as fh:
                back_target = fh.read().strip()
        except OSError as exc:
            raise Refuse(f"git dir back-pointer unreadable: {exc}") from exc
        if os.path.realpath(back_target) != os.path.realpath(git_file):
            raise Refuse(
                f"git dir {git_dir} belongs to {back_target}, not to this worktree"
            )
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
    status = _status_entries_raw(ident)
    # --no-ext-diff / --no-textconv are the only reliable way to stop a
    # repository-owned diff.external or diff.<d>.textconv from executing on the
    # host. --git-dir pinning does not help when the repository's OWN config is
    # hostile (a disposable clone), which is the case this rail must survive.
    diff = _git_pinned(ident, "diff", "--no-ext-diff", "--no-textconv", "HEAD", text=False)

    # `git status` reports an untracked file by NAME ONLY and `git diff HEAD`
    # covers tracked content only. A bounded-write worker cannot stage (the git
    # dir is read-only in the sandbox), so EVERY file it creates is untracked and
    # its content would otherwise never enter the digest -- the freeze, the
    # reviewer binding and the "worktree changed after review" check would all be
    # blind to it. Hash non-tracked content explicitly.
    h = hashlib.sha256()
    h.update(head.encode() + b"\n")
    h.update(status + b"\n")
    h.update(diff + b"\n")
    budget = [MAX_UNTRACKED_BYTES]
    for path in _nontracked_paths(ident, status):
        h.update(b"\x00NT\x00" + path.encode("utf-8", "surrogateescape") + b"\x00")
        h.update(_content_fingerprint(os.path.join(ident.realpath, path), budget))
    return h.hexdigest()


def _nontracked_paths(ident: WorktreeIdentity, status: bytes) -> list[str]:
    """Every untracked/ignored FILE, expanding collapsed directory entries.

    git reports an ignored directory as a single `loot/` entry, so hashing that
    entry alone would leave its contents invisible -- the exact hiding place a
    worker-written .gitignore creates.
    """
    out: list[str] = []
    for code, path in _parse_status(status):
        if code[0] not in {"?", "!"}:
            continue
        abs_path = os.path.join(ident.realpath, path)
        if path.endswith("/") or (os.path.isdir(abs_path) and not os.path.islink(abs_path)):
            for root, dirs, files in os.walk(abs_path):
                # os.walk puts a symlink-to-DIRECTORY in `dirs` and never
                # descends it, so collecting only `files` left those entries
                # invisible to both the digest and changed_files -- a worker
                # could repoint one freely (luna-1).
                linkdirs = [d for d in dirs if os.path.islink(os.path.join(root, d))]
                dirs[:] = sorted(d for d in dirs if d not in linkdirs)
                for name in sorted(files) + sorted(linkdirs):
                    rel = os.path.relpath(os.path.join(root, name), ident.realpath)
                    out.append(rel)
        else:
            out.append(path.rstrip("/"))
    return sorted(set(out))


# Hashing is streamed (O(1) memory); this bounds WORK, not memory.
MAX_UNTRACKED_BYTES = int(os.environ.get("AI_OPS_MAX_UNTRACKED_BYTES") or 2 * 1024 * 1024 * 1024)


def _content_fingerprint(abs_path: str, budget: list[int] | None = None) -> bytes:
    """sha256 of a non-tracked path's content, bounded.

    Refuses rather than silently skipping past the bound: a worker must not be
    able to hide content by making it large.
    """
    import hashlib

    try:
        st = os.lstat(abs_path)
    except OSError:
        return b"MISSING"
    if stat.S_ISLNK(st.st_mode):
        return b"LNK:" + os.readlink(abs_path).encode("utf-8", "surrogateescape")
    if stat.S_ISDIR(st.st_mode):
        return b"DIR"
    if not stat.S_ISREG(st.st_mode):
        return b"SPECIAL"
    if budget is not None:
        budget[0] -= st.st_size
        if budget[0] < 0:
            # Fail closed: never silently skip content a reviewer would not see.
            raise Refuse(
                "non-tracked content exceeds the digest bound "
                f"({MAX_UNTRACKED_BYTES} bytes); largest offender so far: {abs_path}. "
                "Remove it or raise AI_OPS_MAX_UNTRACKED_BYTES."
            )
    elif st.st_size > MAX_UNTRACKED_BYTES:
        raise Refuse(f"untracked file exceeds the digest bound: {abs_path}")
    h = hashlib.sha256()
    # Mode is part of the content story: making an existing dirty file
    # executable changes nothing byte-wise but changes what it IS (luna-3).
    h.update(b"MODE:%o\0" % stat.S_IMODE(st.st_mode))
    with open(abs_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def _status_entries_raw(ident: WorktreeIdentity) -> bytes:
    """Porcelain status including ALL untracked files and ignored matches.

    Without --untracked-files=all a worker hides files inside a new directory;
    without --ignored a worker hides them by writing .gitignore.
    """
    return _git_pinned(
        ident,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        text=False,
    )


def _parse_status(raw: bytes) -> list[tuple[str, str]]:
    """Parse porcelain v1 -z into (XY, path) pairs.

    Rename/copy records occupy TWO NUL-separated fields (new path, then the old
    path); the second must not be mistaken for its own entry.
    """
    fields = [f for f in raw.split(b"\x00") if f]
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code = entry[:2].decode("ascii", "replace")
        path = entry[3:].decode("utf-8", "surrogateescape")
        out.append((code, path))
        if code[0] in {"R", "C"} or code[1] in {"R", "C"}:
            i += 1  # consume the paired original path
    return out


def changed_files(ident: WorktreeIdentity) -> list[str]:
    """Paths the subject job actually touched, tracked + untracked.

    Used to check that a reviewer names the change it claims to have reviewed.
    """
    assert_gitdir_pointer_intact(ident)
    # -uall + --ignored so a reviewer is shown files a worker tried to hide in a
    # new directory or behind a worker-written .gitignore.
    raw = _status_entries_raw(ident)
    tracked = [p for code, p in _parse_status(raw) if code[0] not in {"?", "!"} and p]
    return sorted(set(tracked) | set(_nontracked_paths(ident, raw)))


def worktree_diff(ident: WorktreeIdentity, max_bytes: int = 200_000) -> str:
    """The uncommitted change, as the controller computes it.

    Handed to a reviewer in its prompt. The reviewer agent has no shell and no
    git, so it cannot obtain a diff itself -- and it should not: reviewing the
    controller's own frozen diff is what binds the review to the change that will
    actually be promoted, rather than to whatever the model managed to scrape.
    """
    assert_gitdir_pointer_intact(ident)
    out = _git_pinned(ident, "diff", "--no-ext-diff", "--no-textconv", "HEAD", text=False)
    text = out.decode("utf-8", "replace")
    if len(text) > max_bytes:
        text = text[:max_bytes] + "\n[diff truncated]\n"
    return text


def dirty_fingerprints(ident: WorktreeIdentity) -> dict[str, str]:
    """path -> content fingerprint, for every path that differs from HEAD.

    Candidate set = tracked-but-modified + untracked + ignored, i.e. exactly the
    paths `changed_files` reports. Comparing this map before and after a job
    yields that job's OWN delta, which is what a review must be bound to. The
    worktree carries earlier jobs' uncommitted work, so the post-job snapshot
    alone would credit a job with someone else's change.
    """
    import hashlib

    out: dict[str, str] = {}
    budget = [MAX_UNTRACKED_BYTES]
    for rel in changed_files(ident):
        abs_path = os.path.join(ident.realpath, rel)
        try:
            fp = _content_fingerprint(abs_path, budget)
        except Refuse:
            raise
        out[rel] = hashlib.sha256(fp).hexdigest()
    return out


def delta_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths this job actually created, modified or removed."""
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def git_identity_digest(ident: WorktreeIdentity) -> str:
    from .digest import sha256_text

    assert_gitdir_pointer_intact(ident)
    head = _git_pinned(ident, "rev-parse", "HEAD").strip()
    branch = _git_pinned(ident, "rev-parse", "--abbrev-ref", "HEAD").strip()
    remotes = _git_pinned(ident, "remote", "-v")
    wtl = _git_pinned(ident, "worktree", "list", "--porcelain")
    cfg = _git_pinned(ident, "config", "--list", "--local")
    return sha256_text("\n".join([head, branch, remotes, wtl, cfg]))
