from __future__ import annotations

import os
import resource
import signal
import subprocess
import threading
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


def _drain(fd: int, sink, cap: int, state: dict) -> None:
    """Copy a pipe to `sink`, writing at most `cap` bytes but always draining.

    Draining past the cap matters: if the reader stopped, the worker would block
    on a full pipe and the timeout would become the only way out. So keep
    reading, stop writing, and record that we truncated.

    Flush every chunk. The point of streaming is that the record is readable
    WHILE the job runs; a buffered sink would hold the tail back and there would
    be nothing to observe until exit -- which is the bug this replaces.
    """
    seen = written = 0
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            seen += len(chunk)
            if written < cap:
                part = chunk[: cap - written]
                sink.write(part)
                sink.flush()
                written += len(part)
    finally:
        state["truncated"] = seen > cap
        state["written"] = written
        try:
            sink.flush()
        except Exception:
            pass
        os.close(fd)


def run_sandboxed(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: int,
    cwd: str | None = None,
    max_output: int = 2_000_000,
    max_file_bytes: int = 256 * 1024 * 1024,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> ProcResult:
    if not argv:
        raise Refuse("empty sandbox argv")
    # First element must be trusted bwrap
    if argv[0] != "/usr/bin/bwrap":
        raise Refuse("sandbox argv must start with /usr/bin/bwrap")

    # Capture through PIPES read by controller threads that stream to disk.
    #
    # Not communicate(): that buffers everything in controller memory before any
    # size limit is consulted, and a hostile provider emits unbounded output
    # (luna-5). The threads below write incrementally and enforce the cap as they
    # go, so memory stays flat.
    #
    # Not a file handed to the child as its stdout either, which is what this
    # used to do. A regular-file fd is seekable, so a worker could lseek back and
    # rewrite or erase its own evidence -- and with the stream now landing
    # directly in the job's evidence directory rather than a controller-private
    # tempfile, that would have handed the worker its own record. Through a pipe
    # the worker can only append, and only the controller decides what is kept.
    #
    # It also makes the record LIVE: evidence/events.jsonl grows during the run,
    # which is what makes mid-run observation possible at all.
    def _open(path: str | None):
        # "w+b": the drain thread writes it and _collect reads it back, and a
        # real path means the record is on disk and growing during the run.
        return open(path, "w+b") if path else tempfile.TemporaryFile()

    out_fh = _open(stdout_path)
    err_fh = _open(stderr_path)
    out_state: dict = {"truncated": False, "written": 0}
    err_state: dict = {"truncated": False, "written": 0}
    try:
        def _apply_limits() -> None:
            # RLIMIT_FSIZE is inherited by every process in the sandbox and is
            # enforced by the kernel, so it bounds any single file the worker
            # writes into its worktree. Polling for size cannot do this: a worker
            # writes 500MB in 0.1s, far inside any poll interval. The stdout
            # spool is bounded by the reader threads instead, since a pipe is not
            # a file and RLIMIT_FSIZE does not apply to it.
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))

        try:
            proc = subprocess.Popen(
                list(argv),
                # No stdin. A provider that reads stdin (codex exec announces
                # "Reading additional input from stdin...") would otherwise
                # inherit the CONTROLLER's stdin and block until the job timeout
                # -- measured: a live Codex job hung for the full 240s and
                # produced zero events. Nothing in the rail feeds a provider on
                # stdin; the prompt is argv.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
                preexec_fn=_apply_limits,
            )
        except OSError as exc:
            raise Refuse(f"failed to start sandbox: {exc}") from exc

        # Take the fds from Popen so the threads own them exclusively.
        out_fd = os.dup(proc.stdout.fileno()); proc.stdout.close()
        err_fd = os.dup(proc.stderr.fileno()); proc.stderr.close()
        t_out = threading.Thread(
            target=_drain, args=(out_fd, out_fh, max_output, out_state), daemon=True
        )
        t_err = threading.Thread(
            target=_drain, args=(err_fd, err_fh, 65536, err_state), daemon=True
        )
        t_out.start(); t_err.start()

        timed_out = _wait_or_kill(proc, timeout_s)
        # The child is dead, so the write ends of both pipes are closed and the
        # readers see EOF. Bound the join anyway: a grandchild that escaped the
        # process group could hold the pipe open, and hanging here would be worse
        # than an incomplete tail.
        t_out.join(timeout=10); t_err.join(timeout=10)
        return _collect(proc, out_fh, err_fh, timed_out, out_state)
    finally:
        for fh in (out_fh, err_fh):
            try:
                fh.close()
            except Exception:
                pass


def _read_back(fh) -> bytes:
    """Re-read what the drain thread wrote. The cap was applied on the way in."""
    fh.flush()
    fh.seek(0)
    return fh.read()


def _collect(proc, out_fh, err_fh, timed_out: bool, out_state: dict) -> ProcResult:
    if proc.poll() is None:
        proc.wait(timeout=5)
    if proc.poll() is None:
        raise ProviderError("sandbox process still alive after kill")
    out = _read_back(out_fh)
    err = _read_back(err_fh)[:65536]
    return ProcResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out,
        stderr=err,
        timed_out=timed_out,
        pid=proc.pid,
        truncated=bool(out_state.get("truncated")),
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
