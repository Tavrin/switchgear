from __future__ import annotations

import subprocess

from .errors import Refuse

PINNED_OPENCODE = "1.18.18"
PINNED_BINARY = "/home/user/.opencode/bin/opencode"


def check_opencode_version(binary: str) -> str:
    proc = subprocess.run(
        [binary, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    ver = (proc.stdout or proc.stderr or "").strip().splitlines()[-1].strip() if proc.returncode == 0 else ""
    if ver != PINNED_OPENCODE:
        raise Refuse(
            f"OpenCode version {ver!r} is outside the tested contract {PINNED_OPENCODE}"
        )
    return ver
