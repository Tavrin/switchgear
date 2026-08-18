"""A second containment layer: run the worker as a uid that is not yours.

Until now isolation was mount VISIBILITY only. The worker ran as the invoking
user, so anything that became reachable — a bind that should not have been there,
a symlink trick, an inherited fd, a bwrap bug — was also writable, because the
process *was* you. One layer, and one mistake away from your account.

With this, the kernel's permission check is a second, independent layer: even if
a path becomes reachable, the worker's uid does not own it and the write is
refused.

**The obvious version of this does not work, and would be worse than nothing.**
Measured here first: `bwrap --unshare-user --uid 65534` gives a namespace whose
map has exactly one entry, so the invoking uid maps to the sandbox's own uid.
Your files then appear to be owned by the worker and remain fully writable. It
looks like a boundary in `id -u` and is not one. Confirmed by writing to a
mode-600 file owned by the host user from inside such a sandbox: it succeeded.

What does work, and what this module sets up:

  1. bwrap creates the namespace and BLOCKS on `--userns-block-fd`.
  2. The controller writes a TWO-range map with the setuid helpers
     `newuidmap`/`newgidmap`: inside-0 -> your uid (one id, so bwrap can still
     operate), inside-1.. -> your allocated subuid range.
  3. The controller unblocks bwrap, which execs `setpriv --reuid 1 --regid 1`,
     dropping the payload to inside-uid 1 — which maps to a subuid, NOT to you.
  4. `--cap-add CAP_SETUID/CAP_SETGID` is required for step 3 and for nothing
     else: bwrap drops all capabilities before exec, so without them setpriv
     fails with EPERM. They are dropped again by setpriv itself.

Verified end to end before any of this was written: a mode-600 file owned by the
host user, bind-mounted read-write into such a sandbox, showed as owned by uid 0
and the write was DENIED. The same test without the map succeeded.

**Readonly jobs only, deliberately.** A bounded-write worker has to produce files
the controller then reads and commits, and files written by a subuid are owned by
that subuid — the controller cannot chown them back without privileges it does
not have. Claiming the boundary for a lane where it forces an ownership problem
would be trading a real property for a broken one. Scout and review are readonly,
which is most jobs and includes the review gate itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

#: Inside-namespace uid the payload is dropped to. 1 rather than 0, because
#: inside-0 is mapped to the invoking user and is therefore exactly the identity
#: this is meant to escape.
PAYLOAD_UID = 1
PAYLOAD_GID = 1

_ENV_DISABLE = "AI_OPS_NO_UID_BOUNDARY"


def _subid_range(path: str, name: str, numeric: int) -> tuple[int, int] | None:
    """The user's allocated (start, count) from /etc/subuid or /etc/subgid."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split(":")
                if len(parts) != 3:
                    continue
                who, start, count = parts
                if who == name or who == str(numeric):
                    return int(start), int(count)
    except (OSError, ValueError):
        return None
    return None


def capability() -> dict[str, Any]:
    """Whether a real uid boundary can be established here, and why not if not.

    Every negative answer names the missing piece. A silent "unavailable" would
    be indistinguishable from "not attempted", and the whole point of this module
    is that a boundary you cannot verify is not one.
    """
    import getpass

    if os.environ.get(_ENV_DISABLE) == "1":
        return {"available": False, "reason": f"disabled by {_ENV_DISABLE}=1"}

    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    uid, gid = os.getuid(), os.getgid()

    if uid == 0:
        return {"available": False,
                "reason": "running as root; this boundary is for unprivileged use"}

    for tool in ("newuidmap", "newgidmap", "setpriv"):
        if not shutil.which(tool):
            return {"available": False,
                    "reason": f"{tool} not installed (uidmap / util-linux)"}

    subuid = _subid_range("/etc/subuid", user, uid)
    subgid = _subid_range("/etc/subgid", user, gid)
    if not subuid:
        return {"available": False,
                "reason": f"no /etc/subuid range allocated for {user!r}"}
    if not subgid:
        return {"available": False,
                "reason": f"no /etc/subgid range allocated for {user!r}"}
    if subuid[1] < 2 or subgid[1] < 2:
        return {"available": False,
                "reason": "the allocated subid range is too small to map a payload id"}

    try:
        if int(open("/proc/sys/user/max_user_namespaces").read().strip()) <= 0:
            return {"available": False,
                    "reason": "user namespaces are disabled (max_user_namespaces=0)"}
    except (OSError, ValueError):
        pass

    return {
        "available": True,
        "subuid": {"start": subuid[0], "count": subuid[1]},
        "subgid": {"start": subgid[0], "count": subgid[1]},
        "payload_uid": PAYLOAD_UID,
    }


