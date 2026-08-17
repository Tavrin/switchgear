from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .errors import Refuse
from .identity import WorktreeIdentity
from .paths import open_nofollow, reject_symlinks, require_safe_id
from .schema import validate
from .state import StateRoot, atomic_write_json, read_json


def _boot_id() -> str:
    path = "/proc/sys/kernel/random/boot_id"
    if os.path.isfile(path):
        return open(path, encoding="utf-8").read().strip()
    return "unknown-boot"


def _starttime(pid: int) -> str:
    with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
        st = fh.read()
    rest = st[st.rfind(")") + 2 :].split()
    return rest[19]  # starttime is field 22; after comm: index 19


def _alive(pid: int, start: str, boot: str) -> bool:
    if boot != _boot_id():
        return False
    if not os.path.isdir(f"/proc/{pid}"):
        return False
    try:
        return _starttime(pid) == start
    except OSError:
        return False


def identity_key(ident: WorktreeIdentity) -> str:
    raw = f"{ident.st_dev}:{ident.st_ino}:{ident.realpath}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class LeaseToken:
    lease_uuid: str
    owner: str
    owner_pid: int
    owner_starttime: str
    boot_id: str
    realpath: str
    st_dev: int
    st_ino: int
    mode: str
    job_id: Optional[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lease_uuid": self.lease_uuid,
            "owner": self.owner,
            "owner_pid": self.owner_pid,
            "owner_starttime": self.owner_starttime,
            "boot_id": self.boot_id,
            "realpath": self.realpath,
            "st_dev": self.st_dev,
            "st_ino": self.st_ino,
            "mode": self.mode,
            "job_id": self.job_id,
            "acquired_at": self.acquired_at if hasattr(self, "acquired_at") else "",
        }


def _dir(root: StateRoot, ident: WorktreeIdentity) -> str:
    d = os.path.join(root.leases, identity_key(ident))
    return d


def acquire(
    root: StateRoot,
    ident: WorktreeIdentity,
    owner: str,
    owner_pid: int,
    mode: str,
) -> dict[str, Any]:
    require_safe_id(owner, "owner")
    d = _dir(root, ident)
    os.makedirs(d, exist_ok=True)
    reject_symlinks(d, "lease dir")
    lock_path = os.path.join(d, "lock")
    token_path = os.path.join(d, "token.json")
    fd = open_nofollow(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise Refuse("live lease lock already held on this worktree") from exc
        # we hold flock only for metadata mutation here; worker will re-lock
        # Holding LOCK_EX here means no worker is mid-job on this worktree, so an
        # existing token is stale by definition and is replaced. (A previous
        # version computed owner liveness here and then discarded the result;
        # dead code that looks like an exclusivity check is worse than none.)
        start = _starttime(owner_pid)
        token = {
            "lease_uuid": str(uuid.uuid4()),
            "owner": owner,
            "owner_pid": int(owner_pid),
            "owner_starttime": start,
            "boot_id": _boot_id(),
            "acquired_at": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            "realpath": ident.realpath,
            "st_dev": ident.st_dev,
            "st_ino": ident.st_ino,
            "mode": mode,
            "job_id": None,
        }
        validate(token, "lease.schema.json")
        atomic_write_json(token_path, token)
        return token
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def release(root: StateRoot, ident: WorktreeIdentity, token_uuid: str, owner: str) -> None:
    d = _dir(root, ident)
    token_path = os.path.join(d, "token.json")
    lock_path = os.path.join(d, "lock")
    if not os.path.isfile(token_path):
        raise Refuse("no lease to release")
    fd = open_nofollow(lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise Refuse("cannot release: worker still holds the lease") from exc
        existing = read_json(token_path)
        if existing.get("lease_uuid") != token_uuid:
            raise Refuse("forged or mismatched lease token")
        if existing.get("owner") != owner:
            raise Refuse("unauthorized release (owner mismatch)")
        os.unlink(token_path)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def load_token(root: StateRoot, ident: WorktreeIdentity) -> dict[str, Any]:
    path = os.path.join(_dir(root, ident), "token.json")
    if not os.path.isfile(path):
        raise Refuse(f"no lease for {ident.realpath}")
    token = read_json(path)
    validate(token, "lease.schema.json")
    return token


class WorktreeLock:
    """Exclusive worktree flock with no lease-token requirement.

    Used for bounded-write when the profile opts out of leases: the token is an
    authorization concern, mutual exclusion is a correctness one, and dropping
    the latter with the former left writers racing promotion.
    """

    def __init__(self, root: StateRoot, ident: WorktreeIdentity) -> None:
        self.root = root
        self.ident = ident
        self.fd: Optional[int] = None

    def __enter__(self) -> dict[str, Any]:
        d = _dir(self.root, self.ident)
        os.makedirs(d, exist_ok=True)
        reject_symlinks(d, "lease dir")
        self.fd = open_nofollow(os.path.join(d, "lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise Refuse("another worker holds this worktree") from exc
        return {}

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


class PromotionLock:
    """Exclusive worktree lock held across the promotion decision AND write.

    Promotion samples the live tree and then writes `ok`. Without this lock a
    bounded-write worker can change the tree in between, so the subject is
    promoted against a tree that no longer matches the reviewed freeze.
    """

    def __init__(self, root: StateRoot, ident: WorktreeIdentity) -> None:
        self.root = root
        self.ident = ident
        self.fd: Optional[int] = None

    def __enter__(self) -> None:
        d = _dir(self.root, self.ident)
        os.makedirs(d, exist_ok=True)
        reject_symlinks(d, "lease dir")
        lock_path = os.path.join(d, "lock")
        # Always create and hold the lock. Skipping it when the file is absent
        # skipped serialization in exactly the configuration where workers run
        # lock-free (require_lease_for_write=false) -- the race this is for.
        self.fd = open_nofollow(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise Refuse("cannot promote while a worker holds this worktree") from exc
        return None

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


class WorkerLock:
    """Exclusive flock held for the entire worker lifetime."""

    def __init__(
        self,
        root: StateRoot,
        ident: WorktreeIdentity,
        token_uuid: str,
        job_id: str,
        mode: str,
    ) -> None:
        self.root = root
        self.ident = ident
        self.token_uuid = token_uuid
        self.job_id = job_id
        self.mode = mode
        self.fd: Optional[int] = None

    def __enter__(self) -> dict[str, Any]:
        d = _dir(self.root, self.ident)
        lock_path = os.path.join(d, "lock")
        self.fd = open_nofollow(lock_path, os.O_RDWR)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise Refuse("another worker holds this worktree lease") from exc
        # Validate only AFTER holding the lock. Reading the token first and
        # writing it back afterwards let a concurrent re-acquire be clobbered by
        # a stale token, resurrecting a revoked lease (luna-4).
        token = load_token(self.root, self.ident)
        if token["lease_uuid"] != self.token_uuid:
            raise Refuse("forged lease token")
        if token["realpath"] != self.ident.realpath:
            raise Refuse("lease worktree mismatch")
        if token["st_dev"] != self.ident.st_dev or token["st_ino"] != self.ident.st_ino:
            raise Refuse("lease worktree identity mismatch")
        if token.get("mode") and token["mode"] != self.mode:
            raise Refuse("lease mode mismatch")
        token["job_id"] = self.job_id
        atomic_write_json(os.path.join(d, "token.json"), token)
        return token

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
