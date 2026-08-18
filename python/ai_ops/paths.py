from __future__ import annotations

import os
import re
import stat
from typing import Optional

from .errors import Refuse

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SAFE_JOB = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def require_safe_id(value: str, name: str = "id") -> str:
    if not SAFE_ID.match(value or ""):
        raise Refuse(
            f"unsafe {name}: {value!r} — use only letters, digits, dot, dash and "
            "underscore, at most 128 characters"
        )
    if ".." in value:
        raise Refuse(f"traversal in {name}: {value!r} contains '..'; use a plain name")
    return value


def require_job_id(value: str) -> str:
    if not SAFE_JOB.match(value or ""):
        raise Refuse(
            f"unsafe job id: {value!r} — a job id is the uuid printed by the "
            "command that created it (`ai-opencode jobs` lists them)"
        )
    return value


def _symlink_in_path(abs_path: str) -> Optional[str]:
    """Return the first symlink component, or None."""
    parts = []
    cur = os.path.abspath(abs_path)
    # walk from root
    segs = []
    while True:
        parent, name = os.path.split(cur)
        if name:
            segs.append(name)
        if parent == cur:
            break
        cur = parent
    segs.reverse()
    built = os.sep
    for name in segs:
        built = os.path.join(built, name) if built != os.sep else os.path.join(os.sep, name)
        # Walk every component. Stopping at the first missing one would report
        # "clean" for a path whose later components are symlinks (finding N8).
        if os.path.islink(built):
            return built
    return None


def reject_symlinks(abs_path: str, label: str) -> str:
    path = os.path.abspath(abs_path)
    hit = _symlink_in_path(path)
    if hit:
        raise Refuse(
            f"{label} contains symlink component: {hit} — pass the resolved path "
            "instead. A symlink is refused because it can be repointed underneath "
            "the rail after the check and before the use."
        )
    return path


def require_absolute(path: str, label: str) -> str:
    if not path or not os.path.isabs(path):
        raise Refuse(f"{label} must be an absolute path (got {path!r})")
    return path


def open_nofollow(path: str, flags: int, mode: int = 0o600) -> int:
    flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags, mode)
    except OSError as exc:
        raise Refuse(f"open nofollow failed for {path}: {exc}") from exc


def mkdir_exclusive(path: str, mode: int = 0o700) -> None:
    parent = os.path.dirname(path)
    reject_symlinks(parent, "parent")
    if os.path.islink(path) or os.path.lexists(path):
        raise Refuse(
            f"refusing to create over existing path: {path} — remove it first if "
            "it is stale; the rail never overwrites what it did not create"
        )
    os.mkdir(path, mode)


def is_within(inner: str, outer: str) -> bool:
    """True if `inner` is `outer` or lives underneath it, after canonicalization."""
    a = os.path.realpath(inner)
    b = os.path.realpath(outer)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def require_disjoint(a: str, b: str, label_a: str, label_b: str) -> None:
    """Refuse if either path contains the other.

    The synthetic HOME is bind-mounted writable and lives under the state root.
    If the state root sits inside the target worktree, that writable bind nests
    inside a --ro-bind and punches a real hole in readonly containment.
    """
    if is_within(a, b) or is_within(b, a):
        raise Refuse(
            f"{label_a} and {label_b} must not overlap ({a} vs {b}). Put the state "
            "root somewhere outside every worktree it records jobs for — "
            "/var/tmp/ai-ops-state or ~/.local/state/ai-ops are reasonable "
            "choices. The synthetic HOME is a writable bind under the state root, "
            "so nesting it inside a worktree punches a hole straight through "
            "readonly containment."
        )


def stat_identity(path: str) -> tuple[int, int]:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise Refuse(
            f"path is a symlink: {path} — pass the path it resolves to. Identity "
            "here is (device, inode), and a symlink has its own, so accepting one "
            "would let the target be swapped after the check."
        )
    return st.st_dev, st.st_ino
