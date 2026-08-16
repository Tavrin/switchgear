from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from .errors import ProviderError, Refuse


@dataclass
class ProcResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    pid: int


def run_sandboxed(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: int,
    cwd: str | None = None,
) -> ProcResult:
    if not argv:
        raise Refuse("empty sandbox argv")
    # First element must be trusted bwrap
    if argv[0] != "/usr/bin/bwrap":
        raise Refuse("sandbox argv must start with /usr/bin/bwrap")
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise Refuse(f"failed to start sandbox: {exc}") from exc
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            out, err = proc.communicate(timeout=5)
    # Wait until the process is reaped.
    if proc.poll() is None:
        proc.wait(timeout=5)
    if proc.poll() is None:
        raise ProviderError("sandbox process still alive after kill")
    return ProcResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out or b"",
        stderr=err or b"",
        timed_out=timed_out,
        pid=proc.pid,
    )
