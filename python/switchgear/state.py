from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import Refuse
from .paths import mkdir_exclusive, open_nofollow, reject_symlinks, require_absolute, require_job_id
from .schema import validate

MARKER = ".switchgear-state"


class StateRoot:
    def __init__(self, path: str) -> None:
        require_absolute(path, "state")
        self.path = reject_symlinks(os.path.abspath(path), "state")
        marker = os.path.join(self.path, MARKER)
        if not os.path.isdir(self.path):
            raise Refuse(
                f"state root does not exist: {self.path}. Create it with "
                f"`switchgear state provision {self.path}`. A state root holds "
                "job records and evidence; put it outside every worktree it "
                "records jobs for."
            )
        if os.path.islink(self.path):
            raise Refuse("state root is a symlink")
        if not os.path.isfile(marker) or os.path.islink(marker):
            raise Refuse(f"state root missing marker {MARKER}")
        jobs = os.path.join(self.path, "jobs")
        leases = os.path.join(self.path, "leases")
        if os.path.islink(jobs) or os.path.islink(leases):
            raise Refuse("state jobs/leases must not be symlinks")
        os.makedirs(jobs, exist_ok=True)
        os.makedirs(leases, exist_ok=True)
        reject_symlinks(jobs, "jobs")
        reject_symlinks(leases, "leases")

    @property
    def jobs(self) -> str:
        return os.path.join(self.path, "jobs")

    @property
    def leases(self) -> str:
        return os.path.join(self.path, "leases")

    def job_dir(self, job_id: str) -> str:
        require_job_id(job_id)
        path = os.path.join(self.jobs, job_id)
        # Every component, not just the top-level jobs/ dir: a symlinked
        # jobs/<uuid> would otherwise redirect result.json reads (and therefore
        # `promote --review`) outside the state root.
        if os.path.lexists(path):
            reject_symlinks(path, "job dir")
        return path


def provision(path: str) -> str:
    require_absolute(path, "state")
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    reject_symlinks(parent, "state parent")
    if not os.path.isdir(abs_path):
        os.mkdir(abs_path, 0o700)
    reject_symlinks(abs_path, "state")
    marker = os.path.join(abs_path, MARKER)
    if not os.path.isfile(marker):
        fd = open_nofollow(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"switchgear-state-v1\n")
        finally:
            os.close(fd)
    os.makedirs(os.path.join(abs_path, "jobs"), exist_ok=True)
    os.makedirs(os.path.join(abs_path, "leases"), exist_ok=True)
    return abs_path


def new_job_id() -> str:
    return str(uuid.uuid4())


def atomic_write_json(path: str, obj: Any) -> None:
    """Write JSON so a reader never sees a half-written file.

    The temp name is UNIQUE PER WRITER. It used to be a fixed `<path>.tmp` that
    was unlinked first if present, which made this atomic only for one writer at
    a time and actively destructive for two: writer B would unlink A's in-flight
    temp, A would go on writing to an unlinked inode, and whichever reached
    `os.replace` second either clobbered the other's file or died with ENOENT
    because its own temp had been renamed away.

    That is not theoretical. A soak run at 40 concurrent jobs killed one outright
    on exactly this path: two jobs sharing a worktree both wrote its session
    marker, and one died with
    `FileNotFoundError: ... worktree.json.tmp -> worktree.json`. Only concurrency
    surfaces it, which is why the soak test exists.

    `os.replace` is atomic, so concurrent writers now resolve to last-writer-wins
    on the rename -- the correct semantics -- instead of corrupting each other.
    """
    import uuid

    directory = os.path.dirname(path)
    reject_symlinks(directory, "write parent")
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = open_nofollow(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        # Never leave the temp behind for a later reader or a gc sweep to puzzle
        # over.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str) -> Any:
    # Check the whole path, not only the final component: a symlinked parent
    # directory redirects the read just as effectively.
    reject_symlinks(path, "read path")
    fd = open_nofollow(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, encoding="utf-8", closefd=False) as fh:
            return json.load(fh)
    finally:
        os.close(fd)


def create_job_dirs(root: StateRoot, job_id: str) -> dict[str, str]:
    require_job_id(job_id)
    jd = root.job_dir(job_id)
    mkdir_exclusive(jd)
    evidence = os.path.join(jd, "evidence")
    home = os.path.join(jd, "sandbox-home")
    os.mkdir(evidence, 0o700)
    os.mkdir(home, 0o700)
    os.makedirs(os.path.join(home, ".config", "opencode"), exist_ok=True)
    os.makedirs(os.path.join(home, ".cache"), exist_ok=True)
    os.makedirs(os.path.join(home, "tmp"), exist_ok=True)
    # An explicit start marker. `status` must be able to report elapsed time for a
    # RUNNING job, and result.json (which carries `started`) does not exist until
    # the job is over. Directory mtimes are not a substitute: they move as files
    # land in the job dir, which produced negative elapsed times.
    with open(os.path.join(jd, "started_at"), "w", encoding="utf-8") as fh:
        fh.write(f"{time.time():.3f}\n")
    return {"job": jd, "evidence": evidence, "home": home}