def bwrap_flags(block_fd: int, info_fd: int) -> list[str]:
    """Flags that put bwrap in blocked-namespace mode."""
    return [
        "--unshare-user",
        "--userns-block-fd", str(block_fd),
        "--info-fd", str(info_fd),
        # Needed ONLY so setpriv can drop the payload to an unmapped id; bwrap
        # otherwise clears all capabilities before exec and setpriv fails EPERM.
        # setpriv drops them again immediately.
        "--cap-add", "CAP_SETUID",
        "--cap-add", "CAP_SETGID",
    ]


def payload_prefix() -> list[str]:
    """Wrapper that drops the worker to the unmapped payload id."""
    return [
        shutil.which("setpriv") or "/usr/bin/setpriv",
        "--reuid", str(PAYLOAD_UID),
        "--regid", str(PAYLOAD_GID),
        "--clear-groups",
    ]


def apply_map(child_pid: int, cap: dict[str, Any]) -> None:
    """Write the two-range map for a blocked namespace.

    inside-0 -> the invoking uid (exactly one id, so bwrap can still function),
    inside-1.. -> the allocated subid range. The payload runs at inside-1, which
    therefore maps to a subuid and NOT to the invoking user.

    Raises on failure: a namespace whose map was not written is one where the
    payload would run as the invoking user, and proceeding would mean claiming a
    boundary that is not there.
    """
    from .errors import Refuse

    uid, gid = os.getuid(), os.getgid()
    sub_u, sub_g = cap["subuid"], cap["subgid"]
    plans = (
        ("newuidmap", [str(child_pid), "0", str(uid), "1",
                       "1", str(sub_u["start"]), str(sub_u["count"])]),
        ("newgidmap", [str(child_pid), "0", str(gid), "1",
                       "1", str(sub_g["start"]), str(sub_g["count"])]),
    )
    for tool, args in plans:
        proc = subprocess.run([shutil.which(tool) or f"/usr/bin/{tool}", *args],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise Refuse(
                f"{tool} failed while establishing the uid boundary: "
                f"{(proc.stderr or '').strip()[:200]}. Refusing to run the job "
                "rather than run it as your own user while reporting otherwise."
            )


def read_child_pid(info_fd_read: int) -> int:
    """bwrap's json info blob carries the namespace's child pid."""
    from .errors import ProviderError

    buf = b""
    while True:
        chunk = os.read(info_fd_read, 4096)
        if not chunk:
            break
        buf += chunk
        try:
            return int(json.loads(buf.decode("utf-8"))["child-pid"])
        except Exception:
            continue
    raise ProviderError("bwrap did not report a child pid for the user namespace")


def grant_payload_access(paths: list[str]) -> None:
    """Let the payload id use the directories the controller made for it.

    The synthetic HOME is created 0700 and owned by the invoking user, so under
    the boundary the worker cannot read or write the very directory that exists
    for it. Measured: the job ran, and the provider silently fell back to
    defaults because it could not read its own config.

    Widened to 0777 rather than ACL-matched to the subuid, because the enclosing
    job directory is 0700 and owned by the invoking user -- no other local user
    can traverse into these at all, so the mode on the inner directory grants
    access to the payload id and to nobody else. Inside the sandbox the bind
    destination's parents are bwrap-created, so the host parent's mode does not
    block the worker.

    Only ever applied to per-job scratch and to session stores, never to
    evidence: the job directory and everything the controller must be able to
    trust stays 0700.
    """
    for root in paths:
        if not os.path.isdir(root):
            continue
        try:
            os.chmod(root, 0o777)
        except OSError:
            continue
        for base, dirs, files in os.walk(root):
            for name in dirs:
                try:
                    os.chmod(os.path.join(base, name), 0o777)
                except OSError:
                    pass
            for name in files:
                try:
                    os.chmod(os.path.join(base, name), 0o666)
                except OSError:
                    pass


def payload_env() -> dict[str, str]:
    """Environment the payload needs once it is no longer the repo's owner.

    git refuses a repository owned by a different user -- "detected dubious
    ownership" -- which under this boundary is every repository, since the
    worktree belongs to the invoking user and the worker does not. Measured:
    `git status` exits 128 and a reviewer that inspects with git reports no files
    at all.

    That check exists to stop you being tricked into running hooks from someone
    else's repo on a shared machine. Inside this sandbox it protects nothing the
    boundary does not already cover: the worktree is a read-only mount for a
    readonly job, the git dir is read-only, and the payload cannot write either.
    So it is turned off HERE, in the sandbox env only, and never in any config
    the host reads.

    Set via GIT_CONFIG_COUNT rather than a config file, so it cannot outlive the
    process or be picked up by anything outside it.
    """
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }
