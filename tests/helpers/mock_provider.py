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

    emit({"type": "error", "message": f"unknown behavior {beh}"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
