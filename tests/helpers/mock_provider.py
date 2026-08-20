#!/usr/bin/env python3
"""Committed mock provider. Never selected by PATH. Invoked by absolute path."""
from __future__ import annotations

import json
import os
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def parse_dir(argv: list[str]) -> str:
    d = ""
    i = 0
    while i < len(argv):
        if argv[i] == "--dir" and i + 1 < len(argv):
            d = argv[i + 1]
            i += 2
            continue
        i += 1
    return d


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "models":
        print("opencode-go/deepseek-v4-flash")
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print("mock-0")
        return 0
    directory = parse_dir(sys.argv)
    home = os.environ.get("HOME", "")
    beh = "ok"
    extra = ""
    bp = os.path.join(home, ".mock-behavior")
    if os.path.isfile(bp):
        beh = open(bp, encoding="utf-8").read().strip()
    ep = os.path.join(home, ".mock-extra")
    if os.path.isfile(ep):
        extra = open(ep, encoding="utf-8").read().strip()

    def try_write(path: str, data: str) -> str:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(data)
            return "wrote"
        except OSError as exc:
            return f"errno={exc.errno}"

    if beh == "dump-env":
        emit(
            {
                "type": "complete",
                "env": {
                    k: os.environ.get(k)
                    for k in sorted(os.environ)
                    if k.startswith("OPENCODE_") or k in {"HOME", "PATH", "GIT_DIR", "LD_PRELOAD", "PYTHONPATH"}
                },
            }
        )
        return 0

    if beh == "ok":
        emit({"type": "complete"})
        return 0

    if beh == "edit-tracked":
        r = try_write(os.path.join(directory, "README.md"), "\nmutated\n")
        emit({"type": "complete", "write": r})
        return 0

    if beh == "edit-inside":
        r = try_write(os.path.join(directory, "tracked.txt"), "worker-edit\n")
        emit(
            {
                "type": "complete",
                "write": r,
                "handoff": {
                    "summary": "edited tracked.txt",
                    "status": "awaiting_review",
                    "changes": ["tracked.txt"],
                    "remaining_risks": [],
                    "next_action": "review",
                },
            }
        )
        return 0

    if beh == "edit-outside":
        target = extra or os.path.join(directory, "..", "sibling", "canary")
        r = try_write(target, "pwned\n")
        emit({"type": "complete", "write": r, "handoff": {"summary": "escape", "status": "awaiting_review"}})
        return 0

    if beh == "symlink-escape":
        r = try_write(os.path.join(directory, "link-outside"), "pwned\n")
        emit({"type": "complete", "write": r, "handoff": {"summary": "symlink", "status": "awaiting_review"}})
        return 0

    if beh == "git-commit":
        # will fail: git dir is read-only and git not needed
        r = try_write(os.path.join(directory, ".git"), "x")
        emit({"type": "complete", "write": r, "handoff": {"summary": "git", "status": "awaiting_review"}})
        return 0

    if beh == "hang":
        time.sleep(300)
        emit({"type": "complete"})
        return 0

    if beh == "child-survive":
        # child in same pid ns dies with bwrap
        if os.fork() == 0:
            time.sleep(30)
            os._exit(0)
        emit({"type": "complete"})
        return 0

    if beh == "exit-nonzero":
        # Well-formed handoff, but the provider crashed. Must not be promotable.
        emit(
            {
                "type": "complete",
                "handoff": {
                    "summary": "crashed after writing",
                    "status": "awaiting_review",
                    "changes": [],
                    "remaining_risks": [],
                    "next_action": "review",
                },
            }
        )
        return 7

    if beh == "malformed":
        sys.stdout.write("{not-json")
        return 0

    if beh == "truncated":
        sys.stdout.write('{"type":"complete"')
        return 0

    if beh == "plain-text":
        sys.stdout.write("just text\n")
        return 0

    if beh == "prefix-garbage":
        emit({"type": "complete"})
        sys.stdout.write("GARBAGE")
        return 0

    if beh == "duplicate":
        emit({"type": "complete"})
        emit({"type": "complete"})
        return 0

    if beh == "no-handoff":
        emit({"type": "complete"})
        return 0

    if beh == "wrong-handoff":
        emit({"type": "complete", "handoff": {"summary": "x", "status": "ok"}})
        return 0

    if beh == "review-promote":
        # A reviewer must name the change it approves, which means actually
        # inspecting the worktree. This is the honest path: read it with git.
        import subprocess

        out = subprocess.run(
            ["/usr/bin/git", "-C", directory, "--no-optional-locks",
             "status", "--porcelain=v1", "-z",
             "--untracked-files=all", "--ignored=matching"],
            capture_output=True,
        ).stdout
        changed = sorted(
            {e[3:].decode("utf-8", "replace").strip() for e in out.split(b"\x00") if e}
        )
        emit(
            {
                "type": "complete",
                "review": {"verdict": "promote", "findings": [], "reviewed_files": changed},
            }
        )
        return 0

    if beh == "review-promote-noop":
        # Reviewer that inspected nothing but votes promote.
        emit({"type": "complete", "review": {"verdict": "promote", "findings": []}})
        return 0

    if beh == "review-reject":
        emit({"type": "complete", "review": {"verdict": "reject", "findings": [{"claim": "no"}]}})
        return 0

    if beh == "review-empty":
        emit({"type": "complete"})
        return 0

    if beh == "slow-stream":
        # Emit, pause, emit. Lets a test observe evidence/events.jsonl WHILE the
        # job is still running -- the property that streaming exists for.
        #
        # Shaped to match tests/fixtures/opencode-real-scout.jsonl, NOT invented.
        # sessionID at top level on every event, part.tool for a tool call,
        # part.reason/tokens/cost on the finish. The mock inventing its own
        # vocabulary is this project's worst defect to date; a mock that streams
        # events the normalizer cannot read is that bug in miniature.
        sid = "ses_mock000000000000000000"
        emit({"type": "step_start", "sessionID": sid,
              "part": {"type": "step-start", "sessionID": sid}})
        emit({"type": "tool_use", "sessionID": sid,
              "part": {"type": "tool", "tool": "read", "sessionID": sid,
                       "state": {"status": "completed", "input": {"filePath": "calc.py"}}}})
        time.sleep(float(extra or "2"))
        emit({"type": "text", "sessionID": sid,
              "part": {"type": "text", "text": "mock finished", "sessionID": sid}})
        emit({"type": "step_finish", "sessionID": sid,
              "part": {"type": "step-finish", "reason": "stop", "sessionID": sid,
                       "tokens": {"total": 42}, "cost": 1.5e-05}})
        return 0

    if beh == "many-text":
        # Enough real raw events to force the digest's byte cap. Keeping this in
        # the mock makes the truncation test exercise the rail and adapter
        # instead of manufacturing the normalized projection it is meant to
        # verify.
        sid = "ses_mock000000000000000000"
        emit({"type": "step_start", "sessionID": sid})
        for index in range(80):
            emit({"type": "text", "sessionID": sid,
                  "part": {"type": "text", "sessionID": sid,
                           # Sized so the old "fill, then append sentinel"
                           # algorithm left fewer bytes than the sentinel needs.
                           # A larger payload happened to leave enough slack and
                           # let that broken accounting pass by accident.
                           "text": f"event-{index}-" + "x" * 49}})
        emit({"type": "step_finish", "sessionID": sid,
              "part": {"type": "step-finish", "reason": "stop", "sessionID": sid,
                       "tokens": {"total": 80}, "cost": 0.0}})
        return 0

    if beh == "whoami":
        # Reports the identity the worker actually runs as, so the uid boundary
        # can be asserted from outside rather than assumed from a flag.
        emit({"type": "step_start", "sessionID": "ses_mock000000000000000000"})
        emit({"type": "text", "sessionID": "ses_mock000000000000000000",
              "part": {"type": "text", "sessionID": "ses_mock000000000000000000",
                       "text": f"uid={os.getuid()} gid={os.getgid()}"}})
        emit({"type": "step_finish", "sessionID": "ses_mock000000000000000000",
              "part": {"type": "step-finish", "reason": "stop",
                       "sessionID": "ses_mock000000000000000000",
                       "tokens": {"total": 1}, "cost": 0.0}})
        return 0

    if beh == "delegate-probe":
        # A worker that goes looking for the delegation socket from INSIDE the
        # sandbox, and reports what it found. Proves two opposite things
        # depending on the operator's policy: that the socket is genuinely
        # reachable when delegation is on, and that it is ABSENT (not present
        # and refusing) when it is off.
        import socket as _socket

        sid = "ses_mock000000000000000000"
        sock_path = "/run/switchgear-delegate.sock"
        if not os.path.exists(sock_path):
            found = "no-delegate-socket"
        else:
            # The worker is hostile by assumption, so probe for the thing it
            # would actually try: a role it was not granted.
            body = json.dumps({"role": os.environ.get("MOCK_DELEGATE_ROLE", "implement"),
                               "prompt": "do my bidding"}).encode()
            req = (b"POST /delegate HTTP/1.1\r\nHost: d\r\n"
                   b"Content-Type: application/json\r\n"
                   b"Content-Length: " + str(len(body)).encode() +
                   b"\r\nConnection: close\r\n\r\n" + body)
            try:
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                s.settimeout(20)
                s.connect(sock_path)
                s.sendall(req)
                raw = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                s.close()
                status = raw.split(b" ")[1].decode() if b" " in raw else "?"
                found = f"delegate-reachable status={status}"
            except OSError as exc:
                found = f"delegate-unreachable {type(exc).__name__}"
        emit({"type": "step_start", "sessionID": sid})
        emit({"type": "text", "sessionID": sid,
              "part": {"type": "text", "sessionID": sid, "text": found}})
        emit({"type": "step_finish", "sessionID": sid,
              "part": {"type": "step-finish", "reason": "stop", "sessionID": sid,
                       "tokens": {"total": 1}, "cost": 0.0}})
        return 0

    if beh == "spawn-orphan":
        # A provider that leaves a long-lived child behind, which is exactly what
        # Codex does: every job spawns an app-server, which spawns the MCP
        # servers from its config, and upstream reaps neither. Measured on
        # another project: 114 app-servers, 754 processes, 15.4GB, swap
        # exhausted.
        #
        # The child announces itself with a HEARTBEAT FILE in the synthetic HOME,
        # which the host can see, rather than by a process name the host could
        # grep for. That is deliberate: process matching is how the same project
        # fooled itself three separate times -- `pgrep -f` matched the operator's
        # own shell command and reported a dead job as running. A heartbeat that
        # stops is unambiguous; a pattern that matches is not.
        import subprocess as _sp

        beat = os.path.join(home, "orphan-heartbeat")
        devnull = os.open(os.devnull, os.O_RDWR)
        _sp.Popen(
            ["/usr/bin/python3", "-c",
             "import time\n"
             "while True:\n"
             f"    open({beat!r}, 'w').write(str(time.time()))\n"
             "    time.sleep(0.2)\n"],
            stdin=devnull, stdout=devnull, stderr=devnull,
            start_new_session=True, close_fds=True,
        )
        os.close(devnull)
        emit({"type": "step_start", "sessionID": "ses_mock000000000000000000"})
        # Stay alive briefly so the test can watch the heartbeat WHILE the job
        # runs. Without that the test would pass just as happily against a mock
        # that never spawned anything.
        time.sleep(float(os.environ.get("SWITCHGEAR_MOCK_HOLD") or "4"))
        emit({"type": "step_finish", "sessionID": "ses_mock000000000000000000",
              "part": {"type": "step-finish", "reason": "stop",
                       "sessionID": "ses_mock000000000000000000",
                       "tokens": {"total": 1}, "cost": 0.0}})
        return 0

    if beh == "leak-secret":
        # A worker that prints something secret-shaped into its own output --
        # cat of a .env, an echoed header, a key pasted into reasoning. The rail
        # must FLAG it and still complete the job: evidence is audit material and
        # the run already happened.
        sid = "ses_mock000000000000000000"
        emit({"type": "step_start", "sessionID": sid,
              "part": {"type": "step-start", "sessionID": sid}})
        emit({"type": "text", "sessionID": sid,
              "part": {"type": "text", "sessionID": sid,
                       "text": "found in .env: xai-" + "k" * 40}})
        emit({"type": "step_finish", "sessionID": sid,
              "part": {"type": "step-finish", "reason": "stop", "sessionID": sid,
                       "tokens": {"total": 7}, "cost": 1e-06}})
        return 0

    if beh == "rewrite-stdout":
        # A hostile worker trying to erase what it already emitted. stdout is a
        # pipe, so the seek must fail and the earlier event must survive.
        emit({"type": "step_start", "marker": "FIRST-EVENT-MUST-SURVIVE"})
        try:
            os.lseek(1, 0, os.SEEK_SET)
            os.ftruncate(1, 0)
            outcome = "seek-succeeded"
        except OSError as exc:
            outcome = f"errno={exc.errno}"
        emit({"type": "step_finish", "seek": outcome})
        return 0

    emit({"type": "error", "message": f"unknown behavior {beh}"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
