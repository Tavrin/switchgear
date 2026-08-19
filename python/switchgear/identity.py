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
    try:
        head = _git(abs_path, "rev-parse", "HEAD").strip()
    except Refuse as exc:
        # A repository with no commits is the common case here, and git's own
        # message for it ("ambiguous argument 'HEAD'") reads like a usage error
        # in the rail rather than a description of the worktree.
        if "ambiguous argument" in str(exc) or "unknown revision" in str(exc):
            raise Refuse(
                f"{abs_path} is a git repository with no commits yet, so it has "
                "no HEAD to pin the job against. Make an initial commit "
                "(`git commit --allow-empty -m init`) and run this again."
            ) from exc
        raise
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
MAX_UNTRACKED_BYTES = int(os.environ.get("SWITCHGEAR_MAX_UNTRACKED_BYTES") or 2 * 1024 * 1024 * 1024)


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
                "Remove it or raise SWITCHGEAR_MAX_UNTRACKED_BYTES."
            )
    elif st.st_size > MAX_UNTRACKED_BYTES:
        raise Refuse(f"untracked file exceeds the digest bound: {abs_path}")
    h = hashlib.sha256()
    # Mode is part of the content story: making an existing dirty file
    # executable changes nothing byte-wise but changes what it IS (luna-3).
    h.update(b"MODE:%o\0" % stat.S_IMODE(st.st_mode))
    try:
        with open(abs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        # A file the CONTROLLER cannot read (root-owned data dirs, mode 000 --
        # measured on a large private repository's search-index store) must not crash the whole
        # audit and make the platform unauditable. It also must not vanish
        # silently. Fingerprint it from the metadata we CAN see (size, mode,
        # mtime) under an UNREADABLE marker: the digest still moves if the file
        # changes size or mode, and the anti-hiding property is intact because a
        # same-uid worker cannot create a file the same-uid controller cannot
        # read -- such files are pre-existing and ambient, never worker output.
        h.update(
            b"UNREADABLE:%d:%d:%d:%s"
            % (st.st_size, stat.S_IMODE(st.st_mode), int(st.st_mtime), type(exc).__name__.encode())
        )
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


# The diff is delivered as a FILE the reviewer reads (and pages through), not as
# an argv element, so the kernel's 128KiB MAX_ARG_STRLEN no longer bounds it.
# The old 200KB cap was sized for the argv path and, being ABOVE that kernel
# limit, managed to both crash large reviews AND truncate them. A live reviewer
# on a 229KB diff reported the truncation as its principal finding, which is the
# correct behaviour and also a waste of a review.
MAX_DIFF_BYTES = int(os.environ.get("SWITCHGEAR_MAX_DIFF_BYTES") or 5_000_000)


def review_manifest(ident: WorktreeIdentity) -> tuple[list[str], list[str]]:
    """Files split for REVIEW, which is a different consumer from the DIGEST.

    Returns (changed, ignored). `changed` is tracked-modified plus new
    NON-ignored untracked files -- the actual change a reviewer should read.
    `ignored` is the ignored matches: ambient .venv/.idea/__pycache__/caches,
    which are environment, not the change under review.

    The integrity digest (tree_digest) still covers BOTH, unchanged -- the
    anti-hiding property lives there and at the promote gate. This split only
    shapes what the reviewer is ASKED to read. Without it, a repo with a
    populated .venv put 42,205 filenames (3.7MB) in the review attachment and a
    reviewer burned its whole timeout paging through ambient noise (measured on
    a large private repository, a mid-tier model, $1.05). Review is a quality signal, not the boundary,
    so trimming what it reads costs no security.
    """
    raw = _status_entries_raw(ident)
    entries = _parse_status(raw)
    tracked = [p for code, p in entries if code[0] not in {"?", "!"} and p]
    # Ignored entries as git reports them: exact files, or directory prefixes.
    ignored_files = {p for code, p in entries if code[0] == "!" and not p.endswith("/")}
    ignored_prefixes = tuple(p for code, p in entries if code[0] == "!" and p.endswith("/"))

    def _is_ignored(path: str) -> bool:
        return path in ignored_files or path.startswith(ignored_prefixes)

    ignored: list[str] = []
    new: list[str] = []
    for path in _nontracked_paths(ident, raw):
        (ignored if _is_ignored(path) else new).append(path)
    return sorted(set(tracked) | set(new)), sorted(ignored)


def worktree_diff(ident: WorktreeIdentity, max_bytes: int | None = None) -> str:
    """The uncommitted change, as the controller computes it.

    Attached to a review job as a file. The reviewer agent has no shell and no
    git, so it cannot obtain a diff itself -- and it should not: reviewing the
    controller's own frozen diff is what binds the review to the change that will
    actually be promoted, rather than to whatever the model managed to scrape.

    Still capped, because a pathological repo should not be able to fill the
    state store; but the cap is now a backstop rather than a routine event, and
    when it fires the reviewer is told so explicitly.
    """
    max_bytes = MAX_DIFF_BYTES if max_bytes is None else max_bytes
    assert_gitdir_pointer_intact(ident)
    out = _git_pinned(ident, "diff", "--no-ext-diff", "--no-textconv", "HEAD", text=False)
    text = out.decode("utf-8", "replace")
    if len(text) > max_bytes:
        text = text[:max_bytes] + "\n[diff truncated]\n"
    return text


def untracked_diff(
    ident: WorktreeIdentity, paths: list[str], max_bytes: int | None = None
) -> str:
    """Added-file diffs for NEW files, which `git diff HEAD` cannot show.

    `git diff HEAD` covers tracked content only, so a file the worker CREATED
    appears in the changed-file list with no content behind it. A live reviewer
    caught this on a real repo: it reported that it could not verify a new
    security-sensitive config file because no diff hunk was provided. For a
    bounded-write job -- where a worker cannot stage, so everything it creates is
    untracked -- that means reviewing the most important changes blind.

    Rendered with `git diff --no-index /dev/null <path>`, which produces a normal
    added-file hunk and handles binary detection itself.
    """
    max_bytes = MAX_DIFF_BYTES if max_bytes is None else max_bytes
    out: list[str] = []
    used = 0
    for rel in paths:
        abs_path = os.path.join(ident.realpath, rel)
        if not os.path.isfile(abs_path) or os.path.islink(abs_path):
            continue
        try:
            # Exit status 1 just means "there are differences", which is the
            # whole point; only a real failure should be swallowed.
            raw = _git_pinned(
                ident, "diff", "--no-ext-diff", "--no-textconv", "--no-index",
                "--", "/dev/null", abs_path, text=False,
            )
        except Refuse:
            proc = subprocess.run(
                [GIT, "--no-pager", "diff", "--no-ext-diff", "--no-textconv",
                 "--no-index", "--", "/dev/null", abs_path],
                check=False, capture_output=True, env=git_clean_env(), cwd=ident.realpath,
            )
            raw = proc.stdout
        text = raw.decode("utf-8", "replace")
        if used + len(text) > max_bytes:
            out.append(f"\n[new-file diffs truncated at {max_bytes} bytes]\n")
            break
        out.append(text)
        used += len(text)
    return "".join(out)


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
