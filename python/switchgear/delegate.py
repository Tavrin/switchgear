"""Let a worker already inside the sandbox obtain a subagent. Default OFF.

An agent with ordinary host access can already call this CLI recursively, and
`docs/INTEGRATION.md` treats that as first-class. A worker running *inside* a
sandbox cannot: it has no CLI, no state root and no credential, which is the
point of the boundary.

The wrong fix is to mount them. Handing the worker the launcher, the state store
and the credential files would dismantle exactly what the sandbox is for, in
order to add a feature.

The right shape is the one the credential broker already uses: the capability
lives controller-side and the sandbox gets a socket. The worker can ask for a
subagent; it cannot construct one.

## The constraint that shapes everything here

**The client is the untrusted worker.** Every byte it sends is hostile input --
it may itself be a steered agent, and prompt injection reaching it is an
explicitly unsolved problem (ROADMAP §1). So the protocol is deliberately
anaemic: the worker names a ROLE from an operator's allowlist and supplies a
prompt. It cannot choose a model, a mode, a directory, a budget, a timeout or an
effort. Those come from the profile and the operator, exactly as they do for a
job dispatched from the host.

If a field would let the worker widen its own reach, it is not in the protocol.

## Children are read-only, without exception

A nested writer would need a second worktree: leases are exclusive, and two
writers on one worktree is the case the lease exists to refuse. Switchgear does
not create worktrees -- that is orchestration, and it belongs to the caller. So a
worker that needs a writing subagent must escalate to whoever launched it. A
read-only child (a scout, a second opinion, a reviewer) is exactly what this can
supply safely, and is most of what delegation is for.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
from typing import Any, Optional

from . import jobstate
from .errors import Refuse

#: Where the socket is bound inside the sandbox. Named like the credential
#: broker's for the same reason: a worker that finds one can guess the other, and
#: guessing gains it nothing.
SANDBOX_SOCKET = "/run/switchgear-delegate.sock"

#: Ceilings that apply even when an operator sets nothing. A worker that can
#: spawn children that spawn children is a fork bomb with a language model
#: attached, and the budget it burns is real money.
DEFAULT_MAX_CHILDREN = 2
DEFAULT_MAX_DEPTH = 1
PROMPT_MAX_CHARS = 4000


def policy_for(budget: dict[str, Any]) -> dict[str, Any]:
    """Operator-owned delegation policy, absent meaning DISABLED.

    Same file as `daily_usd` and `acceptance`, same reasoning: a project that can
    grant itself the right to spawn agents has not been granted anything.

    Absent is off rather than defaulted-on because this is the one feature here
    that lets a sandboxed process cause new spend and new processes. A default
    that quietly enabled it would be a change to the threat model disguised as a
    convenience.
    """
    raw = budget.get("delegation")
    if not raw:
        return {"enabled": False, "roles": (), "max_children": 0, "max_depth": 0}
    if not isinstance(raw, dict):
        raise Refuse(
            'delegation must be an object, e.g. {"delegation": {"enabled": true, '
            '"roles": ["scout"], "max_children": 2}}'
        )
    roles = raw.get("roles") or []
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise Refuse("delegation.roles must be a list of role names from the profile")
    enabled = bool(raw.get("enabled")) and bool(roles)
    return {
        "enabled": enabled,
        # A tuple so a handler thread cannot mutate the allowlist it is checking.
        "roles": tuple(roles),
        "max_children": int(raw.get("max_children") or DEFAULT_MAX_CHILDREN),
        "max_depth": int(raw.get("max_depth") or DEFAULT_MAX_DEPTH),
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    broker: "DelegationBroker"

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The worker may be gone; that is not the controller's problem.
            pass

    def _deny(self, reason: str) -> None:
        # Denials are recorded, because "the worker asked for something it was
        # not allowed" is evidence about the worker and belongs in the record.
        self.broker.note_denial(reason)
        self._reply(403, {"error": reason})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/delegate":
            return self._deny(f"unknown path {self.path!r}; only POST /delegate exists")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny("bad Content-Length")
        if length <= 0 or length > 64 * 1024:
            return self._deny("request body must be 1..65536 bytes")
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return self._deny("body must be JSON")
        if not isinstance(req, dict):
            return self._deny("body must be a JSON object")

        # Explicit allowlist of KEYS, not just of values. An unknown key is
        # refused rather than ignored, so a worker cannot discover that some
        # future field is silently accepted by trying it.
        unknown = set(req) - {"role", "prompt"}
        if unknown:
            return self._deny(
                f"unknown field(s) {sorted(unknown)}. A delegation request carries "
                "a role and a prompt; model, mode, directory and limits are not "
                "the worker's to choose."
            )
        role, prompt = req.get("role"), req.get("prompt")
        if not isinstance(role, str) or not isinstance(prompt, str):
            return self._deny("role and prompt must both be strings")
        if not prompt.strip():
            return self._deny("prompt is empty")
        if len(prompt) > PROMPT_MAX_CHARS:
            return self._deny(
                f"prompt is {len(prompt)} chars, limit is {PROMPT_MAX_CHARS}"
            )
        if role not in self.broker.roles:
            return self._deny(
                f"role {role!r} is not delegable here (allowed: "
                f"{list(self.broker.roles)})"
            )
        try:
            job_id = self.broker.spawn(role, prompt)
        except Refuse as exc:
            return self._deny(str(exc))
        self._reply(200, {"job_id": job_id, "mode": "readonly"})

    def do_GET(self) -> None:  # noqa: N802
        prefix = "/delegate/"
        if not self.path.startswith(prefix):
            return self._deny(f"unknown path {self.path!r}")
        job_id = self.path[len(prefix):]
        try:
            self._reply(200, self.broker.child_result(job_id))
        except Refuse as exc:
            self._deny(str(exc))

    def log_message(self, *args) -> None:
        return


class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    server_name = "delegate"
    server_port = 0

    def get_request(self):
        req, _ = super().get_request()
        return req, ("127.0.0.1", 0)


class DelegationBroker:
    """Controller-side subagent factory, scoped to one parent job."""

    def __init__(
        self,
        *,
        unix_socket: str,
        parent_job: str,
        worktree: str,
        state_path: str,
        profile_path: str | None,
        provider_path: str | None,
        roles: tuple[str, ...],
        max_children: int,
        max_depth: int,
        depth: int,
        socket_mode: int = 0o600,
    ) -> None:
        self.unix_socket = unix_socket
        self.parent_job = parent_job
        self.worktree = worktree
        self.state_path = state_path
        self.profile_path = profile_path
        self.provider_path = provider_path
        self.roles = roles
        self.max_children = max_children
        self.max_depth = max_depth
        self.depth = depth
        self.socket_mode = socket_mode
        self.children: list[str] = []
        self.denials: list[str] = []
        # One lock over both counters. Without it two concurrent requests can
        # each see children < max and both spawn -- the same shape as the
        # concurrency-cap over-run the soak test found.
        self._lock = threading.Lock()
        self._srv: Optional[_UnixHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def note_denial(self, reason: str) -> None:
        with self._lock:
            if len(self.denials) < 50:
                self.denials.append(reason)

    def spawn(self, role: str, prompt: str) -> str:
        with self._lock:
            if self.depth >= self.max_depth:
                raise Refuse(
                    f"delegation depth {self.depth} is already at the limit "
                    f"({self.max_depth}); a child may not delegate further"
                )
            if len(self.children) >= self.max_children:
                raise Refuse(
                    f"this job has already delegated {len(self.children)} "
                    f"subagent(s), limit is {self.max_children}"
                )
            # Reserve the slot before the subprocess starts, so a slow launch
            # cannot be raced into an over-run.
            slot = len(self.children)
            self.children.append("")

        argv = [sys.executable, "-m", "switchgear"]
        if self.profile_path:
            argv += ["--profile", self.profile_path]
        argv += ["--state", self.state_path]
        if self.provider_path:
            argv += ["--provider", self.provider_path]
        # readonly, always: `scout` is the only verb offered. A nested writer
        # needs a second worktree, and creating one is orchestration.
        argv += ["--json", "scout", self.worktree, prompt,
                 "--correlation", f"caused_by_job={self.parent_job}",
                 "--correlation", f"delegated_role={role}",
                 "--background"]
        env = dict(os.environ)
        # The child is one level deeper, and reads this to refuse delegating on.
        env["SWITCHGEAR_DELEGATION_DEPTH"] = str(self.depth + 1)
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                                 env=env, cwd=self.worktree)
        except (OSError, subprocess.SubprocessError) as exc:
            with self._lock:
                self.children.pop(slot)
            raise Refuse(f"could not launch the subagent: {exc}") from None
        if out.returncode != 0:
            with self._lock:
                self.children.pop(slot)
            raise Refuse(f"subagent launch failed: {(out.stderr or '').strip()[:300]}")
        try:
            job_id = json.loads(out.stdout)["job_id"]
        except (ValueError, KeyError):
            with self._lock:
                self.children.pop(slot)
            raise Refuse("subagent launch produced no job id") from None
        with self._lock:
            self.children[slot] = job_id
        return job_id

    def child_result(self, job_id: str) -> dict[str, Any]:
        """A child's outcome -- and only a child's.

        The scoping is the security property: without it this is a read primitive
        over the whole state root, handed to the untrusted worker, which is the
        store the sandbox deliberately cannot see.
        """
        with self._lock:
            known = job_id in self.children
        if not known:
            raise Refuse("no such subagent for this job")
        path = os.path.join(self.state_path, "jobs", job_id, "result.json")
        rec: dict[str, Any] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                parsed = json.load(fh)
            if isinstance(parsed, dict):
                rec = parsed
        except (OSError, ValueError):
            pass
        # `finished` requires the record to SAY how it finished. Bytes that parse
        # are not an outcome: an empty object, or a record whose status field is
        # absent or not a string, reported `finished` with `status: null` -- the
        # same absence-is-benign answer this method was fixed to stop giving,
        # rebuilt out of a different absence. Anything short of a stated string
        # status falls through to the same liveness triple everything else uses.
        if not isinstance(rec.get("status"), str):
            # Missing or unstated outcome used to mean `running` forever after a
            # child crashed or was cancelled. Children are background launches,
            # so the launch triple is the authority rather than a guess.
            job_dir = os.path.join(self.state_path, "jobs", job_id)
            return {
                "job_id": job_id,
                "state": jobstate.live_state(self.state_path, job_id, {}, job_dir),
            }
        # A bounded projection, not the record. The worker gets the answer it
        # asked for, not the child's paths, digests or policy.
        return {
            "job_id": job_id,
            "state": "finished",
            "status": rec.get("status"),
            "answer": rec.get("exitSummary") or (rec.get("error") or ""),
        }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "children": [c for c in self.children if c],
                "denied": len(self.denials),
                "denied_detail": sorted(set(self.denials))[:10],
            }

    def __enter__(self) -> "DelegationBroker":
        handler = type("BoundDelegateHandler", (_Handler,), {"broker": self})
        if os.path.exists(self.unix_socket):
            os.unlink(self.unix_socket)
        self._srv = _UnixHTTPServer(self.unix_socket, handler)
        os.chmod(self.unix_socket, self.socket_mode)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._srv is not None:
            try:
                os.unlink(self.unix_socket)
            except OSError:
                pass
            self._srv.shutdown()
