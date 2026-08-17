from __future__ import annotations

import os
import signal
import subprocess
import tempfile
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
    truncated: bool = False


def run_sandboxed(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: int,
    cwd: str | None = None,
    max_output: int = 2_000_000,
) -> ProcResult:
    if not argv:
        raise Refuse("empty sandbox argv")
    # First element must be trusted bwrap
    if argv[0] != "/usr/bin/bwrap":
        raise Refuse("sandbox argv must start with /usr/bin/bwrap")
    # Capture through temp FILES, not pipes held in controller memory: a hostile
    # provider can emit unbounded output, and communicate() buffers all of it
    # before any size limit is consulted.
    with tempfile.TemporaryFile() as out_fh, tempfile.TemporaryFile() as err_fh:
        try:
            proc = subprocess.Popen(
                list(argv),
                stdout=out_fh,
                stderr=err_fh,
                env=dict(env),
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise Refuse(f"failed to start sandbox: {exc}") from exc
        timed_out = _wait_or_kill(proc, timeout_s)
        return _collect(proc, out_fh, err_fh, timed_out, max_output)


def _read_capped(fh, cap: int) -> tuple[bytes, bool]:
    fh.seek(0)
    data = fh.read(cap + 1)
    if len(data) > cap:
        return data[:cap], True
    return data, False


def _collect(proc, out_fh, err_fh, timed_out: bool, cap: int) -> ProcResult:
    if proc.poll() is None:
        proc.wait(timeout=5)
    if proc.poll() is None:
        raise ProviderError("sandbox process still alive after kill")
    out, out_trunc = _read_capped(out_fh, cap)
    err, _ = _read_capped(err_fh, 65536)
    return ProcResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out,
        stderr=err,
        timed_out=timed_out,
        pid=proc.pid,
        truncated=out_trunc,
    )


def _wait_or_kill(proc, timeout_s: int) -> bool:
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait(timeout=5)
    return timed_out
